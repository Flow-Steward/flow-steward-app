import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import {
  authoringContentOf,
  buildEditSession,
  contentWithDefinition,
  definitionOfContent,
  newEditSessionId,
  parseStoredEditSession,
  sessionHasLocalWork,
  storableFormDraft,
  type RecoveryProblem,
  type StoredFormDraft,
  type WorkflowAuthoringContent,
  type WorkflowEditSessionIdentity,
  type WorkflowServerCheckpoint,
} from "@/pages/workflows/detail/workflow-edit-session";
import {
  claimSessionOwnership,
  clearSessionReference,
  createWorkflowEditSessionStore,
  readSessionReference,
  writeSessionReference,
  type SessionOwnership,
  type WorkflowEditSessionStore,
} from "@/pages/workflows/detail/workflow-edit-session-store";

export type WorkflowEditSessionStatus = "off" | "loading" | "active" | "recovery_unavailable";
/** Whether the latest completed action is in the browser's database. */
export type WorkflowEditSessionPersistence = "idle" | "saving" | "saved" | "failed" | "unavailable";

type ActiveSession = {
  key: string;
  identity: WorkflowEditSessionIdentity;
  sessionId: string;
  origin: WorkflowServerCheckpoint;
  checkpoint: WorkflowServerCheckpoint;
  /** One more for every author action, Undo, Redo or draft change; not for a server refresh. */
  editSequence: number;
  lastJournal: unknown;
  lastDrafts: unknown;
  /** One more for every write, so only the newest completed write reports "saved". */
  writeSequence: number;
  ownership: SessionOwnership | null;
  writes: Promise<void>;
  generation: number;
};

const NO_DRAFTS: Record<string, StoredFormDraft> = {};
type LockManagerLike = Exclude<Parameters<typeof claimSessionOwnership>[1], undefined>;
type TabStorage = Exclude<Parameters<typeof readSessionReference>[1], undefined>;

function identityKey(identity: WorkflowEditSessionIdentity | null): string {
  return identity ? JSON.stringify([identity.accountId, identity.userId, identity.workflowId]) : "";
}

function workflowPathPrefix(workflowId: string): string {
  return `/registry/workflows/${encodeURIComponent(workflowId)}`;
}

/**
 * One workflow's edit session for this tab (Canvas › Undo, edit session).
 *
 * It recovers the session this tab had (a reload), continues its history in
 * the change set, and writes the session after every completed action. It
 * never writes the workflow: a recovered copy on another server revision is
 * held by the change set for the author to compare. A record that cannot be
 * recovered (another version, damaged, another user) is reported and kept
 * until the author deletes it. Leaving the workflow normally ends the session;
 * a reload, a crash or a switch between this workflow's pages does not.
 */
