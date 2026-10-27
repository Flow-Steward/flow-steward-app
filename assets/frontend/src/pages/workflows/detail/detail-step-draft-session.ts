import { useEffect, useRef, useState } from "react";

import type { WorkflowEditSessionController } from "@/pages/workflows/detail/use-workflow-edit-session";

/**
 * The step editor's unsaved values in the edit session (Task 5): kept after
 * every change while the form differs from the saved step, restored after a
 * reload by opening the same step's editor with them.
 *
 * The parameters are stored as parsed JSON, so credential-like fields are left
 * out by name. When something had to be left out, the restored editor keeps the
 * saved parameters instead of a copy without them, so a later Save cannot clear
 * a saved credential; the author enters the change again.
 */

export const STEP_DRAFT_KEY = "step";

export type StepEditorValues = {
  stepId: string;
  displayName: string;
  kind: string;
  overrideDraft: string;
  agentId: string;
  optional: boolean;
  blockKey: string;
};

type StoredStepDraft = {
  stepId: string;
  displayName: string;
  kind: string;
  override?: unknown;
  agentId: string;
  optional: boolean;
  blockKey: string;
};

export type StepDraftRestore = {
  /** Values to show once the step's editor is open; `overrideDraft` is null when it must be entered again. */
  values: Omit<StepEditorValues, "overrideDraft"> & { overrideDraft: string | null };
  reentry: string[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parsedOverride(text: string): { ok: true; value: unknown } | { ok: false } {
  try {
    return { ok: true, value: text.trim() ? JSON.parse(text) : {} };
  } catch {
    return { ok: false };
  }
}

/** The stored draft as restorable values, or null when it is not a step draft. */
export function stepDraftRestore(stored: { value: unknown; reentry: string[] } | undefined): StepDraftRestore | null {
  if (!stored || !isRecord(stored.value) || typeof stored.value.stepId !== "string" || !stored.value.stepId) return null;
  const draft = stored.value as Partial<StoredStepDraft>;
  const overrideLeftOut = stored.reentry.some((path) => path === "override" || path.startsWith("override.") || path.startsWith("override["));
  const hasOverride = "override" in draft && !overrideLeftOut;
  return {
    values: {
      stepId: String(draft.stepId),
      displayName: typeof draft.displayName === "string" ? draft.displayName : "",
      kind: typeof draft.kind === "string" ? draft.kind : "",
      overrideDraft: hasOverride ? JSON.stringify(draft.override ?? {}, null, 2) : null,
      agentId: typeof draft.agentId === "string" ? draft.agentId : "",
      optional: Boolean(draft.optional),
      blockKey: typeof draft.blockKey === "string" ? draft.blockKey : "",
    },
    reentry: stored.reentry,
  };
}

export function useStepDraftSession({
  session,
  enabled,
  values,
  unsaved,
  stepExists,
  openStep,
  apply,
}: {
  session: Pick<WorkflowEditSessionController, "status" | "formDrafts" | "setFormDraft">;
  /** The author's own saved step is in the editor (not a new local step, not a hook member). */
  enabled: boolean;
  values: StepEditorValues;
  unsaved: boolean;
  stepExists: (stepId: string) => boolean;
  openStep: (stepId: string) => Promise<boolean>;
  apply: (restore: StepDraftRestore) => void;
}) {
  // Sticky: set once the recovered draft was handled, so later drafts are not reopened.
  const done = useRef(false);
  const [restored, setRestored] = useState(false);
  const restoring = useRef(false);
  const openStepRef = useRef(openStep);
  const applyRef = useRef(apply);
  useEffect(() => {
    openStepRef.current = openStep;
    applyRef.current = apply;
  });

  // Once per session activation: reopen the editor with the recovered values.
  useEffect(() => {
    if (done.current || restoring.current || session.status !== "active") return;
    const restore = stepDraftRestore(session.formDrafts[STEP_DRAFT_KEY]);
    if (!restore) {
      done.current = true;
      return;
    }
    if (!stepExists(restore.values.stepId)) return;
    restoring.current = true;
    void openStepRef.current(restore.values.stepId).then((opened) => {
      if (opened) applyRef.current(restore);
    }).finally(() => {
      restoring.current = false;
      done.current = true;
      setRestored(true);
    });
  }, [session.formDrafts, session.status, stepExists]);

  // After the restore: keep the editor's values while they differ from the saved step.
  const { setFormDraft } = session;
  useEffect(() => {
    if (!done.current || session.status !== "active") return;
    if (!enabled || !unsaved || !values.stepId) {
      setFormDraft(STEP_DRAFT_KEY, null);
      return;
    }
    const override = parsedOverride(values.overrideDraft);
    const draft: StoredStepDraft = {
      stepId: values.stepId,
      displayName: values.displayName,
      kind: values.kind,
      agentId: values.agentId,
      optional: values.optional,
      blockKey: values.blockKey,
      override: override.ok ? override.value : values.overrideDraft,
    };
    // Text that is not JSON yet cannot be checked for credentials: it is left out.
    setFormDraft(STEP_DRAFT_KEY, draft, override.ok ? [] : ["override"]);
  }, [enabled, restored, session.status, setFormDraft, unsaved, values.agentId, values.blockKey, values.displayName, values.kind, values.optional, values.overrideDraft, values.stepId]);

  return { restored };
}
