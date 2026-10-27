import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type SetStateAction } from "react";

import { useMutation } from "@tanstack/react-query";

import {
  EXPLICIT_EMPTY_TEST_VALUE,
  inputIsText,
  inputSupportsBoolean,
  inputSupportsJson,
  inputSupportsNumber,
  objectOrEmpty,
  mergeStepTestRunState,
  testRunBannerMessage,
  testRunInputBindingsFromRows,
  testRunPayloadFromInputs,
  textOrEmpty,
  waitForWorkflowTestRunState,
  workflowTestRunIsPending,
} from "@/pages/workflows/detail/detail-helpers";
import { isHttpError } from "@/lib/http/errors";
import type { TestRunSubmissionProof } from "./detail-selected-test-run";
import { isWorkflowRevisionConflict } from "@/pages/workflows/builder/api";
import {
  fetchWorkflowTestRunState,
  submitWorkflowTestRunApproval,
  testRunWorkflow,
  testRunWorkflowStep,
  type StepTestRunResult,
  type WorkflowTestRunResult,
} from "@/pages/workflows/shared/simulate-api";
import {
  firstStringFromUnknownValue,
  isTruthy,
  stringFromUnknownValue,
} from "@/lib/utils/unknown-values";

/**
 * One Test Run input row, derived from the workflow's public contract.
 *
 * A field with a configured default is fixed for the revision: the backend
 * answers `workflow.input.override_forbidden` for any value a caller sends, so
 * an editable box here asks the operator to type something guaranteed to be
 * rejected. It is shown as fixed instead, and stops counting as required —
 * there is nothing for them to supply.
 */
export function workflowTestRunInputRow(source: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t) {
  const name = textOrEmpty(source.name);
  const type = textOrEmpty(source.type) || "string";
  const label = textOrEmpty(source.label) || name;
  const description = textOrEmpty(source.description);
  if (type === "file") {
    // A test run has no upload of its own: it uses the input's test file, so
    // the row shows which file that is, or where to attach one.
    const sample = objectOrEmpty(source.sample_file);
    const filename = textOrEmpty(sample.filename);
    const options: string[] = [];
    return {
      name,
      label,
      description,
      type,
      required: Boolean(source.required) && !filename,
      fixed: true,
      testFile: Boolean(filename),
      defaultPreview: filename
        ? t("react.workflows.display_test_file_name", "Test file: {name}", { name: filename })
        : t("react.workflows.display_attach_test_file", "No test file. Attach one to this input in the workflow settings."),
      options,
    };
  }
  const configured = Boolean(source.default_configured);
  const isSecret = type === "secret";
  let defaultPreview = "";
  if (configured) {
    // A configured secret has no value to preview, only a state.
    if (isSecret) {
      defaultPreview = t("react.workflows.display_configured", "configured");
    } else if (typeof source.default === "object" && source.default !== null) {
      defaultPreview = JSON.stringify(source.default) ?? "";
    } else {
      defaultPreview = stringFromUnknownValue(source.default);
    }
  }
  return {
    name,
    label,
    description,
    type,
    required: Boolean(source.required) && !configured,
    fixed: configured,
    defaultPreview,
    options: Array.isArray(source.options)
      ? source.options.map((value) => textOrEmpty(value)).filter(isTruthy)
      : [],
  };
}


type WorkflowTestRunInput = ReturnType<typeof workflowTestRunInputRow>;
type WorkflowTestRunServerError = { message: string; value: string };
type AcceptedTestRunInputs = Readonly<{
  jobId: string; workflowId: string; projectId: string; summary: string;
  inputs: Readonly<Record<string, string>>; contract: string;
}>;
type TestRunLaunchIntent = void | { reuse: AcceptedTestRunInputs; owner: WorkflowTestRunSubmissionProof; isCurrentSelection: () => boolean };
export type WorkflowTestRunAgainOutcome = "admitted" | "needs-inputs" | "blocked";

/** Only typed public values admitted by this controller can be reused. */
function reusableInputContract(rows: readonly WorkflowTestRunInput[]): string | null {
  const publicTextTypes = new Set(["string", "text", "paragraph", "multiline", "textarea", "markdown", "select", "url", "email"]);
  if (rows.some((row) => !row.fixed && !publicTextTypes.has(row.type.toLowerCase()) &&
    !inputSupportsNumber(row.type) && !inputSupportsBoolean(row.type) && !inputSupportsJson(row.type))) return null;
  return JSON.stringify(rows.map((row) => [row.name, row.type, row.required, row.fixed, row.options]));
}

export type WorkflowTestRunSubmissionProof = Readonly<TestRunSubmissionProof>;
export type WorkflowTestRunSubmissionBridge = Readonly<{
  captureSubmissionOwner: () => WorkflowTestRunSubmissionProof | null;
  isCurrent: (captured: WorkflowTestRunSubmissionProof) => boolean;
  acceptAcknowledgement: (payload: WorkflowTestRunResult, captured: WorkflowTestRunSubmissionProof) => boolean;
}>;
type RunProof = {
  owner: WorkflowTestRunSubmissionProof | null; stamp: string; generation: number;
  threadId: string; jobId: string; workflowId: string;
};
type RunSession = {
  stamp: string; payload: WorkflowTestRunResult | null; run: RunProof | null; message: string;
  touched: Record<string, boolean>; errors: Record<string, WorkflowTestRunServerError>;
};
function emptyRunSession(stamp: string): RunSession {
  return { stamp, payload: null, run: null, message: "", touched: {}, errors: {} };
}
function submissionStamp(owner: WorkflowTestRunSubmissionProof | null): string {
  return owner ? JSON.stringify([owner.workflowId, owner.projectId, owner.editorRevision, owner.ownerEpoch]) : "";
}
type SubmissionContext = {
  token: symbol; owner: WorkflowTestRunSubmissionProof | null; stamp: string;
  bridge: WorkflowTestRunSubmissionBridge | undefined;
  request: Parameters<typeof testRunWorkflow>[0]; summary: string; inputs: Record<string, string>;
  rows: WorkflowTestRunInput[]; errors: Record<string, string>; blocked: string; contractReady: boolean;
  jobStorageKey: string; threadStorageKey: string;
  onSubmitted?: (payload: WorkflowTestRunResult) => void; onConflict?: () => void;
  reusedJobId?: string;
  isCurrentSelection?: () => boolean;
};
type IsolatedStepAuthority = Readonly<{ key: string; epoch: number }>;
type IsolatedStepRequest = Parameters<typeof testRunWorkflowStep>[0];
type IsolatedStepSubmission = {
  authority: IsolatedStepAuthority; token: symbol; request: IsolatedStepRequest;
  blocked: string; local: boolean; onConflict?: () => void;
};
type IsolatedStepSession = {
  context: IsolatedStepSubmission; payload: StepTestRunResult | null; message: string;
};
type IsolatedStepDraft = {
  authority: IsolatedStepAuthority; stepId: string; summary: string;
  inputs: Record<string, string>; types: Record<string, string>;
};


