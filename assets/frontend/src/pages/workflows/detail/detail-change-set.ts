import type { WorkflowStep, WorkflowNodeRef } from "@/pages/workflows/shared/api";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { workflowIOIssueMessage } from "./detail-webhook";
import { objectOrEmpty } from "./detail-interface";
import { textOrEmpty } from "./detail-test-run";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  appendGraphIntent,
  attachCreationPlacement,
  appendSavedAction,
  applyGraphJournal,
  canRedoGraphIntent,
  canUndoGraphIntent,
  discardGraphJournal,
  dropStepOperations,
  emptyGraphJournal,
  flattenGraphJournal,
  graphJournalIsEmpty,
  hasActiveGraphIntent,
  latestGraphIntentToken,
  noteActionSequence,
  placementRedoneBy,
  placementUndoneBy,
  redoSequence,
  retirePlacementIntents,
  redoGraphIntent as replayGraphIntent,
  undoGraphIntent as reverseGraphIntent,
  undoSequence,
  type GraphJournal,
  type GraphIntentToken,
  type CanvasPlacementDelta,
  type CanvasPlacementValues,
} from "@/pages/workflows/builder/canvas-history";
import { definitionRestoreOperations, stableDefinitionJson } from "@/pages/workflows/builder/canvas-history-restore";
import { hookMemberId } from "@/pages/workflows/builder/hook-frames";
import { nodeIdForRef } from "@/pages/workflows/builder/layout";

import { useI18n } from "@/lib/i18n/useI18n";
import {
  applyWorkflowChanges,
  isWorkflowRevisionConflict,
  workflowChangeErrorIssues,
  type WorkflowChangeIssue,
  type WorkflowChangeOperation,
} from "@/pages/workflows/builder/api";
import {
  applyChangeOperations,
  foldStagedStepEdits,
  localDefinitionIssues,
  reconcileChangeOperations,
  operationTouchesGroup,
  operationTouchesStep,
  type WorkflowDefinitionModel,
} from "@/pages/workflows/builder/change-set";

export type WorkflowStructureStatus = "clean" | "pending" | "applying" | "conflict" | "error";
export type StagedStepNoticeOwner = Readonly<{ isCurrent: () => boolean }>;

/**
 * Where the issues shown before Apply come from: the server's dry run of the
 * exact set ("checked"), a dry run the server refused outright ("refused",
 * an invalid operation), or, only when the dry run could not be made at all,
 * the local estimate ("unavailable").
 */
export type WorkflowChangeCheckState = "idle" | "checking" | "checked" | "refused" | "unavailable";

const DRY_RUN_DELAY_MS = 400;

type ChangeCheck = {
  state: WorkflowChangeCheckState;
  blocking: WorkflowChangeIssue[];
  kept: WorkflowChangeIssue[];
};

const IDLE_CHECK: ChangeCheck = { state: "idle", blocking: [], kept: [] };
const EMPTY_JOURNAL = emptyGraphJournal();

function issueIdentity(issue: WorkflowChangeIssue): string {
  const params = Object.fromEntries(Object.entries(objectOrEmpty(issue.message_params)).filter(([, value]) =>
    value === null || ["string", "number", "boolean"].includes(typeof value),
  ));
  return JSON.stringify([
    ...[issue.code, issue.path, issue.message].map((part) => String(part ?? "")),
    textOrEmpty(issue.message_key),
    Object.keys(params).sort((left, right) => left.localeCompare(right)).map((key) => [key, params[key]]),
  ]);
}

export function changeIssueMessage(
  issue: WorkflowChangeIssue,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  const message = String(issue.message || issue.code || "");
  if (!message && !issue.message_key) return "";
  return workflowIOIssueMessage({ ...issue, message }, t);
}

/**
 * What the change-set bar says after Apply: a refused Apply says first that
 * nothing was applied, then why, so it never reads like a success.
 */
export function applyRefusalMessage(
  applyError: string,
  refused: boolean,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  const notApplied = t("react.workflows.change_set_apply_failed", "The changes were not applied.");
  if (!refused || !applyError || applyError === notApplied) return applyError;
  return t("react.workflows.change_set_apply_refused", "The changes were not applied: {reason}", { reason: applyError });
}

function issueList(value: unknown): WorkflowChangeIssue[] {
  if (!Array.isArray(value)) return [];
  const unique = new Map<string, WorkflowChangeIssue>();
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const issue = item as WorkflowChangeIssue;
    unique.set(issueIdentity(issue), issue);
  }
  return [...unique.values()];
}

export type WorkflowChangeSetController = ReturnType<typeof useWorkflowChangeSet>;

/** A recovered session's journal and the server checkpoint it was made on. */
export type WorkflowJournalRestore = {
  journal: GraphJournal;
  checkpointRevision: number;
  checkpointDefinition: WorkflowDefinitionModel;
};

const STEP_STRUCTURE_FIELDS = new Set(["step_id", "step_kind", "group_id", "for_each_id", "lane_id"]);

function sameDefinition(left: WorkflowDefinitionModel, right: WorkflowDefinitionModel): boolean {
  return stableDefinitionJson(left) === stableDefinitionJson(right);
}

