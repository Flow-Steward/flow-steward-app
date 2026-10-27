import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { containerRuntimeMarks, snapshotContainersForOwner, type ContainerRuntimeMarks } from "@/pages/workflows/builder/runtime-container-marks";
import { runtimeEdgePassCounts, snapshotTraceForOwner } from "@/pages/workflows/builder/runtime-edge-marks";
import { useJobExecutionSnapshot } from "@/pages/jobs/detail/job-execution-snapshot";
import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { HookExecutionObserver, type ExecutionSource, type SnapshotListener } from "./detail-hook-execution-observer";
import { hookExecutionMarks, hookInvocationForExecution, workflowExecutionMarks, type HookExecutionMarks, type HookExecutionOwner } from "./detail-hook-execution-marks";

/** A trusted selected-run proof from the page's owner controller, not project authorization. */
export type ControlledRunOwner = { projectId: string; ownerEpoch: number };
export type ControlledParentRun = {
  proof: { workflowId: string; projectId: string; editorRevision: number | null; jobId: string; generation: number };
  snapshot: JobExecutionSnapshot | null;
};
type IsolatedRun = { jobId: string; hookId: string; workflowId: string; revision: number; parentJobId?: string; controlIdentity: string; controlEpoch: number };

/** One snapshot authority for author steps and their actual immediate children. */
export function useWorkflowHookExecutionMarks(
  graph: WorkflowCanvasGraph | undefined,
  frames: ReadonlyMap<string, HookExecutionOwner>,
  workflowId: string,
  revision: number,
  acknowledgedJobId?: string,
  observeAcknowledgedJob = true,
  controlledRun: { parent?: ControlledParentRun | null; owner?: ControlledRunOwner } = {},
) {
  const { parent: controlledParent, owner: controlledOwner } = controlledRun;
  const controlled = controlledParent !== undefined;
  const proof = controlledParent?.proof;
  const suppliedOwnerValid = Boolean(controlledOwner?.projectId.trim() && Number.isInteger(controlledOwner.ownerEpoch) && controlledOwner.ownerEpoch > 0);
  const proofMatchesView = Boolean(proof?.workflowId === workflowId && proof.editorRevision === revision &&
    proof.jobId && proof.projectId && Number.isInteger(proof.generation) && proof.generation > 0);
  const controlledAuthority = !controlled || (controlledParent === null ? suppliedOwnerValid
    : proofMatchesView && (!controlledOwner || suppliedOwnerValid && controlledOwner.projectId === proof?.projectId));
  const projectId = controlled && controlledAuthority ? controlledOwner?.projectId || proof?.projectId : undefined;
  const controlIdentity = controlled
    ? JSON.stringify(["controlled", workflowId, revision, controlledOwner?.projectId || "", controlledOwner?.ownerEpoch ?? null,
      proof?.workflowId || "", proof?.projectId || "", proof?.editorRevision ?? null, proof?.jobId || "", proof?.generation ?? null])
    : JSON.stringify(["legacy", workflowId, revision, acknowledgedJobId ?? null]);
  const [controlState, setControlState] = useState(() => ({ identity: controlIdentity, epoch: 1 }));
  // Adjust derived identity state before committing the changed view. The
  // conditional rerender preserves the monotonic epoch without an effect update.
  if (controlState.identity !== controlIdentity) setControlState((current) => current.identity === controlIdentity ? current
    : { identity: controlIdentity, epoch: current.epoch + 1 });
  const activeEpoch = controlState.identity === controlIdentity ? controlState.epoch : null;
  const currentControl = useRef({ identity: controlIdentity, epoch: activeEpoch });
  useLayoutEffect(() => {
    currentControl.current = { identity: controlIdentity, epoch: activeEpoch };
  }, [activeEpoch, controlIdentity]);
  const [parent, setParent] = useState<JobExecutionSnapshot | null>(null);
  const [isolated, setIsolated] = useState<IsolatedRun | null>(null);
  // Keep the hook call stable; controlled mode never starts a duplicate parent observer.
  const { snapshot: acknowledgedSnapshot } = useJobExecutionSnapshot(controlled ? "" : acknowledgedJobId || "",
    !controlled && Boolean(acknowledgedJobId) && observeAcknowledgedJob);
  const observedParent = observeAcknowledgedJob ? acknowledgedSnapshot : parent;
  const legacyParent = acknowledgedJobId === undefined ? parent
    : observedParent?.jobId === acknowledgedJobId ? observedParent : null;
  const controlledSnapshot = controlledAuthority && proof?.workflowId === workflowId && proof.editorRevision === revision && proof.jobId &&
    controlledParent?.snapshot?.jobId === proof.jobId && controlledParent.snapshot.definitionWorkflowId === workflowId
    ? controlledParent.snapshot : null;
  const parentSnapshot = controlled ? activeEpoch === null ? null : controlledSnapshot : legacyParent;
  const parentJobId = controlled ? proof?.jobId : acknowledgedJobId;
  const isolatedRun = (!controlled || controlledAuthority) && isolated && isolated.parentJobId === parentJobId && isolated.controlIdentity === controlIdentity &&
    isolated.controlEpoch === activeEpoch ? isolated : null;
  const [evidence, setEvidence] = useState<Record<string, JobExecutionSnapshot>>({});
  const onWorkflowSnapshot = useCallback((snapshot: JobExecutionSnapshot | null) => {
    if (controlled || currentControl.current.identity !== controlIdentity || currentControl.current.epoch !== activeEpoch) return;
    setParent(acknowledgedJobId !== undefined && snapshot?.jobId !== acknowledgedJobId ? null : snapshot);
    setIsolated(null);
  }, [acknowledgedJobId, activeEpoch, controlIdentity, controlled]);
  const onHookTestRunStarted = useCallback((jobId: string, hookId: string) => {
    if (currentControl.current.identity !== controlIdentity || currentControl.current.epoch !== activeEpoch || activeEpoch === null ||
      controlled && !controlledAuthority) return;
    if (!controlled) setParent(null);
    setIsolated(jobId ? { jobId, hookId, workflowId, revision, parentJobId, controlIdentity, controlEpoch: activeEpoch } : null);
  }, [activeEpoch, controlIdentity, controlled, controlledAuthority, parentJobId, workflowId, revision]);
  const activeSourceKeys = useRef<ReadonlySet<string>>(new Set());
  useLayoutEffect(() => () => {
    currentControl.current = { identity: "unmounted", epoch: null };
    activeSourceKeys.current = new Set();
  }, []);
  const onSnapshot = useCallback<SnapshotListener>((key, snapshot) => {
    if (!activeSourceKeys.current.has(key)) return;
    setEvidence((current) => {
      if (current[key] === snapshot || (!snapshot && !(key in current))) return current;
      const next = { ...current };
      if (snapshot) next[key] = snapshot;
      else delete next[key];
      return next;
    });
  }, []);
  const sources = useMemo<ExecutionSource[]>(() => {
    if (controlled && activeEpoch === null) return [];
    const prefix = controlled ? `controlled:${activeEpoch}:` : "";
    if (isolatedRun) {
      if (isolatedRun.workflowId !== workflowId || isolatedRun.revision !== revision) return [];
      const frame = [...frames.values()].find((candidate) => candidate.hookId === isolatedRun.hookId);
      return frame ? [{ key: `${prefix}isolated:${isolatedRun.jobId}:${frame.hookId}:${frame.targetWorkflowId}:${frame.targetRevision}`, jobId: isolatedRun.jobId, frame, ...(projectId ? { projectId } : {}) }] : [];
    }
    return [...frames.values()].flatMap((frame) => {
      const invocation = hookInvocationForExecution(parentSnapshot, frame, workflowId, revision);
      return invocation ? [{ key: `${prefix}whole:${parentSnapshot?.jobId}:${frame.hookId}:${invocation.stepExecutionId}:${invocation.childJobId}`, jobId: invocation.childJobId, frame, ...(projectId ? { projectId } : {}) }] : [];
    });
  }, [activeEpoch, controlled, frames, isolatedRun, parentSnapshot, projectId, revision, workflowId]);
  useLayoutEffect(() => { activeSourceKeys.current = new Set(sources.map((source) => source.key)); }, [sources]);
  const marks = useMemo<HookExecutionMarks>(() => {
    if (!graph) return {};
    const ordinaryMarks = isolatedRun ? {} : workflowExecutionMarks(parentSnapshot, graph, workflowId, revision, controlled ? parentJobId : acknowledgedJobId);
    return Object.assign(ordinaryMarks, ...sources.map((source) => hookExecutionMarks(
      evidence[source.key]?.jobId === source.jobId ? evidence[source.key] : null, source.frame, graph,
    )));
  }, [acknowledgedJobId, controlled, evidence, graph, isolatedRun, parentJobId, parentSnapshot, revision, sources, workflowId]);
  const edges = useMemo(() => {
    if (!graph || revision <= 0) return new Map<string, number>();
    const ordinary = isolatedRun ? new Map<string, number>() : runtimeEdgePassCounts(
      snapshotTraceForOwner(parentSnapshot, workflowId, revision, controlled ? parentJobId : acknowledgedJobId), graph, frames,
    );
    for (const source of sources) {
      const snapshot = evidence[source.key];
      const child = snapshotTraceForOwner(snapshot, source.frame.targetWorkflowId, source.frame.targetRevision || 0, source.jobId);
      for (const [key, count] of runtimeEdgePassCounts(child, graph, frames, source.frame.hookId)) ordinary.set(key, (ordinary.get(key) || 0) + count);
    }
    return ordinary;
  }, [acknowledgedJobId, controlled, evidence, frames, graph, isolatedRun, parentJobId, parentSnapshot, revision, sources, workflowId]);
  const containerMarks = useMemo<ContainerRuntimeMarks>(() => {
    if (!graph || revision <= 0 || !projectId || controlled && !controlledAuthority) return {};
    const ordinary = isolatedRun ? {} : containerRuntimeMarks(
      snapshotContainersForOwner(parentSnapshot, workflowId, revision, projectId, controlled ? parentJobId : acknowledgedJobId), graph, frames,
    );
    for (const source of sources) {
      const child = snapshotContainersForOwner(evidence[source.key], source.frame.targetWorkflowId, source.frame.targetRevision || 0, source.projectId, source.jobId);
      Object.assign(ordinary, containerRuntimeMarks(child, graph, frames, source.frame.hookId));
    }
    return ordinary;
  }, [acknowledgedJobId, controlled, controlledAuthority, evidence, frames, graph, isolatedRun, parentJobId, parentSnapshot, projectId, revision, sources, workflowId]);
  const observers = sources.map((source) => <HookExecutionObserver key={source.key} onSnapshot={onSnapshot} source={source} />);
  return { marks, edges, containerMarks, observers, onWorkflowSnapshot, onHookTestRunStarted };
}