type ApprovalContext = {
  token: symbol; run: RunProof | null; threadId: string; approvalId: string; comment: string; approved: boolean;
};

function payloadMatchesRun(payload: WorkflowTestRunResult, run: RunProof): boolean {
  return !run.owner || (
    textOrEmpty(payload.workflow_id) === run.owner.workflowId && textOrEmpty(payload.project_id) === run.owner.projectId &&
    (!textOrEmpty(payload.thread_id) || textOrEmpty(payload.thread_id) === run.threadId) &&
    (!run.jobId || !textOrEmpty(payload.job_id) || textOrEmpty(payload.job_id) === run.jobId)
  );
}

/** Validate the declared launch fields without changing their raw editing drafts. */
function workflowTestRunInputError(row: WorkflowTestRunInput, raw: string, t: I18nContextValue["t"]): string {
  if (row.fixed) return row.required && row.type === "file" ? row.defaultPreview : "";
  const text = raw.trim();
  if (!text) return row.required ? t("extensions.repeater.field_required", "{field} is required.", { field: row.label }) : "";
  let requirement = "";
  if (inputSupportsNumber(row.type) && !Number.isFinite(Number(text))) {
    requirement = t("react.workflows.display_valid_number", "a valid number");
  } else if (inputSupportsBoolean(row.type) && !["true", "false", "1", "0", "yes", "no", "on", "off"].includes(text.toLowerCase())) {
    requirement = t("react.workflows.boolean", "boolean");
  } else if (row.type === "select" && row.options.length && !row.options.includes(text)) {
    requirement = t("react.workflows.display_declared_option", "a declared option");
  } else if (inputSupportsJson(row.type)) {
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      return t("react.workflows.value0_must_be_valid_json", "{value0} must be valid JSON.", { value0: row.label });
    }
    // The API also accepts one string-encoded object/array after the serializer's
    // parse. Match that coercion without broadening it to primitive JSON/null.
    const encoded = typeof value === "string";
    if (encoded) {
      try { value = JSON.parse(value as string); } catch { value = null; }
    }
    const object = value !== null && typeof value === "object" && !Array.isArray(value);
    if (row.type === "array" ? !Array.isArray(value) : !object || (encoded && !Object.keys(value as object).length)) {
      requirement = row.type === "array"
        ? t("react.workflows.display_valid_array", "a valid array")
        : t("react.workflows.display_valid_object", "a valid object");
    }
  }
  return requirement ? t("react.workflows.test_run_input_must_be_value1", "'{value0}' must be {value1}.", { value0: row.label, value1: requirement }) : "";
}

function usePendingTestRunPolling(
  threadId: string,
  onState: (state: WorkflowTestRunResult) => void,
) {
  useEffect(() => {
    if (!threadId) {
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof globalThis.setTimeout> | undefined;
    const poll = async () => {
      try {
        const latest = await fetchWorkflowTestRunState(
          threadId,
          { presentation: "background" },
        );
        if (cancelled) {
          return;
        }
        onState(latest);
        if (workflowTestRunIsPending(latest)) {
          timer = globalThis.setTimeout(poll, 1500);
        }
      } catch {
        if (!cancelled) {
          timer = globalThis.setTimeout(poll, 1500);
        }
      }
    };
    timer = globalThis.setTimeout(poll, 1500);
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        globalThis.clearTimeout(timer);
      }
    };
  }, [threadId, onState]);
}