/**
 * The builder's local, unapplied set of structural changes and its history.
 *
 * Structural edits (create/delete steps and groups, edges, entries,
 * membership, a step's ports and parameters) collect here until the author
 * presses Apply, which sends them as one `POST /changes` against the revision
 * they were made on. Every edit is one action of the owner-local Undo/Redo
 * history (Canvas › Undo). The history survives Apply: undoing an applied
 * action stages its reverse, and the server changes only on the next Apply.
 *
 * Nothing is merged silently: a newer server revision that this controller did
 * not write keeps the local copy (preview and history) as it was and reports a
 * conflict; the author compares and chooses (`keepOwnVersionApplied`,
 * `acceptServerVersion`) or decides later. Discard drops what is unapplied.
 */
export function useWorkflowChangeSet({
  refetchWorkflowSurfaces,
  serverDefinition,
  serverRevision,
  workflowId,
  placementViewKey = "",
}: {
  refetchWorkflowSurfaces: () => Promise<unknown>;
  serverDefinition: WorkflowDefinitionModel;
  serverRevision: number | null;
  workflowId: string;
  placementViewKey?: string;
}) {
  const { t } = useI18n();
  const [journalState, setJournalState] = useState<{ owner: string; journal: GraphJournal; writeVersion: number }>(() => ({
    owner: workflowId,
    journal: emptyGraphJournal(),
    writeVersion: 0,
  }));
  const journal = journalState.owner === workflowId ? journalState.journal : EMPTY_JOURNAL;
  const journalRef = useRef(journal);
  // A queued write supersedes render-captured canonical rows before React commits it.
  const journalWriteVersionRef = useRef(0);
  const journalWriteVersion = journalState.writeVersion;
  const journalResetEpochRef = useRef(0);
  useLayoutEffect(() => { journalRef.current = journal; }, [journal]);
  // The server checkpoint the local copy was made on, while a newer revision this
  // controller did not write is shown elsewhere: the preview stays on it.
  const [held, setHeld] = useState<{
    owner: string;
    revision: number;
    definition: WorkflowDefinitionModel;
    /** The newer server revision that was seen; null for a recovered session. */
    seenRevision: number | null;
  } | null>(null);
  const heldCheckpoint = held?.owner === workflowId ? held : null;
  const baseDefinition = heldCheckpoint?.definition ?? serverDefinition;
  const intentTokenControllerRef = useRef(Symbol("workflow-graph-intents"));
  const intentTokenSerialRef = useRef(0);
  const lastStagedIntentRef = useRef<GraphIntentToken | null>(null);
  const placementReplaySerialRef = useRef(0);
  const placementSubjectGenerationRef = useRef(new Map<string, number>());
  const placementSubjectGeneration = useCallback((id: string) => placementSubjectGenerationRef.current.get(id) ?? 0, []);
  const notePlacementSubjects = useCallback((ids: readonly string[]) => {
    for (const id of ids) placementSubjectGenerationRef.current.set(id, placementSubjectGeneration(id) + 1);
  }, [placementSubjectGeneration]);
  const placementViewRef = useRef({ workflowId, key: placementViewKey });
  const placementViewGenerationRef = useRef(0);
  // Captured by this render; callbacks from A cannot become valid again after A→B→A.
  const placementViewGeneration = placementViewGenerationRef.current;
  const [placementReplay, setPlacementReplay] = useState<{
    ownerWorkflowId: string;
    serial: number;
    canvasWorkflowId: string;
    subjectIds: string[];
    viewKey: string;
    values: CanvasPlacementValues;
  } | null>(null);
  // A new step's parameter edits travel inside its creation (one change on the wire).
  const rawOperations = useMemo(() => foldStagedStepEdits(flattenGraphJournal(journal)), [journal]);
  const operations = useMemo(
    () => reconcileChangeOperations(baseDefinition, rawOperations),
    [rawOperations, baseDefinition],
  );
  const ownerEpochRef = useRef(0);
  const ownerIdRef = useRef(workflowId);
  const ownerMatches = useCallback((owner: string, epoch: number) => (
    ownerIdRef.current === owner && ownerEpochRef.current === epoch
  ), []);
  const updateJournal = useCallback((change: (current: GraphJournal) => GraphJournal) => {
    if (ownerIdRef.current !== workflowId) return;
    journalWriteVersionRef.current += 1;
    setJournalState((current) => {
      if (ownerIdRef.current !== workflowId) return current;
      return {
        owner: workflowId,
        journal: change(current.owner === workflowId ? current.journal : emptyGraphJournal()),
        // Count only writes included in this snapshot, including React's replay.
        writeVersion: current.writeVersion + 1,
      };
    });
  }, [workflowId]);
  const [baseRevision, setBaseRevision] = useState<number | null>(null);
  const [applying, setApplying] = useState(false);
  const applyingRef = useRef(false);
  const [conflict, setConflict] = useState(false);
  const [applyError, setApplyError] = useState("");
  // Apply was attempted and nothing was written: the set stays unapplied.
  const [applyRefused, setApplyRefused] = useState(false);
  const [applyIssues, setApplyIssues] = useState<WorkflowChangeIssue[]>([]);
  const applyIssuesRevisionRef = useRef<number | null>(null);
  const [notice, setNotice] = useState("");
  const [check, setCheck] = useState<ChangeCheck>(IDLE_CHECK);
  const committedCheckRef = useRef(check);
  useLayoutEffect(() => { committedCheckRef.current = check; }, [check]);
  const checkSequenceRef = useRef(0);
  // Revisions this controller wrote (Apply, own one-step saves, a kept own copy).
  const ownRevisionsRef = useRef(new Set<number>());
  const expectOwnRevisionRef = useRef(false);
  const lastServerRef = useRef<{ owner: string; revision: number | null; definition: WorkflowDefinitionModel } | null>(null);

  // The hook can be reused for another workflow while an old request is pending.
  // Retire its local journal and diagnostics before that owner can stage or Apply.
  useLayoutEffect(() => {
    if (ownerIdRef.current === workflowId) return;
    ownerIdRef.current = workflowId;
    ownerEpochRef.current += 1;
    checkSequenceRef.current += 1;
    ownRevisionsRef.current = new Set();
    journalWriteVersionRef.current += 1;
    setJournalState({ owner: workflowId, journal: emptyGraphJournal(), writeVersion: journalWriteVersionRef.current });
    setHeld(null);
    setPlacementReplay(null);
    setBaseRevision(null);
    setApplying(false);
    applyingRef.current = false;
    setConflict(false);
    setApplyError("");
    setApplyRefused(false);
    setApplyIssues([]);
    applyIssuesRevisionRef.current = null;
    setNotice("");
    setCheck(IDLE_CHECK);
  }, [workflowId]);

  useLayoutEffect(() => {
    const previous = placementViewRef.current;
    placementViewRef.current = { workflowId, key: placementViewKey };
    if (previous.workflowId === workflowId && previous.key === placementViewKey) return;
    // A prior workflow or Hook frame may reappear with the same visual key.
    // Retire its callbacks on every identity transition, including A→B→A.
    placementViewGenerationRef.current += 1;
    if (previous.workflowId !== workflowId) return; // The owner effect clears its journal.
    journalWriteVersionRef.current += 1;
    setJournalState((current) => current.owner === workflowId
      ? { ...current, journal: retirePlacementIntents(current.journal), writeVersion: current.writeVersion + 1 } : current);
    setPlacementReplay(null);
  }, [placementViewKey, workflowId]);

  // A newer server revision this controller did not write never replaces the
  // local copy behind the author's back. With local work or history, the copy
  // stays on the revision it was made on and the conflict asks for a choice.
  // A revision that changed nothing of the definition (enable, a setting) only
  // moves the checkpoint when nothing is unapplied.
  useLayoutEffect(() => {
    const previous = lastServerRef.current;
    lastServerRef.current = { owner: workflowId, revision: serverRevision, definition: serverDefinition };
    if (!previous || previous.owner !== workflowId || !previous.revision || !serverRevision) return;
    if (previous.revision === serverRevision || ownRevisionsRef.current.has(serverRevision)) return;
    if (expectOwnRevisionRef.current) {
      expectOwnRevisionRef.current = false;
      return;
    }
    if (applyingRef.current || heldCheckpoint) return;
    const current = journalRef.current;
    // Canvas placements are visual: a history of them alone follows the server.
    if (graphJournalIsEmpty(current) || !current.items.some((item) => item.operations.length) && !current.pending.length) return;
    if (!current.pending.length && sameDefinition(previous.definition, serverDefinition)) return;
    setHeld({ owner: workflowId, revision: previous.revision, definition: previous.definition, seenRevision: serverRevision });
    setBaseRevision((value) => value ?? previous.revision);
    setConflict(true);
  }, [heldCheckpoint, serverDefinition, serverRevision, workflowId]);

  const preview = useMemo(
    () => applyChangeOperations(baseDefinition, operations),
    [operations, baseDefinition],
  );
  const localIssues = useMemo(
    () => localDefinitionIssues(preview.definition, t),
    [preview.definition, t],
  );
  const hasChanges = operations.length > 0;
  const canUndo = !applying && !conflict && canUndoGraphIntent(journal);
  const canRedo = !applying && !conflict && canRedoGraphIntent(journal);
  const undoSeq = canUndo ? undoSequence(journal) : null;
  const redoSeq = canRedo ? redoSequence(journal) : null;
  useEffect(() => {
    if (!hasChanges && !heldCheckpoint) setBaseRevision(null);
  }, [hasChanges, heldCheckpoint]);
  // Every edit of the set keeps it applicable: an operation whose edge an
  // earlier edit removed is restated against the definition it now meets.
  const reconcile = useCallback(
    (next: WorkflowChangeOperation[]) => reconcileChangeOperations(baseDefinition, next),
    [baseDefinition],
  );
  const checkRevision = baseRevision ?? serverRevision;
  /** Exactly the operation list stage would send after its local reconciliation. */
  const candidateOperations = useCallback(
    (next: WorkflowChangeOperation | WorkflowChangeOperation[]) =>
      reconcile([...operations, ...(Array.isArray(next) ? next : [next])]),
    [operations, reconcile],
  );

  // Judge the current local set exactly as Apply would, without writing:
  // debounced after each structural edit, and only the latest answer counts.
  useEffect(() => {
    checkSequenceRef.current += 1;
    const sequence = checkSequenceRef.current;
    if (!operations.length || conflict || !checkRevision || !workflowId) {
      // An idle reset must not repeatedly interrupt a pending journal transition.
      if (committedCheckRef.current !== IDLE_CHECK) setCheck(IDLE_CHECK);
      return undefined;
    }
    setCheck((current) => ({ ...current, state: "checking" }));
    const timer = globalThis.setTimeout(() => {
      applyWorkflowChanges(workflowId, {
        expected_revision: checkRevision,
        operations,
        dry_run: true,
      })
        .then((result) => {
          if (sequence !== checkSequenceRef.current) return;
          const blocking = issueList(result?.blocking_issues);
          const blockingIds = new Set(blocking.map(issueIdentity));
          const kept = issueList(result?.issues).filter((issue) => !blockingIds.has(issueIdentity(issue)));
          setCheck({ state: "checked", blocking, kept });
        })
        .catch((error: unknown) => {
          if (sequence !== checkSequenceRef.current) return;
          if (isWorkflowRevisionConflict(error)) {
            setConflict(true);
            setCheck(IDLE_CHECK);
            return;
          }
          const refused = workflowChangeErrorIssues(error);
          if (refused.length) {
            setCheck({ state: "refused", blocking: refused, kept: [] });
            return;
          }
          setCheck({ state: "unavailable", blocking: [], kept: [] });
        });
    }, DRY_RUN_DELAY_MS);
    return () => globalThis.clearTimeout(timer);
  }, [checkRevision, conflict, operations, workflowId]);

  // Before Apply: what Apply would refuse, and what the saved definition would keep.
  // The local estimate is only a fallback for a dry run that could not be made.
  const blockingIssueMessages = useMemo(
    () => (hasChanges ? check.blocking.map((issue) => changeIssueMessage(issue, t)).filter(Boolean) : []),
    [check.blocking, hasChanges, t],
  );
  const keptIssueMessages = useMemo(() => {
    if (!hasChanges) return [];
    if (check.state === "unavailable") return localIssues.map((issue) => issue.message);
    return check.kept.map((issue) => changeIssueMessage(issue, t)).filter(Boolean);
  }, [check.kept, check.state, hasChanges, localIssues, t]);

  const clearStagingDiagnostics = useCallback((message: string) => {
    setApplyError("");
    setApplyRefused(false);
    setApplyIssues([]);
    applyIssuesRevisionRef.current = null;
    setNotice(message || t("react.workflows.change_set_staged", "Change added to the unapplied structure."));
  }, [t]);

  /** One graph command, even when it expands to multiple structural operations. */
  const stageGraphIntent = useCallback((next: WorkflowChangeOperation[], message = ""): GraphIntentToken | null => {
    if (ownerIdRef.current !== workflowId || !next.length) return null;
    // Allocate before the pure updater. The serial never resets on Discard or
    // owner changes, and controller identity prevents collisions across mounts.
    const token: GraphIntentToken = { controller: intentTokenControllerRef.current, serial: ++intentTokenSerialRef.current };
    lastStagedIntentRef.current = token;
    setBaseRevision((current) => current ?? serverRevision);
    updateJournal((current) => appendGraphIntent(current, next, { intentToken: token }));
    clearStagingDiagnostics(message);
    return token;
  }, [clearStagingDiagnostics, serverRevision, updateJournal, workflowId]);

  /**
   * A form, drawer, palette or hook edit: one action of the history like any
   * canvas command, so Undo never skips over it to an older action.
   */
  const stage = useCallback(
    (next: WorkflowChangeOperation | WorkflowChangeOperation[], message = "") => {
      stageGraphIntent(Array.isArray(next) ? next : [next], message);
    },
    [stageGraphIntent],
  );
  /** Synchronous: the toast that reports a change captures its intent before the next render. */
  const lastStagedGraphIntent = useCallback(() => lastStagedIntentRef.current, []);
  const hasGraphIntent = useCallback((token: GraphIntentToken) =>
    ownerIdRef.current === workflowId && hasActiveGraphIntent(journalRef.current, token), [workflowId]);

  /** One local creation's parameter notice, independent of later ordinary feedback. */
  const captureStagedStepNoticeOwner = useCallback((targetStepId: string): StagedStepNoticeOwner | null => {
    if (ownerIdRef.current !== workflowId || placementViewGenerationRef.current !== placementViewGeneration) return null;
    const createsStep = (item: GraphJournal["items"][number]) => item.operations.some((operation) =>
      (operation.op === "create_step" || operation.op === "insert_step_between") && operation.step.step_id === targetStepId);
    const unapplied = (current: GraphJournal, id: number) =>
      current.pending.some((entry) => entry.item === id && entry.direction === "forward");
    const items = journalRef.current.items.slice(0, journalRef.current.cursor)
      .filter((item) => createsStep(item) && unapplied(journalRef.current, item.id));
    if (items.length !== 1) return null;
    const itemId = items[0].id;
    const ownerEpoch = ownerEpochRef.current;
    const resetEpoch = journalResetEpochRef.current;
    return { isCurrent: () => {
      const current = journalRef.current;
      return ownerMatches(workflowId, ownerEpoch) && journalResetEpochRef.current === resetEpoch &&
        placementViewGenerationRef.current === placementViewGeneration &&
        current.items.slice(0, current.cursor).some((item) => item.id === itemId && createsStep(item)) && unapplied(current, itemId);
    } };
  }, [ownerMatches, placementViewGeneration, workflowId]);

  /** Geometry belongs to the same Undo item as the successfully staged creation. */
  const attachPlacementToCreation = useCallback((token: GraphIntentToken, target: WorkflowNodeRef, placement: CanvasPlacementDelta, expectedSubjectGeneration: number): boolean => {
    const projected = placementViewKey ? hookMemberId(placementViewKey, target.id) : target.id;
    const displayId = nodeIdForRef({ ...target, id: projected });
    const bound = { ...placement, viewKey: placementViewKey };
    if (ownerIdRef.current !== workflowId || placementViewRef.current.key !== placementViewKey ||
      placementViewGenerationRef.current !== placementViewGeneration || applyingRef.current || conflict ||
      placementSubjectGeneration(displayId) !== expectedSubjectGeneration || !hasActiveGraphIntent(journalRef.current, token) ||
      attachCreationPlacement(journalRef.current, token, target, displayId, bound) === journalRef.current) return false;
    updateJournal((current) => attachCreationPlacement(current, token, target, displayId, bound));
    notePlacementSubjects([displayId]);
    return true;
  }, [conflict, notePlacementSubjects, placementSubjectGeneration, placementViewGeneration, placementViewKey, updateJournal, workflowId]);

  /** XY is a local visual intent: it never contributes a server operation. */
  const recordPlacementIntent = useCallback((placement: CanvasPlacementDelta) => {
    if (ownerIdRef.current !== workflowId || placementViewRef.current.key !== placementViewKey ||
      placementViewGenerationRef.current !== placementViewGeneration || applying || conflict || !placement.subjectIds.length ||
      !Object.keys(placement.after).length) return;
    // A placement is an intent like any other: a toast can offer Undo for exactly this move.
    const token: GraphIntentToken = { controller: intentTokenControllerRef.current, serial: ++intentTokenSerialRef.current };
    lastStagedIntentRef.current = token;
    notePlacementSubjects(placement.subjectIds);
    updateJournal((current) => appendGraphIntent(current, [], { placement: { ...placement, viewKey: placementViewKey }, intentToken: token }));
  }, [applying, conflict, notePlacementSubjects, placementViewGeneration, placementViewKey, updateJournal, workflowId]);

  const clearChangedIntentDiagnostics = useCallback((remaining: WorkflowChangeOperation[]) => {
    setApplyError("");
    setApplyRefused(false);
    setApplyIssues([]);
    applyIssuesRevisionRef.current = null;
    setNotice("");
    if (!remaining.length) {
      setBaseRevision(null);
      setConflict(false);
    }
  }, []);

  const undoGraphIntent = useCallback(() => {
    if (ownerIdRef.current !== workflowId || placementViewRef.current.key !== placementViewKey ||
      placementViewGenerationRef.current !== placementViewGeneration || applying || conflict) return;
    const next = reverseGraphIntent(journal);
    if (next === journal) return;
    const placement = placementUndoneBy(journal);
    if (placement) notePlacementSubjects(placement.subjectIds);
    updateJournal(() => next);
    // Undoing a saved action stages its reverse against the revision on screen.
    if (next.pending.length) setBaseRevision((current) => current ?? serverRevision);
    setPlacementReplay(placement ? {
      ownerWorkflowId: workflowId, serial: ++placementReplaySerialRef.current,
      canvasWorkflowId: placement.canvasWorkflowId, subjectIds: placement.subjectIds,
      viewKey: placement.viewKey ?? placementViewKey, values: placement.before,
    } : null);
    clearChangedIntentDiagnostics(reconcileChangeOperations(baseDefinition, flattenGraphJournal(next)));
  }, [applying, baseDefinition, clearChangedIntentDiagnostics, conflict, journal, notePlacementSubjects, placementViewGeneration, placementViewKey, serverRevision, updateJournal, workflowId]);

  const undoGraphIntentRef = useRef(undoGraphIntent);
  useLayoutEffect(() => { undoGraphIntentRef.current = undoGraphIntent; });
  /** A toast's Undo reverses its own change only while that change is still the latest intent. */
  const undoGraphIntentIfLatest = useCallback((token: GraphIntentToken) => {
    if (ownerIdRef.current !== workflowId || latestGraphIntentToken(journalRef.current) !== token) return false;
    undoGraphIntentRef.current();
    return true;
  }, [workflowId]);

  const redoGraphIntent = useCallback(() => {
    if (ownerIdRef.current !== workflowId || placementViewRef.current.key !== placementViewKey ||
      placementViewGenerationRef.current !== placementViewGeneration || applying || conflict) return;
    const next = replayGraphIntent(journal);
    if (next === journal) return;
    const placement = placementRedoneBy(journal);
    if (placement) notePlacementSubjects(placement.subjectIds);
    updateJournal(() => next);
    if (next.pending.length) setBaseRevision((current) => current ?? serverRevision);
    setPlacementReplay(placement ? {
      ownerWorkflowId: workflowId, serial: ++placementReplaySerialRef.current,
      canvasWorkflowId: placement.canvasWorkflowId, subjectIds: placement.subjectIds,
      viewKey: placement.viewKey ?? placementViewKey, values: placement.after,
    } : null);
    clearChangedIntentDiagnostics(reconcileChangeOperations(baseDefinition, flattenGraphJournal(next)));
  }, [applying, baseDefinition, clearChangedIntentDiagnostics, conflict, journal, notePlacementSubjects, placementViewGeneration, placementViewKey, serverRevision, updateJournal, workflowId]);

  /**
   * Edit the parameters of a step whose configuration lives in this set: a step
   * the set created, or a saved step whose kind the set changes. The edit is its
   * own action, so Undo takes back exactly it; a kind change stays structural.
   */
  const updateStagedStep = useCallback(
    (targetStepId: string, patch: Record<string, unknown>): WorkflowStep | null => {
      if (ownerIdRef.current !== workflowId || placementViewGenerationRef.current !== placementViewGeneration ||
        journalWriteVersionRef.current !== journalWriteVersion) return null;
      const current = preview.definition.steps.find((step) => step.step_id === targetStepId);
      if (!current) return null;
      const currentKind = textOrEmpty(current.step_kind);
      const nextKind = textOrEmpty(patch.step_kind) || currentKind;
      const fields = Object.fromEntries(Object.entries(patch).filter(([key, value]) => !STEP_STRUCTURE_FIELDS.has(key) && value !== undefined));
      let next: WorkflowChangeOperation[];
      if (nextKind !== currentKind) {
        next = [{ op: "change_step_kind", step_id: targetStepId, step: { ...fields, step_kind: nextKind } }];
      } else {
        const record = current as Record<string, unknown>;
        const changed = Object.fromEntries(Object.entries(fields).filter(([key, value]) =>
          stableDefinitionJson(record[key] ?? null) !== stableDefinitionJson(value ?? null)));
        if (!Object.keys(changed).length) return structuredClone(current);
        next = [{ op: "update_step_config", step_id: targetStepId, patch: changed }];
      }
      if (!stageGraphIntent(next, t("react.workflows.change_set_step_updated", "The new step's parameters are kept with the unapplied structure."))) return null;
      // Use canonical operation application, including kind-family replacement.
      return applyChangeOperations(preview.definition, next).definition.steps.find((step) => step.step_id === targetStepId) ?? null;
    },
    [journalWriteVersion, placementViewGeneration, preview.definition, stageGraphIntent, t, workflowId],
  );

  /**
   * An own one-step save that the server already holds (a saved step's or
   * group's parameters): it joins the history with its reverse, so Undo can take
   * it back locally; the server changes again only on the next Apply.
   */
  const recordSavedChange = useCallback((saved: WorkflowChangeOperation[], revision: number | null) => {
    if (ownerIdRef.current !== workflowId || !saved.length) return;
    if (revision) ownRevisionsRef.current.add(revision);
    const before = preview.definition;
    const after = applyChangeOperations(before, reconcileChangeOperations(before, saved)).definition;
    const inverse = definitionRestoreOperations(after, before);
    if (!inverse.length) return;
    const token: GraphIntentToken = { controller: intentTokenControllerRef.current, serial: ++intentTokenSerialRef.current };
    lastStagedIntentRef.current = token;
    updateJournal((current) => appendSavedAction(current, saved, inverse, { intentToken: token }));
  }, [preview.definition, updateJournal, workflowId]);

  /** Remove a step this set created, with every staged operation that mentions it. */
  const dropStagedStep = useCallback((targetStepId: string) => {
    if (ownerIdRef.current !== workflowId) return;
    updateJournal((current) => dropStepOperations(current, baseDefinition, targetStepId));
  }, [baseDefinition, updateJournal, workflowId]);

  const discard = useCallback(() => {
    if (ownerIdRef.current !== workflowId) return;
    ++journalResetEpochRef.current;
    updateJournal((current) => discardGraphJournal(current));
    setPlacementReplay(null);
    setBaseRevision(null);
    setHeld(null);
    setConflict(false);
    setApplyError("");
    setApplyRefused(false);
    setApplyIssues([]);
    applyIssuesRevisionRef.current = null;
    setNotice(t("react.workflows.change_set_discarded", "Unapplied structure changes discarded."));
  }, [t, updateJournal, workflowId]);

  const reportConflict = useCallback(() => {
    if (ownerIdRef.current !== workflowId) return;
    setConflict(true);
    setApplyError("");
    setApplyRefused(false);
  }, [workflowId]);

  /** Reload the workflow and keep the set; the author then applies it on the new revision or discards it. */
  const reload = useCallback(async () => {
    if (ownerIdRef.current !== workflowId) return false;
    const requestOwner = workflowId;
    const requestEpoch = ownerEpochRef.current;
    const refreshed = await refetchWorkflowSurfaces() as { workflow_id?: unknown; revision?: unknown } | null;
    if (!ownerMatches(requestOwner, requestEpoch)) return false;
    const revision = Number(refreshed?.revision);
    if (refreshed?.workflow_id !== workflowId || !Number.isInteger(revision) || revision <= 0) {
      setApplyError(t("react.workflows.change_set_reload_failed", "The workflow could not be reloaded. Your unapplied changes are kept. Try again."));
      return false;
    }
    // The author chose this revision as the new base: its arrival is not someone else's change.
    ownRevisionsRef.current.add(revision);
    setBaseRevision(revision);
    setHeld(null);
    setConflict(false);
    setApplyError("");
    setApplyRefused(false);
    setNotice(
      t(
        "react.workflows.change_set_reloaded",
        "Workflow reloaded. Review the unapplied changes against it, then apply or discard them.",
      ),
    );
    return true;
  }, [ownerMatches, refetchWorkflowSurfaces, t, workflowId]);

  /** After this author's own one-step save, the set continues from the new revision. */
  const rebase = useCallback(
    (revision: number, expectedPreviousRevision: number) => {
      if (ownerIdRef.current !== workflowId) return;
      // The page showed this own save's revision before reporting it: it was
      // held as someone else's change, and is released as this author's.
      const ownHeld = Boolean(heldCheckpoint && heldCheckpoint.seenRevision === revision &&
        heldCheckpoint.revision === expectedPreviousRevision);
      if (ownHeld) {
        setHeld(null);
        setConflict(false);
      }
      if (!conflict || ownHeld) {
        if (revision > expectedPreviousRevision &&
          expectedPreviousRevision === (baseRevision ?? heldCheckpoint?.revision ?? lastServerRef.current?.revision ?? serverRevision)) {
          ownRevisionsRef.current.add(revision);
        }
        // Apply diagnostics describe their saved revision, not later own saves.
        if (revision > expectedPreviousRevision && applyIssuesRevisionRef.current === expectedPreviousRevision) {
          setApplyIssues([]);
          setApplyError("");
          setApplyRefused(false);
          applyIssuesRevisionRef.current = null;
        }
        setBaseRevision((current) => (
          current === null || current !== expectedPreviousRevision
            ? current
            : revision
        ));
      }
    },
    [baseRevision, conflict, heldCheckpoint, serverRevision, workflowId],
  );

  /**
   * Another own writer on this page (a group conversion) is about to change the
   * workflow: its next revision is this author's, not someone else's.
   */
  const expectOwnRevision = useCallback(() => {
    if (ownerIdRef.current === workflowId) expectOwnRevisionRef.current = true;
  }, [workflowId]);

  const apply = useCallback(async () => {
    if (ownerIdRef.current !== workflowId || !operations.length || applying) {
      return false;
    }
    const expectedRevision = baseRevision ?? serverRevision;
    if (!expectedRevision) {
      setApplyError(t("react.workflows.change_set_revision_unknown", "The workflow revision is unknown. Reload the workflow."));
      return false;
    }
    const requestOwner = workflowId;
    const requestEpoch = ownerEpochRef.current;
    // The ack belongs to this exact set; later local actions stay unapplied.
    const appliedJournal = journalRef.current;
    const appliedBase = baseDefinition;
    setApplying(true);
    applyingRef.current = true;
    setApplyError("");
    setApplyRefused(false);
    setApplyIssues([]);
    applyIssuesRevisionRef.current = null;
    try {
      const result = await applyWorkflowChanges(workflowId, {
        expected_revision: expectedRevision,
        operations,
      });
      if (!ownerMatches(requestOwner, requestEpoch)) return false;
      if (typeof result.revision === "number" && result.revision > 0) ownRevisionsRef.current.add(result.revision);
      ++journalResetEpochRef.current;
      updateJournal((current) => {
        const saved = applyGraphJournal(appliedJournal, appliedBase);
        if (current === appliedJournal) return saved;
        // Actions staged while Apply was in flight stay unapplied after it.
        const applied = new Set(appliedJournal.pending);
        const inverses = new Map(saved.items.flatMap((item) => item.inverse ? [[item.id, item.inverse] as const] : []));
        return {
          ...current,
          items: current.items.map((item) => !item.inverse && inverses.has(item.id) ? { ...item, inverse: inverses.get(item.id) } : item),
          pending: current.pending.filter((entry) => !applied.has(entry)),
        };
      });
      setPlacementReplay(null);
      setBaseRevision(null);
      setHeld(null);
      setConflict(false);
      applyIssuesRevisionRef.current = result.revision;
      setApplyIssues(issueList(result.issues));
      setNotice(
        Array.isArray(result.issues) && result.issues.length
          ? t("react.workflows.change_set_applied_with_issues", "Changes applied. The workflow still has {count} issue(s).", {
              count: result.issues.length,
            })
          : t("react.workflows.change_set_applied", "Changes applied."),
      );
      await refetchWorkflowSurfaces();
      return ownerMatches(requestOwner, requestEpoch);
    } catch (error) {
      if (!ownerMatches(requestOwner, requestEpoch)) return false;
      if (isWorkflowRevisionConflict(error)) {
        setConflict(true);
        return false;
      }
      const issues = issueList(workflowChangeErrorIssues(error));
      applyIssuesRevisionRef.current = issues.length ? expectedRevision : null;
      setApplyRefused(true);
      setApplyIssues(issues);
      const issueText = issues.map((issue) => changeIssueMessage(issue, t)).filter(Boolean).join(" ");
      setApplyError(
        issueText || (error instanceof Error
          ? error.message
          : t("react.workflows.change_set_apply_failed", "The changes were not applied.")),
      );
      return false;
    } finally {
      if (ownerMatches(requestOwner, requestEpoch)) {
        setApplying(false);
        applyingRef.current = false;
      }
    }
  }, [applying, baseDefinition, baseRevision, operations, ownerMatches, refetchWorkflowSurfaces, serverRevision, t, updateJournal, workflowId]);

  /**
   * Continue a recovered session (a reload): its history, cursor and unapplied
   * set. On another server revision the local copy stays on its checkpoint and
   * the conflict asks the author to compare; nothing is replayed or written.
   */
  const restoreJournal = useCallback((restore: WorkflowJournalRestore) => {
    if (ownerIdRef.current !== workflowId) return;
    noteActionSequence(restore.journal);
    journalWriteVersionRef.current += 1;
    ++journalResetEpochRef.current;
    setJournalState({ owner: workflowId, journal: restore.journal, writeVersion: journalWriteVersionRef.current });
    setPlacementReplay(null);
    const sameRevision = restore.checkpointRevision === serverRevision;
    journalRef.current = restore.journal;
    if (sameRevision || (!restore.journal.pending.length && sameDefinition(restore.checkpointDefinition, serverDefinition))) {
      setHeld(null);
      setConflict(false);
      setBaseRevision(restore.journal.pending.length ? restore.checkpointRevision : null);
      return;
    }
    setHeld({ owner: workflowId, revision: restore.checkpointRevision, definition: restore.checkpointDefinition, seenRevision: null });
    setBaseRevision(restore.checkpointRevision);
    setConflict(true);
  }, [serverDefinition, serverRevision, workflowId]);

  /** "Accept the server version": a new local base and an empty Undo/Redo history. Nothing is written. */
  const acceptServerVersion = useCallback((message?: string) => {
    if (ownerIdRef.current !== workflowId) return;
    ++journalResetEpochRef.current;
    updateJournal(() => emptyGraphJournal());
    setPlacementReplay(null);
    setBaseRevision(null);
    setHeld(null);
    setConflict(false);
    setApplyError("");
    setApplyRefused(false);
    setApplyIssues([]);
    applyIssuesRevisionRef.current = null;
    setNotice(message ?? t("react.workflows.edit_session_server_accepted", "The server version is on the canvas. Your local copy and its Undo history were discarded."));
  }, [t, updateJournal, workflowId]);

  /**
   * "Keep my version" was written as `revision`: the local copy is now the
   * checkpoint, and the history continues; each action learns its reverse.
   */
  const keepOwnVersionApplied = useCallback((revision: number) => {
    if (ownerIdRef.current !== workflowId) return;
    ownRevisionsRef.current.add(revision);
    const base = baseDefinition;
    ++journalResetEpochRef.current;
    updateJournal((current) => applyGraphJournal(current, base));
    setPlacementReplay(null);
    setBaseRevision(null);
    setHeld(null);
    setConflict(false);
    setApplyError("");
    setApplyRefused(false);
    setApplyIssues([]);
    applyIssuesRevisionRef.current = null;
    setNotice(t("react.workflows.edit_session_mine_saved", "Your version was saved as revision {revision}.", { revision }));
  }, [baseDefinition, t, updateJournal, workflowId]);

  const touchesStep = useCallback(
    (targetStepId: string) => operations.some((operation) => operationTouchesStep(operation, targetStepId)),
    [operations],
  );
  const touchesGroup = useCallback(
    (groupId: string) => operations.some((operation) => operationTouchesGroup(operation, groupId)),
    [operations],
  );

  let status: WorkflowStructureStatus = "clean";
  if (applying) status = "applying";
  else if (conflict) status = "conflict";
  else if (applyError) status = "error";
  else if (hasChanges) status = "pending";

  return {
    apply,
    canRedo,
    canUndo,
    undoSeq,
    redoSeq,
    candidateOperations,
    /** The rendered journal belongs to this controller, beyond its empty loading projection. */
    get ownerReady() { return journalState.owner === workflowId; },
    applyError,
    applyIssues,
    applyRefused,
    baseRevision,
    blockingIssueMessages,
    checkState: check.state,
    discard,
    dropStagedStep,
    hasChanges,
    keptIssueMessages,
    localIssues,
    notice,
    operations,
    placementReplay,
    placementViewKey,
    get placementViewEpoch() { return placementViewGenerationRef.current; },
    attachPlacementToCreation,
    placementSubjectGeneration,
    preview: preview.definition,
    rebase,
    reload,
    reportConflict,
    setNotice,
    stage,
    stageGraphIntent,
    hasGraphIntent,
    captureStagedStepNoticeOwner,
    recordPlacementIntent,
    recordSavedChange,
    expectOwnRevision,
    undoGraphIntent,
    undoGraphIntentIfLatest,
    lastStagedGraphIntent,
    redoGraphIntent,
    status,
    touchesGroup,
    touchesStep,
    updateStagedStep,
    workflowId,
    /** The session journal and the checkpoint it is made on (Canvas › Undo, edit session). */
    journal,
    checkpoint: {
      revision: heldCheckpoint?.revision ?? baseRevision ?? serverRevision,
      definition: baseDefinition,
      held: Boolean(heldCheckpoint),
    },
    restoreJournal,
    acceptServerVersion,
    keepOwnVersionApplied,
  };
}
