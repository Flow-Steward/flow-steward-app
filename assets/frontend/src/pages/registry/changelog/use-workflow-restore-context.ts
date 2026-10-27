import { useCallback, useEffect, useRef, useState } from "react";

import { fetchWorkflowCurrentRevision } from "@/pages/registry/changelog/api";

export type WorkflowRestoreStatus = "idle" | "loading" | "ready" | "error" | "conflict";

/**
 * What a reader sees when they open one workflow revision: which workflow and
 * entry, and the workflow revision read once when the view opened. A restore
 * from this view is guarded by exactly that revision.
 */
export type WorkflowRestoreContext = {
  workflowId: string;
  entryId: string;
  status: WorkflowRestoreStatus;
  /** A number only while the context is ready. */
  expectedRevision: number | null;
  /** Names one opening of the view; a reopen or another selection gets a new one. */
  generation: number;
};

const IDLE: WorkflowRestoreContext = {
  workflowId: "",
  entryId: "",
  status: "idle",
  expectedRevision: null,
  generation: 0,
};

/**
 * The revision baseline of an open workflow revision view.
 *
 * Opening the view reads the workflow's current revision once and keeps it:
 * background refetches never move it, and an answer that arrives after the view
 * closed or moved to another workflow or entry is dropped. A conflict ends the
 * context; the reader closes and reopens the view to restore against the new
 * revision. Other entity types have no context.
 */
export function useWorkflowRestoreContext({
  open,
  entityType,
  entityId,
  entryId,
}: Readonly<{ open: boolean; entityType: string; entityId: string; entryId: string }>) {
  const generationRef = useRef(0);
  const currentRef = useRef<WorkflowRestoreContext>(IDLE);
  const [context, setContext] = useState<WorkflowRestoreContext>(IDLE);
  const applyContext = useCallback((next: WorkflowRestoreContext) => {
    currentRef.current = next;
    setContext(next);
  }, []);
  const active = open && entityType === "workflow" && Boolean(entityId) && Boolean(entryId);

  useEffect(() => {
    generationRef.current += 1;
    const generation = generationRef.current;
    if (!active) {
      applyContext({ ...IDLE, generation });
      return undefined;
    }
    const opened = { workflowId: entityId, entryId, generation };
    applyContext({ ...opened, status: "loading", expectedRevision: null });
    let live = true;
    const settle = (next: WorkflowRestoreContext) => {
      if (live && generationRef.current === generation) {
        applyContext(next);
      }
    };
    fetchWorkflowCurrentRevision(entityId).then(
      (revision) => settle({ ...opened, status: "ready", expectedRevision: revision }),
      () => settle({ ...opened, status: "error", expectedRevision: null }),
    );
    return () => {
      live = false;
    };
  }, [active, entityId, entryId, applyContext]);

  /** Whether ``captured`` is still the open, ready context with the same baseline. */
  const isCurrent = useCallback((captured: WorkflowRestoreContext) => {
    const current = currentRef.current;
    return (
      current.status === "ready"
      && captured.status === "ready"
      && current.generation === captured.generation
      && current.workflowId === captured.workflowId
      && current.entryId === captured.entryId
      && current.expectedRevision === captured.expectedRevision
    );
  }, []);

  /** The server refused ``captured``'s revision: no restore until the view is reopened. */
  const markConflict = useCallback((captured: WorkflowRestoreContext) => {
    const current = currentRef.current;
    if (current.generation !== captured.generation) {
      return;
    }
    applyContext({ ...current, status: "conflict", expectedRevision: null });
  }, [applyContext]);

  return { context, isCurrent, markConflict };
}