export function useWorkflowTestRunController(ctx: Record<string, any> & {
  submissionOwner?: WorkflowTestRunSubmissionBridge | null;
}) {
  const { t } = useI18n();
  const {
    onRevisionConflict,
    onWorkflowTestRunSubmitted,
    selectedProjectId,
    selectedStep,
    serverRevision,
    stepContractPreviewQuery,
    stepTestRunBlockedReason,
    workflowContractQuery,
    workflowId,
    workflowTestRunBlockedReason,
    launchLock,
    submissionOwner,
    stepTestRunScope,
  } = ctx;
  const conflictMessage = t(
    "react.workflows.test_run_revision_conflict",
    "The workflow changed after it was loaded. No test run was started; reload the workflow and run it again.",
  );

  const [workflowTestRunSummary, setWorkflowTestRunSummary] = useState(() =>
    t("react.workflows.test_run_initial_summary", "Run this workflow from the builder test surface."),
  );
  const controlled = submissionOwner !== undefined;
  const normalizedWorkflowId = textOrEmpty(workflowId);
  const normalizedProjectId = textOrEmpty(selectedProjectId);
  const normalizedRevision = typeof serverRevision === "number" ? serverRevision : null;
  const acceptedInputs = useRef<AcceptedTestRunInputs | null>(null);
  const againAdmission = useRef<symbol | null>(null);
  const mounted = useRef(false);
  const liveOwner = useRef({ controlled, bridge: submissionOwner, workflowId: normalizedWorkflowId,
    projectId: normalizedProjectId, revision: normalizedRevision });
  const [registeredOwner, setRegisteredOwner] = useState<WorkflowTestRunSubmissionProof | null>(null);
  const registration = useRef<WorkflowTestRunSubmissionProof | null>(null);
  useLayoutEffect(() => {
    if (liveOwner.current.workflowId !== normalizedWorkflowId || liveOwner.current.projectId !== normalizedProjectId) acceptedInputs.current = null;
    liveOwner.current = { controlled, bridge: submissionOwner, workflowId: normalizedWorkflowId,
      projectId: normalizedProjectId, revision: normalizedRevision };
  });
  useLayoutEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; acceptedInputs.current = null; };
  }, []);
  /** Call after the future parent's committed bridge registration, including
   * owner/readiness transitions. Equal stamps do not schedule another render. */
  const refreshWorkflowTestRunSubmissionOwner = useCallback(() => {
    if (!mounted.current) return;
    const current = liveOwner.current;
    const candidate = current.controlled ? current.bridge?.captureSubmissionOwner() ?? null : null;
    const proof = candidate && candidate.workflowId === current.workflowId && candidate.projectId === current.projectId &&
      candidate.editorRevision === current.revision && Number.isSafeInteger(candidate.ownerEpoch) && candidate.ownerEpoch > 0 &&
      current.bridge?.isCurrent(candidate) ? { ...candidate } : null;
    if (submissionStamp(registration.current) === submissionStamp(proof)) return;
    registration.current = proof;
    setRegisteredOwner(proof);
  }, []);
  const ownerCurrent = useCallback((owner: WorkflowTestRunSubmissionProof | null) => {
    if (!mounted.current) return false;
    const current = liveOwner.current;
    if (!owner) return !current.controlled;
    return current.controlled && owner.workflowId === current.workflowId && owner.projectId === current.projectId &&
      owner.editorRevision === current.revision && submissionStamp(registration.current) === submissionStamp(owner) &&
      Boolean(current.bridge?.isCurrent(owner));
  }, []);
  const ownerStamp = !controlled ? "legacy" : registeredOwner?.workflowId === normalizedWorkflowId &&
    registeredOwner.projectId === normalizedProjectId && registeredOwner.editorRevision === normalizedRevision &&
    submissionOwner?.isCurrent(registeredOwner) ? submissionStamp(registeredOwner) : "";
  const [runSession, setRunSession] = useState<RunSession>(() => emptyRunSession(controlled ? "" : "legacy"));
  const session = runSession.stamp === ownerStamp ? runSession : emptyRunSession(ownerStamp);
  const workflowTestRunPayload = session.payload;
  const workflowTestRunMessage = session.message;
  const workflowTestRunTouched = session.touched;
  const workflowTestRunServerErrors = session.errors;
  const updateRunSession = useCallback((stamp: string, current: () => boolean, update: (previous: RunSession) => RunSession) => {
    setRunSession((previous) => {
      // This live predicate read deliberately fences queued updates. It performs
      // no setter, ref mutation or storage side effect during updater replay.
      if (!current()) return previous;
      return update(previous.stamp === stamp ? previous : emptyRunSession(stamp));
    });
  }, []);
  const setWorkflowTestRunMessage = (value: SetStateAction<string>) => {
    const owner = controlled ? registeredOwner : null;
    updateRunSession(ownerStamp, () => Boolean(ownerStamp) && ownerCurrent(owner), (previous) => ({
      ...previous, message: typeof value === "function" ? value(previous.message) : value,
    }));
  };
  const [workflowTestRunApprovalComment, setWorkflowTestRunApprovalComment] = useState("");
  const activeSubmission = useRef<SubmissionContext | null>(null);
  const latestSubmission = useRef<symbol | null>(null);
  const activeApproval = useRef<ApprovalContext | null>(null);
  const latestApproval = useRef<symbol | null>(null);
  const runGeneration = useRef(0);
  const restoreGeneration = useRef(0);
  const [submissionSettlement, setSubmissionSettlement] = useState(0);
  const currentRun = useRef<RunProof | null>(null);
  const submissionCurrent = useCallback((context: SubmissionContext | undefined) => Boolean(context &&
    latestSubmission.current === context.token && ownerCurrent(context.owner)), [ownerCurrent]);
  const runCurrent = useCallback((run: RunProof | null | undefined) => Boolean(run && currentRun.current === run &&
    ownerCurrent(run.owner)), [ownerCurrent]);

  const latestJobStorageKey = `fs.workflow-run-job.${workflowId || "unknown"}`;
  const [workflowTestRunJobId, setWorkflowTestRunJobId] = useState(() => {
    try { return globalThis.localStorage?.getItem(latestJobStorageKey) ?? ""; } catch { return ""; }
  });
  const pendingRunStorageKey = `fs.workflow-test-run.${workflowId || "unknown"}`;
  const [pendingRunRestoreSettled, setPendingRunRestoreSettled] = useState(false);
  const persistRun = (run: RunProof, previous: RunProof | null, key: string) => {
    if (!runCurrent(run)) return;
    try {
      if (run.threadId) globalThis.localStorage?.setItem(key, run.threadId);
      else if (previous?.threadId && previous.stamp === run.stamp && globalThis.localStorage?.getItem(key) === previous.threadId)
        globalThis.localStorage?.removeItem(key);
    } catch { /* storage unavailable */ }
  };

  // Legacy restoration behavior remains opt-out. Controlled restoration has an
  // explicit registered-owner state dependency, not an invisible ref trigger.
  useEffect(() => {
    if (controlled || workflowTestRunPayload || !workflowId) return;
    let cancelled = false;
    let remembered = "";
    try { remembered = globalThis.localStorage?.getItem(pendingRunStorageKey) ?? ""; } catch { /* unavailable */ }
    if (!remembered) { setPendingRunRestoreSettled(true); return; }
    void (async () => {
      try {
        const restored = await fetchWorkflowTestRunState(remembered);
        if (!cancelled && restored) {
          const run: RunProof = { owner: null, stamp: "legacy", generation: ++runGeneration.current,
            threadId: textOrEmpty(restored.thread_id), jobId: textOrEmpty(restored.job_id), workflowId: textOrEmpty(workflowId) };
          currentRun.current = run;
          updateRunSession("legacy", () => !cancelled && ownerCurrent(null), (previous) => ({ ...previous,
            payload: restored, run, message: testRunBannerMessage("workflow", restored, t) }));
        }
      } catch {
        try { globalThis.localStorage?.removeItem(pendingRunStorageKey); } catch { /* unavailable */ }
      } finally { if (!cancelled) setPendingRunRestoreSettled(true); }
    })();
    return () => { cancelled = true; };
    // The legacy path retains its workflow-scoped restore lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [controlled, workflowId]);
  useEffect(() => {
    if (controlled || !workflowId || !pendingRunRestoreSettled) return;
    const threadId = textOrEmpty(workflowTestRunPayload?.thread_id);
    try {
      if (threadId) globalThis.localStorage?.setItem(pendingRunStorageKey, threadId);
      else globalThis.localStorage?.removeItem(pendingRunStorageKey);
    } catch { /* storage unavailable */ }
  }, [controlled, pendingRunRestoreSettled, pendingRunStorageKey, workflowId, workflowTestRunPayload]);
  const restoreAttempt = useRef("");
  useEffect(() => {
    if (!controlled || !registeredOwner || !ownerStamp || !ownerCurrent(registeredOwner)) return;
    // An explicit refresh transition supplies this deterministic effect trigger.
    const stamp = submissionStamp(registeredOwner);
    if (restoreAttempt.current === stamp || activeSubmission.current) return;
    restoreAttempt.current = stamp;
    if (currentRun.current?.stamp === stamp) return;
    const generation = restoreGeneration.current;
    let cancelled = false;
    let settled = false;
    let remembered = "";
    try { remembered = globalThis.localStorage?.getItem(pendingRunStorageKey) ?? ""; } catch { /* unavailable */ }
    if (!remembered) return;
    void (async () => {
      try {
        const restored = await fetchWorkflowTestRunState(remembered);
        const run: RunProof = { owner: registeredOwner, stamp, generation: runGeneration.current + 1,
          threadId: remembered, jobId: textOrEmpty(restored.job_id), workflowId: registeredOwner.workflowId };
        if (cancelled || restoreGeneration.current !== generation || !ownerCurrent(registeredOwner) ||
          !payloadMatchesRun(restored, run)) return;
        runGeneration.current = run.generation;
        currentRun.current = run;
        updateRunSession(stamp, () => !cancelled && restoreGeneration.current === generation && runCurrent(run), (previous) => ({
          ...previous, payload: restored, run, message: testRunBannerMessage("workflow", restored, t),
        }));
        // A restore is not a new ACK: no job pointer, acceptance, Submitted or
        // storage write/removal. In particular a foreign pointer stays untouched.
      } catch { /* an unverified workflow-only pointer is never deleted here */ }
      finally { settled = true; }
    })();
    return () => {
      cancelled = true;
      // StrictMode effect replay must be able to start the cancelled attempt.
      if (!settled && restoreAttempt.current === stamp) restoreAttempt.current = "";
    };
  }, [controlled, ownerCurrent, ownerStamp, pendingRunStorageKey, registeredOwner, runCurrent, submissionSettlement, t, updateRunSession]);
  const pendingWorkflowTestRunThreadId = workflowTestRunIsPending(workflowTestRunPayload)
    ? textOrEmpty(workflowTestRunPayload?.thread_id) : "";
  const polledRun = session.run;
  const receiveWorkflowTestRunState = useCallback((latest: WorkflowTestRunResult) => {
    if (controlled) {
      if (!runCurrent(polledRun) || !polledRun || !payloadMatchesRun(latest, polledRun)) return;
      updateRunSession(polledRun.stamp, () => runCurrent(polledRun), (previous) => ({ ...previous,
        payload: latest, message: testRunBannerMessage("workflow", latest, t) }));
    } else {
      updateRunSession("legacy", () => ownerCurrent(null), (previous) => ({ ...previous,
        payload: latest, message: testRunBannerMessage("workflow", latest, t) }));
    }
  }, [controlled, ownerCurrent, polledRun, runCurrent, t, updateRunSession]);
  usePendingTestRunPolling(pendingWorkflowTestRunThreadId, receiveWorkflowTestRunState);
  const [workflowTestRunInputs, setWorkflowTestRunInputs] = useState<
    Record<string, string>
  >({});
  const touchWorkflowTestRunField = (path: string) => updateRunSession(ownerStamp,
    () => Boolean(ownerStamp) && ownerCurrent(controlled ? registeredOwner : null),
    (previous) => ({ ...previous, touched: { ...previous.touched, [path]: true } }));
  const clearWorkflowTestRunServerError = (path: string) => updateRunSession(ownerStamp,
    () => Boolean(ownerStamp) && ownerCurrent(controlled ? registeredOwner : null), (previous) => {
      if (!previous.errors[path]) return previous;
      const errors = { ...previous.errors };
      delete errors[path];
      return { ...previous, errors };
    });
  const updateWorkflowTestRunInput = (name: string, value: string) => {
    setWorkflowTestRunInputs((current) => ({ ...current, [name]: value }));
    clearWorkflowTestRunServerError(`inputs.${name}`);
  };
  const updateWorkflowTestRunSummary = (value: string) => {
    setWorkflowTestRunSummary(value);
    clearWorkflowTestRunServerError("task_summary");
  };
  const initialStepSummary = "Run this step from the builder test surface.";
  const stepTestRunInputRows = useMemo(() => {
    let rows = [
      ...(stepContractPreviewQuery.data?.locked_required || []),
      ...(stepContractPreviewQuery.data?.unresolved_required || []),
      ...(stepContractPreviewQuery.data?.optional || []),
      ...(stepContractPreviewQuery.data?.custom || []),
    ];
    if (!rows.length && selectedStep?.stepKind === "if") {
      const config = objectOrEmpty(
        objectOrEmpty(objectOrEmpty(selectedStep.raw).override).if,
      );
      const condition = objectOrEmpty(config.condition);
      const selector = textOrEmpty(condition.selector);
      if (selector) {
        rows = [
          {
            name: "selector",
            required: true,
            resolved: false,
            source: selector,
            type: "text",
          },
        ];
      }
    }
    if (!rows.length && selectedStep?.stepKind === "switch") {
      const config = objectOrEmpty(
        objectOrEmpty(objectOrEmpty(selectedStep.raw).override).switch,
      );
      const selector = textOrEmpty(config.selector);
      if (selector) {
        rows = [
          {
            name: "selector",
            required: true,
            resolved: false,
            source: selector,
            type: "text",
          },
        ];
      }
    }
    return rows
      .map((item) => {
        const row = objectOrEmpty(item);
        return {
          name: textOrEmpty(row.name),
          requiredByStep: row.required !== false,
          // An optional input stays optional even when nothing resolves it yet.
          required: row.required !== false && !row.resolved,
          type: textOrEmpty(row.type) || "string",
          source: textOrEmpty(row.source),
          options: Array.isArray(row.options)
            ? row.options.map((value) => textOrEmpty(value)).filter(isTruthy)
            : [],
        };
      })
      .filter((item) => Boolean(item.name) && item.name !== "project_id"
        && item.source !== "literal" && !item.source.startsWith("literal:"));
  }, [selectedStep, stepContractPreviewQuery.data]);
  const currentStepTestRunKey = textOrEmpty(selectedStep?.runtimeId);
  const stepScopeKey = JSON.stringify([normalizedWorkflowId, normalizedProjectId, normalizedRevision, textOrEmpty(stepTestRunScope)]);
  const stepScopeEpoch = useRef(0);
  const committedStepAuthority = useRef<IsolatedStepAuthority | null>(null);
  const [stepAuthority, setStepAuthority] = useState<IsolatedStepAuthority | null>(null);
  const [stepDraft, setStepDraft] = useState<IsolatedStepDraft | null>(null);
  const [stepSession, setStepSession] = useState<IsolatedStepSession | null>(null);
  const committedStepSelection = useRef("");
  const activeStepSubmission = useRef<IsolatedStepSubmission | null>(null);
  const latestStepSubmission = useRef<symbol | null>(null);
  useLayoutEffect(() => {
    const authority = { key: stepScopeKey, epoch: ++stepScopeEpoch.current };
    committedStepAuthority.current = authority;
    latestStepSubmission.current = null;
    setStepAuthority(authority);
    setStepSession(null);
    setStepDraft(null);
    return () => { if (committedStepAuthority.current === authority) committedStepAuthority.current = null; };
  }, [stepScopeKey]);
  const stepAuthorityCurrent = useCallback((authority: IsolatedStepAuthority | null | undefined) =>
    Boolean(authority && mounted.current && committedStepAuthority.current === authority), []);
  const stepSubmissionCurrent = useCallback((context: IsolatedStepSubmission | null | undefined) =>
    Boolean(context && stepAuthorityCurrent(context.authority) && latestStepSubmission.current === context.token), [stepAuthorityCurrent]);
  useLayoutEffect(() => {
    committedStepSelection.current = currentStepTestRunKey;
    const authority = committedStepAuthority.current;
    if (!authority) return;
    setStepDraft((previous) => {
      if (!stepAuthorityCurrent(authority)) return previous;
      const own = previous && previous.authority === authority && previous.stepId === currentStepTestRunKey ? previous : null;
      const inputs = Object.fromEntries(stepTestRunInputRows.map((row) => {
        const value = own && own.types[row.name] === row.type ? own.inputs[row.name] || "" : "";
        return [row.name, value === EXPLICIT_EMPTY_TEST_VALUE && !inputIsText(row.type) ? "" : value];
      }));
      return { authority, stepId: currentStepTestRunKey, summary: own?.summary ?? initialStepSummary, inputs,
        types: Object.fromEntries(stepTestRunInputRows.map((row) => [row.name, row.type])) };
    });
  }, [currentStepTestRunKey, stepScopeKey, stepTestRunInputRows, stepAuthorityCurrent]);
  const ownStepDraft = stepDraft && stepAuthority?.key === stepScopeKey &&
    stepDraft.authority === stepAuthority && stepDraft.stepId === currentStepTestRunKey ? stepDraft : null;
  const stepTestRunSummary = ownStepDraft?.summary ?? initialStepSummary;
  const stepTestRunInputs = ownStepDraft?.inputs ?? Object.fromEntries(stepTestRunInputRows.map((row) => [row.name, ""]));
  const updateStepDraft = (update: (current: IsolatedStepDraft) => IsolatedStepDraft) => {
    const authority = stepAuthority;
    if (!stepAuthorityCurrent(authority)) return;
    setStepDraft((current) => current && current.authority === authority && current.stepId === currentStepTestRunKey &&
      committedStepSelection.current === currentStepTestRunKey && stepAuthorityCurrent(authority) ? update(current) : current);
  };
  const setStepTestRunSummary = (value: SetStateAction<string>) => updateStepDraft((current) => ({ ...current,
    summary: typeof value === "function" ? value(current.summary) : value }));
  const setStepTestRunInputs = (value: SetStateAction<Record<string, string>>) => updateStepDraft((current) => ({ ...current,
    inputs: typeof value === "function" ? value(current.inputs) : value }));
  const committedStepRequest = useRef<Omit<IsolatedStepSubmission, "token"> | null>(null);
  useLayoutEffect(() => {
    const authority = committedStepAuthority.current;
    committedStepRequest.current = authority ? { authority, blocked: textOrEmpty(stepTestRunBlockedReason),
      local: Boolean(selectedStep?.local), onConflict: onRevisionConflict,
      request: { workflowId: normalizedWorkflowId, expectedRevision: serverRevision, stepId: currentStepTestRunKey,
        projectId: normalizedProjectId.trim(), taskSummary: stepTestRunSummary.trim(),
        inputs: testRunPayloadFromInputs(stepTestRunInputRows, stepTestRunInputs),
        inputBindings: testRunInputBindingsFromRows(stepTestRunInputRows, stepTestRunInputs) } } : null;
  });
  const stepRunContext = stepSession?.context ?? null;
  const pendingStepTestRunThreadId = stepSubmissionCurrent(stepRunContext) &&
    workflowTestRunIsPending(stepSession?.payload) ? textOrEmpty(stepSession?.payload?.thread_id) : "";
  const receiveStepTestRunState = useCallback((latest: WorkflowTestRunResult) => {
    if (!stepRunContext || !stepSubmissionCurrent(stepRunContext)) return;
    setStepSession((current) => current?.context === stepRunContext && current.payload && stepSubmissionCurrent(stepRunContext)
      ? { ...current, payload: mergeStepTestRunState(current.payload, latest), message: testRunBannerMessage("step", latest, t) } : current);
  }, [stepRunContext, stepSubmissionCurrent, t]);
  usePendingTestRunPolling(pendingStepTestRunThreadId, receiveStepTestRunState);
  const visibleStepSession = stepRunContext?.authority.key === stepScopeKey && stepSubmissionCurrent(stepRunContext) &&
    stepRunContext.request.stepId === currentStepTestRunKey ? stepSession : null;
  const committedLaunch = useRef<{
    workflowId: string; projectId: string; revision: number | null | undefined;
    summary: string; inputs: Record<string, string>; rows: WorkflowTestRunInput[];
    errors: Record<string, string>; blocked: string; contractReady: boolean; jobStorageKey: string; threadStorageKey: string;
    onSubmitted?: (payload: WorkflowTestRunResult) => void; onConflict?: () => void;
    run: RunProof | null; approvalId: string; comment: string; threadId: string;
  } | null>(null);
  useLayoutEffect(() => {
    committedLaunch.current = {
      workflowId: normalizedWorkflowId, projectId: normalizedProjectId, revision: serverRevision,
      summary: workflowTestRunSummary, inputs: { ...workflowTestRunInputs },
      rows: workflowTestRunInputRows.map((row) => ({ ...row, options: [...row.options] })),
      errors: { ...workflowTestRunValidationErrors }, blocked: textOrEmpty(workflowTestRunBlockedReason),
      contractReady: Array.isArray(objectOrEmpty(objectOrEmpty(workflowContractQuery.data).interface).expected_inputs) &&
        !workflowContractQuery.isPending && !workflowContractQuery.isFetching && !workflowContractQuery.isError,
      jobStorageKey: latestJobStorageKey, threadStorageKey: pendingRunStorageKey,
      onSubmitted: onWorkflowTestRunSubmitted, onConflict: onRevisionConflict,
      run: session.run, approvalId: textOrEmpty(workflowTestRunPayload?.approval_id),
      comment: workflowTestRunApprovalComment, threadId: textOrEmpty(workflowTestRunPayload?.thread_id),
    };
  });
  const workflowTestRunMutation = useMutation<WorkflowTestRunResult, Error, TestRunLaunchIntent, SubmissionContext>({
    onMutate: (intent) => {
      if (activeSubmission.current || launchLock?.current) throw new Error("A workflow run is already starting.");
      const current = liveOwner.current;
      const draft = committedLaunch.current;
      if (!mounted.current || !draft) throw new Error("Test Run owner is not ready.");
      const captured = current.controlled ? current.bridge?.captureSubmissionOwner() ?? null : null;
      if (current.controlled && (!captured || !ownerCurrent(captured))) throw new Error("Test Run owner is not ready.");
      const reuse = intent && typeof intent === "object" ? intent.reuse : undefined;
      if (reuse && intent && (!captured || !Number.isSafeInteger(captured.editorRevision) || Number(captured.editorRevision) <= 0 ||
        submissionStamp(captured) !== submissionStamp(intent.owner) || !intent.isCurrentSelection())) throw new Error("Test Run owner changed before submission.");
      if (reuse && (!draft.contractReady || acceptedInputs.current !== reuse || reuse.workflowId !== draft.workflowId || reuse.projectId !== draft.projectId ||
        reusableInputContract(draft.rows) !== reuse.contract)) throw new Error("Previous Test Run inputs are unavailable.");
      const summary = reuse ? reuse.summary : draft.summary;
      const inputs = reuse ? { ...reuse.inputs } : draft.inputs;
      const errors: Record<string, string> = reuse ? {} : draft.errors;
      if (reuse) {
        if (!summary.trim()) errors.task_summary = t("react.workflows.test_run_summary_is_required", "Test run summary is required.");
        for (const row of draft.rows) {
          const error = workflowTestRunInputError(row, inputs[row.name] || "", t);
          if (error) errors[`inputs.${row.name}`] = error;
        }
      }
      const context: SubmissionContext = {
        token: Symbol("workflow-test-run"), owner: captured,
        stamp: captured ? submissionStamp(captured) : "legacy", bridge: current.bridge ?? undefined,
        request: { workflowId: draft.workflowId, projectId: draft.projectId,
          taskSummary: firstStringFromUnknownValue([summary, ""]).trim(), dryRun: false,
          expectedRevision: captured ? captured.editorRevision : draft.revision,
          inputs: testRunPayloadFromInputs(draft.rows.filter((row) => !row.fixed), inputs) },
        summary, inputs: { ...inputs }, rows: draft.rows,
        errors, blocked: draft.blocked, contractReady: draft.contractReady, reusedJobId: reuse?.jobId,
        isCurrentSelection: intent && typeof intent === "object" ? intent.isCurrentSelection : undefined,
        jobStorageKey: draft.jobStorageKey, threadStorageKey: draft.threadStorageKey,
        onSubmitted: draft.onSubmitted, onConflict: draft.onConflict,
      };
      if (launchLock) launchLock.current = context.token;
      activeSubmission.current = context;
      latestSubmission.current = context.token;
      restoreGeneration.current += 1;
      return context;
    },
    mutationFn: () => {
      // The admitted slot cannot be replaced until its own settlement. TanStack
      // passes the original onMutate result to callbacks, not to mutationFn.
      const context = activeSubmission.current;
      if (!context || !submissionCurrent(context)) throw new Error("Test Run owner changed before submission.");
      if (context.isCurrentSelection && !context.isCurrentSelection()) throw new Error("Test Run owner changed before submission.");
      if (context.blocked) throw new Error(context.blocked);
      if (!context.request.projectId) throw new Error(t("react.workflows.link_this_workflow_to_a_project_before_running_a_test_run", "Link this workflow to a project before running a test run."));
      const errors = Object.entries(context.errors);
      if (errors.length) {
        updateRunSession(context.stamp, () => submissionCurrent(context), (previous) => ({ ...previous,
          touched: { ...previous.touched, ...Object.fromEntries(errors.map(([path]) => [path, true])) } }));
        throw new Error(errors[0][1]);
      }
      if (!submissionCurrent(context)) throw new Error("Test Run owner changed before submission.");
      return testRunWorkflow(context.request);
    },
    onSuccess: (payload, _variables, context) => {
      if (!submissionCurrent(context)) return;
      if (context.isCurrentSelection && !context.isCurrentSelection()) return;
      if (context.owner && !context.bridge?.acceptAcknowledgement(payload, context.owner)) return;
      if (!submissionCurrent(context)) return;
      const run: RunProof = { owner: context.owner, stamp: context.stamp, generation: ++runGeneration.current,
        threadId: textOrEmpty(payload.thread_id), jobId: textOrEmpty(payload.job_id), workflowId: context.request.workflowId };
      const previous = currentRun.current;
      currentRun.current = run;
      restoreGeneration.current += 1;
      updateRunSession(context.stamp, () => runCurrent(run), (current) => ({ ...current, payload, run,
        message: testRunBannerMessage("workflow", payload, t) }));
      const jobId = run.jobId;
      const contract = reusableInputContract(context.rows);
      acceptedInputs.current = context.owner && jobId && context.contractReady && contract !== null ? Object.freeze({
        jobId, workflowId: context.request.workflowId, projectId: context.request.projectId,
        summary: context.request.taskSummary, contract,
        inputs: Object.freeze(Object.fromEntries(context.rows.filter((row) => !row.fixed).map((row) => [row.name, context.inputs[row.name] || ""]))),
      }) : null;
      if (jobId) {
        setWorkflowTestRunJobId((current) => runCurrent(run) ? jobId : current);
        if (runCurrent(run)) {
          try { globalThis.localStorage?.setItem(context.jobStorageKey, jobId); } catch { /* unavailable */ }
        }
      }
      if (context.owner) persistRun(run, previous, context.threadStorageKey);
      setWorkflowTestRunApprovalComment((current) => submissionCurrent(context) ? "" : current);
      if (submissionCurrent(context)) context.onSubmitted?.(payload);
    },
    onError: (error, _variables, context) => {
      // A rejected second admission has no context and must neither publish its
      // error as the older admitted request nor mutate that request's state.
      if (!submissionCurrent(context) || !context) return;
      if (context.isCurrentSelection && !context.isCurrentSelection()) return;
      if (isWorkflowRevisionConflict(error)) {
        if (submissionCurrent(context)) context.onConflict?.();
        updateRunSession(context.stamp, () => submissionCurrent(context), (previous) => ({ ...previous, message: conflictMessage }));
        return;
      }
      if (isHttpError(error) && error.status === 422) {
        if (context.reusedJobId === acceptedInputs.current?.jobId) acceptedInputs.current = null;
        const payload = objectOrEmpty(error.payload);
        const detail = payload.detail ?? objectOrEmpty(payload.error).details ?? payload.details;
        const issues = Array.isArray(detail) ? detail : objectOrEmpty(detail).errors;
        const fields: Record<string, WorkflowTestRunServerError> = {};
        for (const issue of Array.isArray(issues) ? issues : []) {
          const row = objectOrEmpty(issue);
          const path = textOrEmpty(row.path);
          const message = textOrEmpty(row.message);
          const input = context.rows
            .filter((item) => !item.fixed && (`inputs.${item.name}` === path ||
              (inputSupportsJson(item.type) && path.startsWith(`inputs.${item.name}.`))))
            .sort((left, right) => right.name.length - left.name.length)[0];
          if (message && (input || path === "task_summary")) {
            const field = input ? `inputs.${input.name}` : path;
            fields[field] = { message, value: input ? context.inputs[input.name] || "" : context.summary };
          }
        }
        updateRunSession(context.stamp, () => submissionCurrent(context), (previous) => ({ ...previous, errors: fields }));
      }
      updateRunSession(context.stamp, () => submissionCurrent(context), (previous) => ({ ...previous,
        message: context.reusedJobId && isHttpError(error) && error.status === 422
          ? t("react.workflows.run_again_inputs_rejected", "The previous run's inputs were rejected. Review the current launch form before starting again.")
          : error instanceof Error ? error.message : t("react.workflows.workflow_test_run_failed", "Workflow test run failed.") }));
    },
    onSettled: (_data, _error, _variables, context) => {
      if (context && activeSubmission.current?.token === context.token) {
        if (launchLock?.current === context.token) launchLock.current = null;
        activeSubmission.current = null;
        // Wake only a deferred restore readiness check, never reset owned fields.
        if (mounted.current) setSubmissionSettlement((current) => current + 1);
      }
    },
  });
  const workflowTestRunMutationIsCurrent = workflowTestRunMutation.context
    ? submissionCurrent(workflowTestRunMutation.context) : !controlled && workflowTestRunMutation.isIdle;
  const startWorkflowTestRun = useCallback(() => {
    if (activeSubmission.current || againAdmission.current || workflowTestRunIsPending(workflowTestRunPayload)) return;
    workflowTestRunMutation.mutate();
  }, [workflowTestRunMutation, workflowTestRunPayload]);
  const startWorkflowTestRunAgain = useCallback(async (jobId: string, isCurrentSelection: () => boolean = () => true): Promise<WorkflowTestRunAgainOutcome> => {
    const draft = committedLaunch.current;
    const current = liveOwner.current;
    const owner = current.bridge?.captureSubmissionOwner() ?? null;
    if (!mounted.current || !current.controlled || !owner || !ownerCurrent(owner) || !isCurrentSelection() || !draft?.projectId || draft.blocked ||
      activeSubmission.current || againAdmission.current || workflowTestRunIsPending(workflowTestRunPayload)) return "blocked";
    const reuse = acceptedInputs.current;
    if (!Number.isSafeInteger(owner.editorRevision) || Number(owner.editorRevision) <= 0 || !draft.contractReady || reuse?.jobId !== jobId || reuse.workflowId !== draft.workflowId || reuse.projectId !== draft.projectId ||
      reusableInputContract(draft.rows) !== reuse.contract || !reuse.summary.trim() ||
      draft.rows.some((row) => workflowTestRunInputError(row, reuse.inputs[row.name] || "", t))) return "needs-inputs";
    const admission = Symbol("workflow-test-run-again");
    againAdmission.current = admission;
    const previousRun = currentRun.current;
    try {
      const payload = await workflowTestRunMutation.mutateAsync({ reuse, owner, isCurrentSelection });
      return ownerCurrent(owner) && currentRun.current !== previousRun && currentRun.current?.jobId === textOrEmpty(payload.job_id) ? "admitted" : "blocked";
    } catch (error) {
      if (!ownerCurrent(owner) || !isCurrentSelection() || activeSubmission.current || isWorkflowRevisionConflict(error)) return "blocked";
      return "needs-inputs";
    } finally { if (againAdmission.current === admission) againAdmission.current = null; }
  }, [ownerCurrent, t, workflowTestRunMutation, workflowTestRunPayload]);
  const approvalCurrent = useCallback((context: ApprovalContext | undefined) => Boolean(context &&
    latestApproval.current === context.token && (context.run ? runCurrent(context.run) : ownerCurrent(null))), [ownerCurrent, runCurrent]);
  const workflowTestRunApprovalMutation = useMutation<WorkflowTestRunResult, Error, { approved: boolean }, ApprovalContext>({
    onMutate: ({ approved }) => {
      if (activeApproval.current) throw new Error("A workflow test run approval is already pending.");
      const draft = committedLaunch.current;
      const run = draft?.run ?? null;
      if (!mounted.current || !draft || liveOwner.current.controlled && (!run || !runCurrent(run)))
        throw new Error(t("react.workflows.workflow_test_run_review_session_is_unavailable", "Workflow test run review session is unavailable."));
      const context = { token: Symbol("workflow-test-run-approval"), run, threadId: draft.threadId,
        approvalId: draft.approvalId, comment: draft.comment, approved };
      activeApproval.current = context;
      latestApproval.current = context.token;
      return context;
    },
    mutationFn: async () => {
      const context = activeApproval.current;
      if (!context || !approvalCurrent(context)) throw new Error(t("react.workflows.workflow_test_run_review_session_is_unavailable", "Workflow test run review session is unavailable."));
      const threadId = context.threadId;
      if (!threadId) throw new Error(t("react.workflows.workflow_test_run_review_session_is_unavailable", "Workflow test run review session is unavailable."));
      if (!context.approvalId) throw new Error(t("react.workflows.workflow_test_run_approval_id_is_unavailable_refresh_before_deciding", "Workflow test run approval ID is unavailable. Refresh before deciding."));
      await submitWorkflowTestRunApproval(threadId, { approval_id: context.approvalId, approved: context.approved, comment: context.comment });
      if (!approvalCurrent(context)) throw new Error("Test Run owner changed before approval readback.");
      return waitForWorkflowTestRunState(threadId, { attempts: 80, includeAwaitingApproval: true, intervalMs: 500 });
    },
    onSuccess: (payload, _variables, context) => {
      if (!approvalCurrent(context) || context.run && !payloadMatchesRun(payload, context.run)) return;
      updateRunSession(context.run?.stamp || "legacy", () => approvalCurrent(context), (previous) => ({ ...previous, payload,
        message: testRunBannerMessage("workflow", payload, t) }));
      setWorkflowTestRunApprovalComment((current) => approvalCurrent(context) ? "" : current);
    },
    onError: (error, _variables, context) => {
      if (!approvalCurrent(context) || !context) return;
      updateRunSession(context.run?.stamp || "legacy", () => approvalCurrent(context), (previous) => ({ ...previous,
        message: error instanceof Error ? error.message : t("react.workflows.workflow_test_run_approval_update_failed", "Workflow test run approval update failed.") }));
    },
    onSettled: (_data, _error, _variables, context) => {
      if (context && activeApproval.current?.token === context.token) activeApproval.current = null;
    },
  });
  const workflowTestRunApprovalMutationIsCurrent = workflowTestRunApprovalMutation.context
    ? approvalCurrent(workflowTestRunApprovalMutation.context) : !controlled && workflowTestRunApprovalMutation.isIdle;
  const stepTestRunMutation = useMutation<StepTestRunResult, Error, void, IsolatedStepSubmission>({
    onMutate: () => {
      if (activeStepSubmission.current || launchLock?.current) throw new Error("A workflow run is already starting.");
      const draft = committedStepRequest.current;
      if (!draft || !stepAuthorityCurrent(draft.authority)) throw new Error("Step Test Run owner is not ready.");
      const context = { ...draft, token: Symbol("isolated-step-test-run") };
      if (launchLock) launchLock.current = context.token;
      activeStepSubmission.current = context;
      latestStepSubmission.current = context.token;
      return context;
    },
    mutationFn: () => {
      const context = activeStepSubmission.current;
      if (!context || !stepSubmissionCurrent(context)) throw new Error("Step Test Run owner changed before submission.");
      if (context.blocked) throw new Error(context.blocked);
      if (!context.request.projectId) {
        throw new Error(t("react.workflows.link_this_workflow_to_a_project_before_running_a_test_run", "Link this workflow to a project before running a test run."));
      }
      if (!context.request.taskSummary) {
        throw new Error(t("react.workflows.step_test_run_summary_is_required", "Step test run summary is required."));
      }
      if (!context.request.stepId || context.local) {
        throw new Error(t("react.workflows.select_a_saved_step_before_running_a_step_test_run", "Select a saved step before running a step test run."));
      }
      return testRunWorkflowStep(context.request);
    },
    onSuccess: (payload, _variables, context) => {
      if (!context || !stepSubmissionCurrent(context)) return;
      setStepSession((current) => stepSubmissionCurrent(context)
        ? { context, payload, message: testRunBannerMessage("step", payload, t) } : current);
    },
    onError: (error, _variables, context) => {
      if (!context || !stepSubmissionCurrent(context)) return;
      const conflict = isWorkflowRevisionConflict(error);
      if (conflict) context.onConflict?.();
      if (!stepSubmissionCurrent(context)) return;
      const message = conflict ? conflictMessage : error instanceof Error ? error.message : t("react.workflows.step_test_run_failed", "Step test run failed.");
      setStepSession((current) => stepSubmissionCurrent(context) ? { context, payload: null, message } : current);
    },
    onSettled: (_data, _error, _variables, context) => {
      if (context && activeStepSubmission.current?.token === context.token) {
        if (launchLock?.current === context.token) launchLock.current = null;
        activeStepSubmission.current = null;
      }
    },
  });
  const workflowTestRunInterfaceDisabled = objectOrEmpty(objectOrEmpty(workflowContractQuery.data).interface).enabled === false;
  const workflowTestRunInputRows = useMemo(() => {
    const contractInterface = objectOrEmpty(
      objectOrEmpty(workflowContractQuery.data).interface,
    );
    const declaredRows = Array.isArray(contractInterface.expected_inputs)
      ? contractInterface.expected_inputs
      : [];
    const schema = objectOrEmpty(contractInterface.input_schema);
    const properties = objectOrEmpty(schema.properties);
    const required = Array.isArray(schema.required) ? schema.required : [];
    // Schema-mode contracts do not persist duplicate expected-input rows. The
    // launch form reads the applied public schema without changing that contract.
    const rows = contractInterface.input_mode === "json_schema" && !declaredRows.length
      ? Object.entries(properties).flatMap(([name, rawProperty]) => {
          const property = objectOrEmpty(rawProperty);
          const type = textOrEmpty(property.type);
          if (!["string", "number", "integer", "boolean", "object", "array"].includes(type)) return [];
          return [{
            name,
            label: textOrEmpty(property.title) || name,
            description: textOrEmpty(property.description),
            type,
            required: required.includes(name),
          }];
        })
      : declaredRows;
    return rows
      .map((item) => workflowTestRunInputRow(objectOrEmpty(item), t))
      .filter((item) => Boolean(item.name) && item.name !== "project_id");
  }, [workflowContractQuery.data, t]);
  const workflowTestRunValidationErrors = useMemo(() => {
    const errors: Record<string, string> = {};
    if (!workflowTestRunSummary.trim()) {
      errors.task_summary = t("react.workflows.test_run_summary_is_required", "Test run summary is required.");
    }
    for (const row of workflowTestRunInputRows) {
      const error = workflowTestRunInputError(row, workflowTestRunInputs[row.name] || "", t);
      if (error) errors[`inputs.${row.name}`] = error;
    }
    for (const [path, error] of Object.entries(workflowTestRunServerErrors)) {
      const row = workflowTestRunInputRows.find((item) => `inputs.${item.name}` === path && !item.fixed);
      const value = path === "task_summary" ? workflowTestRunSummary : row ? workflowTestRunInputs[row.name] || "" : undefined;
      if (value === error.value) errors[path] = error.message;
    }
    return errors;
  }, [workflowTestRunInputRows, workflowTestRunInputs, workflowTestRunSummary, workflowTestRunServerErrors, t]);
  const workflowTestRunFieldErrors = Object.fromEntries(Object.entries(workflowTestRunValidationErrors)
    .filter(([path]) => workflowTestRunTouched[path] || Boolean(workflowTestRunServerErrors[path])));
  const workflowTestRunHasValidationErrors = Boolean(Object.keys(workflowTestRunValidationErrors).length);
  useEffect(() => {
    setWorkflowTestRunInputs((current) => {
      const next: Record<string, string> = {};
      for (const row of workflowTestRunInputRows) {
        next[row.name] = current[row.name] || "";
      }
      return next;
    });
  }, [workflowTestRunInputRows]);


  return {
    setStepTestRunInputs,
    setStepTestRunSummary,
    setWorkflowTestRunApprovalComment,
    setWorkflowTestRunInputs,
    setWorkflowTestRunMessage,
    setWorkflowTestRunSummary,
    startWorkflowTestRun,
    startWorkflowTestRunAgain,
    refreshWorkflowTestRunSubmissionOwner,
    workflowTestRunMutationIsCurrent,
    workflowTestRunApprovalMutationIsCurrent,
    touchWorkflowTestRunField,
    updateWorkflowTestRunInput,
    updateWorkflowTestRunSummary,
    workflowTestRunFieldErrors,
    workflowTestRunHasValidationErrors,
    workflowTestRunValidationErrors,
    stepTestRunInputRows,
    stepTestRunInputs,
    stepTestRunMessage: visibleStepSession?.message ?? "",
    stepTestRunMutation,
    stepTestRunPayload: visibleStepSession?.payload ?? null,
    stepTestRunSummary,
    workflowTestRunApprovalComment,
    workflowTestRunApprovalMutation,
    workflowTestRunInputRows,
    workflowTestRunInterfaceDisabled,
    workflowTestRunInputs,
    workflowTestRunJobId,
    workflowTestRunMessage,
    workflowTestRunMutation,
    workflowTestRunPayload,
    workflowTestRunPending: workflowTestRunIsPending(workflowTestRunPayload),
    workflowTestRunSummary,
  };
}