export function useWorkflowEditSession({
  enabled,
  identity,
  workflow,
  serverRevision,
  changeSet,
  store: injectedStore,
  tabStorage,
  locks,
}: {
  enabled: boolean;
  identity: WorkflowEditSessionIdentity | null;
  workflow: Record<string, unknown> | null | undefined;
  serverRevision: number | null;
  changeSet: WorkflowChangeSetController;
  store?: WorkflowEditSessionStore | null;
  tabStorage?: TabStorage;
  locks?: LockManagerLike;
}) {
  const store = useMemo(() => (injectedStore === undefined ? createWorkflowEditSessionStore() : injectedStore), [injectedStore]);
  const key = enabled ? identityKey(identity) : "";
  const [state, setState] = useState<{ key: string; status: WorkflowEditSessionStatus; problem: RecoveryProblem | null; sessionId: string }>(
    { key: "", status: "off", problem: null, sessionId: "" },
  );
  const [persistence, setPersistence] = useState<WorkflowEditSessionPersistence>("idle");
  const [formDrafts, setFormDrafts] = useState<{ key: string; drafts: Record<string, StoredFormDraft> }>({ key: "", drafts: {} });
  const sessionRef = useRef<ActiveSession | null>(null);
  const generationRef = useRef(0);
  const changeSetRef = useRef(changeSet);
  const workflowRef = useRef(workflow);
  const nextPathRef = useRef<string | null>(null);
  const discardOnLeaveRef = useRef(false);
  const dirtyRef = useRef(false);
  useLayoutEffect(() => {
    changeSetRef.current = changeSet;
    workflowRef.current = workflow;
  });
  const workflowId = identity?.workflowId ?? "";
  const workflowReady = Boolean(workflow && stringFromUnknownValue(workflow.workflow_id) === workflowId && serverRevision && serverRevision > 0);
  const status = state.key === key ? state.status : (key ? "loading" : "off");
  const drafts = formDrafts.key === key ? formDrafts.drafts : NO_DRAFTS;

  // Recover or start the session once the workflow and the change set are ready.
  useEffect(() => {
    if (!key || !identity || !workflowReady || !changeSet.ownerReady) return undefined;
    if (sessionRef.current?.key === key) return undefined;
    const generation = ++generationRef.current;
    let cancelled = false;
    setState({ key, status: "loading", problem: null, sessionId: "" });
    const start = async () => {
      let sessionId = readSessionReference(identity, tabStorage);
      let ownership: SessionOwnership | null = sessionId ? await claimSessionOwnership(sessionId, locks) : null;
      let raw: unknown = null;
      let readable = true;
      if (sessionId && store) {
        try { raw = await store.load(sessionId); } catch { readable = false; }
      }
      if (sessionId && ownership && !ownership.owned) {
        // A duplicated tab: its copy becomes its own session before the first write.
        sessionId = newEditSessionId();
        ownership = await claimSessionOwnership(sessionId, locks);
        if (raw && typeof raw === "object") raw = { ...(raw as Record<string, unknown>), sessionId };
      }
      sessionId ||= newEditSessionId();
      ownership ??= await claimSessionOwnership(sessionId, locks);
      if (cancelled || generation !== generationRef.current) {
        ownership.release();
        return;
      }
      writeSessionReference(identity, sessionId, tabStorage);
      const parsed = raw === null || raw === undefined ? null : parseStoredEditSession(raw, identity);
      const server = changeSetRef.current;
      const serverContent = contentWithDefinition(authoringContentOf(workflowRef.current), server.checkpoint.definition);
      const fresh: WorkflowServerCheckpoint = { revision: Number(server.checkpoint.revision ?? 0), content: serverContent };
      if (parsed?.status === "unavailable") {
        sessionRef.current = { key, identity, sessionId, origin: fresh, checkpoint: fresh, editSequence: 0, ownership, writes: Promise.resolve(), generation, lastJournal: undefined, lastDrafts: undefined, writeSequence: 0 };
        setState({ key, status: "recovery_unavailable", problem: parsed.reason, sessionId });
        setPersistence("unavailable");
        return;
      }
      if (parsed?.status === "ok" && sessionHasLocalWork(parsed.session)) {
        const recovered = parsed.session;
        sessionRef.current = {
          key, identity, sessionId, origin: recovered.origin, checkpoint: recovered.serverCheckpoint,
          editSequence: recovered.editSequence, ownership, writes: Promise.resolve(), generation,
          lastJournal: undefined, lastDrafts: undefined, writeSequence: 0,
        };
        server.restoreJournal({
          journal: recovered.history,
          checkpointRevision: recovered.serverCheckpoint.revision,
          checkpointDefinition: definitionOfContent(recovered.serverCheckpoint.content),
        });
        setFormDrafts({ key, drafts: recovered.formDrafts });
      } else {
        // Nothing to recover: the current server version is the new base.
        sessionRef.current = { key, identity, sessionId, origin: fresh, checkpoint: fresh, editSequence: 0, ownership, writes: Promise.resolve(), generation, lastJournal: undefined, lastDrafts: undefined, writeSequence: 0 };
      }
      setState({ key, status: "active", problem: null, sessionId });
      setPersistence(store && readable ? "idle" : "unavailable");
    };
    void start();
    return () => { cancelled = true; };
  }, [changeSet.ownerReady, identity, key, locks, store, tabStorage, workflowReady]);

  const contentDirty = changeSet.hasChanges || changeSet.checkpoint.held || changeSet.status === "applying";
  const draftsDirty = Object.keys(drafts).length > 0;
  dirtyRef.current = contentDirty || draftsDirty;

  // Write the session after every completed action, Undo, Redo, checkpoint or draft change.
  useEffect(() => {
    const session = sessionRef.current;
    if (session?.key !== key || status !== "active") return;
    const current = changeSetRef.current;
    if (!current.checkpoint.held && current.checkpoint.revision) {
      session.checkpoint = {
        revision: current.checkpoint.revision,
        content: contentWithDefinition(authoringContentOf(workflowRef.current), current.checkpoint.definition),
      };
    }
    if (session.lastJournal !== current.journal || session.lastDrafts !== drafts) {
      if (session.lastJournal !== undefined) session.editSequence += 1;
      session.lastJournal = current.journal;
      session.lastDrafts = drafts;
    }
    session.writeSequence += 1;
    const record = buildEditSession({
      sessionId: session.sessionId,
      identity: session.identity,
      editSequence: session.editSequence,
      origin: session.origin,
      serverCheckpoint: session.checkpoint,
      journal: current.journal,
      currentDefinition: current.preview,
      formDrafts: drafts,
    });
    if (!store) {
      setPersistence("unavailable");
      return;
    }
    const sequence = session.writeSequence;
    setPersistence("saving");
    // One write at a time, in order; a completed write counts, a pending one does not.
    session.writes = session.writes.catch(() => undefined).then(() => store.save(record));
    session.writes.then(
      () => { if (sessionRef.current === session && session.writeSequence === sequence) setPersistence("saved"); },
      () => { if (sessionRef.current === session) setPersistence("failed"); },
    );
  }, [changeSet.journal, changeSet.checkpoint.revision, changeSet.checkpoint.held, drafts, key, serverRevision, status, store]);

  const endSession = useCallback(async (session: ActiveSession | null) => {
    if (!session) return;
    if (sessionRef.current === session) sessionRef.current = null;
    clearSessionReference(session.identity, tabStorage);
    session.ownership?.release();
    try {
      await session.writes.catch(() => undefined);
      await store?.remove(session.sessionId);
    } catch {
      // A record that could not be removed is replaced by the next session of this tab.
    }
  }, [store, tabStorage]);

  // Leaving the workflow ends its session: normally, or after "Leave and discard".
  // A switch to this workflow's other page keeps it; a reload never runs this.
  useEffect(() => {
    if (!key) return undefined;
    return () => {
      const session = sessionRef.current;
      if (session?.key !== key) return;
      const next = nextPathRef.current;
      nextPathRef.current = null;
      const prefix = workflowPathPrefix(session.identity.workflowId);
      const staysOnWorkflow = Boolean(next && (next === prefix || next.startsWith(`${prefix}/`)));
      if (staysOnWorkflow || (dirtyRef.current && !discardOnLeaveRef.current)) {
        session.ownership?.release();
        sessionRef.current = null;
        return;
      }
      discardOnLeaveRef.current = false;
      void endSession(session);
    };
  }, [endSession, key]);

  /** The leave guard reports where a navigation goes, before the page unmounts. */
  const noteNavigation = useCallback((pathname: string) => { nextPathRef.current = pathname; }, []);
  /** "Leave and discard" was confirmed: the session ends when the page actually leaves. */
  const discardOnLeave = useCallback(() => { discardOnLeaveRef.current = true; }, []);

  /** Delete a record that cannot be recovered, and start a new session from the server. */
  const deleteRecovery = useCallback(async () => {
    const session = sessionRef.current;
    if (!session || state.status !== "recovery_unavailable") return;
    try {
      await store?.remove(session.sessionId);
    } catch {
      setPersistence("failed");
      return;
    }
    const sessionId = newEditSessionId();
    session.ownership?.release();
    const ownership = await claimSessionOwnership(sessionId, locks);
    writeSessionReference(session.identity, sessionId, tabStorage);
    sessionRef.current = { ...session, sessionId, ownership, editSequence: 0, writes: Promise.resolve(), lastJournal: undefined, lastDrafts: undefined, writeSequence: 0 };
    setState({ key: session.key, status: "active", problem: null, sessionId });
    setPersistence("idle");
  }, [locks, state.status, store, tabStorage]);

  /** Keep or clear one form's unsaved values; credential fields are kept only as "enter again". */
  const setFormDraft = useCallback((draftKey: string, value: unknown, sensitiveFields: readonly string[] = []) => {
    setFormDrafts((current) => {
      const base = current.key === key ? current.drafts : {};
      if (value === null || value === undefined) {
        if (!(draftKey in base)) return current.key === key ? current : { key, drafts: base };
        const next = { ...base };
        delete next[draftKey];
        return { key, drafts: next };
      }
      return { key, drafts: { ...base, [draftKey]: storableFormDraft(value, sensitiveFields) } };
    });
  }, [key]);

  const baseContent = useCallback((): WorkflowAuthoringContent | null => sessionRef.current?.checkpoint.content ?? null, []);
  const localContent = useCallback((): WorkflowAuthoringContent | null => {
    const session = sessionRef.current;
    return session ? contentWithDefinition(session.checkpoint.content, changeSetRef.current.preview) : null;
  }, []);

  return {
    status,
    problem: state.key === key ? state.problem : null,
    sessionId: state.key === key ? state.sessionId : "",
    persistence: key ? persistence : "idle",
    /** Unapplied content, a held copy, an Apply in flight, or unsaved form drafts. */
    dirty: contentDirty || draftsDirty,
    formDrafts: drafts,
    setFormDraft,
    baseContent,
    localContent,
    noteNavigation,
    discardOnLeave,
    deleteRecovery,
    editSequence: () => sessionRef.current?.editSequence ?? 0,
  };
}

export type WorkflowEditSessionController = ReturnType<typeof useWorkflowEditSession>;
