import { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { DsAlert, DsDialog } from "@/components/shared/design-system";
import { useConfirmDialog } from "@/components/shared/use-confirm-dialog";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { HttpError } from "@/lib/http/errors";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import {
  convertWorkflowGroup,
  workflowChangeErrorIssues,
  type GroupExtractionField,
  type GroupExtractionResult,
  type WorkflowChangeIssue,
} from "@/pages/workflows/builder/api";

type Props = {
  workflowId: string;
  groupId: string;
  groupName: string;
  revision: number;
  blockedReason: string;
  projectId?: string;
  onClose: () => void;
  onReload: () => Promise<unknown>;
  onApplied: () => Promise<unknown>;
  /** A step's name as the canvas shows it, so a refusal names "Build the result", not build_result. */
  stepName?: (stepId: string) => string;
  /** Opens the step a refusal points at, closing this dialog. */
  onOpenStep?: (stepId: string) => void;
};

type Translate = ReturnType<typeof useI18n>["t"];

/** "state.inputs.x" is the workflow input x, any other "state.x" the variable x. */
function issueSubject(subject: unknown): { name: string; isInput: boolean } | null {
  const match = /^state\.(inputs\.)?([^.[\]]+)/.exec(typeof subject === "string" ? subject : "");
  if (!match) return null;
  return { name: match[2], isInput: Boolean(match[1]) };
}

/** A refusal in plain words with what to do; the server's sentence for anything else. */
function groupExtractionIssueText(issue: WorkflowChangeIssue, t: Translate, where: { step: string; field: string }): string {
  const subject = issueSubject(issue.subject);
  const name = subject?.name || "";
  switch (issue.code) {
    case "unknown_input_type":
      if (subject?.isInput) return t("react.workflows.convert_issue_untyped_input", "The group reads the workflow input “{name}”, which has no type. Set its type in Workflow settings → Inputs, then preview again.", { name });
      if (subject) return t("react.workflows.convert_issue_untyped_variable", "The group reads the variable “{name}”, whose type is not known. Give it a type where it is set (for example in Set variable or Transform), then preview again.", { name });
      break;
    case "ambiguous_input_type":
      if (subject) return t("react.workflows.convert_issue_mixed_type", "The group reads “{name}” with different types. Use one type for it, then preview again.", { name });
      break;
    case "mutable_external_state":
      if (subject) return t("react.workflows.convert_issue_read_and_written", "The group both reads and changes “{name}”, which is set before the group. Set it inside the group or keep the result in another variable, then preview again.", { name });
      break;
    case "for_each_source_boundary":
      if (subject) return t("react.workflows.convert_issue_for_each_not_list", "The For each in the group goes over “{name}”, which is not declared as a list. Declare it as a list, then preview again.", { name });
      break;
    case "whole_state_input":
      // Owner feedback: "read only the values" read as "read-only"; say which step, which field, what to put there.
      if (issue.subject === "state.inputs") return t("react.workflows.convert_issue_all_inputs", "The step “{step}” takes all of the workflow’s inputs at once in its “{field}” field (state.inputs). A child workflow receives single named values, so this cannot move into it. Open the step and put one input there instead, for example state.inputs.<input name>, then preview again.", { step: where.step || t("react.workflows.convert_issue_a_step", "in the group"), field: where.field || "Source" });
      return t("react.workflows.convert_issue_all_state", "The step “{step}” takes the workflow’s whole data at once in its “{field}” field ({value}). A child workflow receives single named values, so this cannot move into it. Open the step and put one value there instead, for example state.<variable name>, then preview again.", { step: where.step || t("react.workflows.convert_issue_a_step", "in the group"), field: where.field || "Source", value: stringFromUnknownValue(issue.subject) || "state" });
    case "runtime_context_boundary":
      return t("react.workflows.convert_issue_run_context", "A step in the group reads information about the current run, which a child workflow cannot receive. Remove that read or keep this step outside the group.");
    case "child_id_unavailable":
      return t("react.workflows.convert_issue_id_taken", "A workflow with this ID already exists. Choose another ID.");
    case "partial_state_write": {
      const target = /^steps\.[^.]+\.outputs\.(.+)$/.exec(String(issue.path || ""))?.[1];
      if (target && where.step) return t("react.workflows.convert_issue_partial_write", "The step “{step}” writes only part of state.{target}. A child workflow cannot preserve the other fields of this object. Publish a whole typed output instead, or keep this writer outside the group, then preview again.", { step: where.step, target });
      break;
    }
    case "parent_output_boundary": {
      const output = /^settings\.callable\.expected_outputs\.(.+)$/.exec(String(issue.path || ""))?.[1];
      if (output) return t("react.workflows.convert_issue_parent_output", "The parent workflow needs callable output “{output}”, but its retained typed source cannot be proved after this group moves. Keep a compatible typed output in the parent, or keep its writer outside the group, then preview again.", { output });
      if (/^steps\.[^.]+\.override\.returned_files$/.test(String(issue.path || "")) && where.step) return t("react.workflows.convert_issue_returned_file", "The step “{step}” returns a file from the parent workflow. Untick “Return from workflow” for this step or keep it outside the group, then preview again.", { step: where.step });
      break;
    }
    case "group_not_found":
      return t("react.workflows.convert_issue_group_gone", "This group no longer exists. Close this window and reload the workflow.");
    default:
      break;
  }
  return issue.message || t("react.workflows.convert_failed", "Conversion failed.");
}

/** The field a refusal path ends in, as a label: "…transform.input.source" → "Source". */
function issueField(path: unknown): string {
  const last = (typeof path === "string" ? path : "").split(".").findLast(Boolean) || "";
  return last ? last.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase()) : "";
}

/** The step a refusal points at ("steps.<id>…"), or the For each ("for_each.<id>…"). */
function issuePlace(path: unknown): string {
  const match = /^(?:steps|for_each|groups)\.([^.]+)/.exec(typeof path === "string" ? path : "");
  return match ? match[1] : "";
}

function BoundaryFields({ title, fields, mappings }: Readonly<{
  title: string;
  fields: GroupExtractionField[];
  mappings: Record<string, string>;
}>) {
  const { t } = useI18n();
  return (
    <section className="space-y-2">
      <h3 className="font-medium">{title}</h3>
      {fields.length ? (
        <dl className="space-y-2 text-sm">
          {fields.map((field) => (
            <div className="rounded border border-[var(--fs-border-default)] p-2" key={field.name}>
              <dt className="font-mono">{field.name}</dt>
              <dd><span>{field.type}</span>{" · "}<code>{mappings[field.name]}</code></dd>
            </div>
          ))}
        </dl>
      ) : <p className="text-sm text-[var(--fs-text-tertiary)]">{t("react.workflows.convert_no_fields", "None")}</p>}
    </section>
  );
}

/** The preview is tied to both the saved revision and the exact child identity. */
export function GroupExtractionDialog({ workflowId, groupId, groupName, revision, blockedReason, projectId, onClose, onReload, onApplied, stepName, onOpenStep }: Readonly<Props>) {
  const { t } = useI18n();
  const { confirmDialog, requestConfirmation } = useConfirmDialog();
  const inFlight = useRef(false);
  const seededChildId = useRef(`${workflowId}_${groupId}`.toLowerCase().replaceAll(/[^a-z0-9_]/g, "_")).current;
  const initialIdentity = useRef({ name: groupName, childId: seededChildId });
  const [childId, setChildId] = useState(seededChildId);
  const [name, setName] = useState(groupName);
  const [busy, setBusy] = useState(false);
  const [requestPhase, setRequestPhase] = useState<"preview" | "apply" | null>(null);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");
  const [issues, setIssues] = useState<WorkflowChangeIssue[]>([]);
  const [reviewed, setReviewed] = useState<{ key: string; result: GroupExtractionResult } | null>(null);
  const [completed, setCompleted] = useState<GroupExtractionResult | null>(null);
  const scopeKey = JSON.stringify([workflowId, groupId]);
  const requestKey = JSON.stringify([workflowId, groupId, revision, childId, name]);
  const latestOwner = useRef({ scopeKey, requestKey, onClose, onOpenStep, onReload, onApplied });
  latestOwner.current = { scopeKey, requestKey, onClose, onOpenStep, onReload, onApplied };
  const mounted = useRef(true);
  const dismissPending = useRef(false);
  const dismissed = useRef(false);
  const dismissSerial = useRef(0);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const preview = reviewed?.key === requestKey ? reviewed.result : null;
  const dirtyIdentity = name !== initialIdentity.current.name || childId !== initialIdentity.current.childId;
  const stalePreview = stale || Boolean(reviewed && reviewed.key !== requestKey);
  const invalidName = !name.trim() || name.length > 200;
  const invalidChildId = !/^[a-z][a-z0-9_]*$/.test(childId);
  const unavailable = Boolean(blockedReason || busy || completed || invalidName || invalidChildId || revision < 1);
  const blockedByIssues = Boolean(preview && !preview.can_convert);
  const createReason = busy ? t("react.workflows.convert_busy", "Wait for the current request to finish.")
    : revision < 1 ? t("react.workflows.convert_invalid_revision", "Reload the workflow before creating a child.")
      : invalidName ? t("react.workflows.convert_invalid_name", "Enter a child workflow name of 1–200 characters.")
        : invalidChildId ? t("react.workflows.convert_invalid_id", "Use an ID starting with a lowercase letter and containing only lowercase letters, digits and underscores.")
          : blockedReason || (stalePreview ? t("react.workflows.convert_preview_stale", "The preview is out of date. Preview the conversion again.")
            : blockedByIssues ? t("react.workflows.convert_fix_issues_first", "Fix the issues above first.")
              : !preview ? t("react.workflows.convert_preview_first", "Preview the conversion first.") : "");
  const previewStatus = requestPhase === "preview" ? t("react.workflows.convert_preview_pending", "Checking conversion…")
    : requestPhase === "apply" ? t("react.workflows.convert_apply_pending", "Creating child workflow…")
      : stalePreview ? t("react.workflows.convert_preview_stale_status", "Preview out of date. Preview again to review the current name, ID and workflow revision.")
        : preview?.can_convert ? t("react.workflows.convert_preview_ready", "Preview ready for this name, ID and workflow revision.")
          : preview ? t("react.workflows.convert_preview_refused", "Preview found issues to fix before creating the child.")
            : t("react.workflows.convert_preview_needed", "Preview the conversion to see its inputs and outputs.");

  function finishDismiss(owner: string, stepId?: string) {
    if (!mounted.current || latestOwner.current.requestKey !== owner || inFlight.current || dismissed.current) return;
    dismissed.current = true;
    if (stepId) latestOwner.current.onOpenStep?.(stepId);
    else latestOwner.current.onClose();
  }

  async function dismiss(stepId?: string) {
    if (inFlight.current || dismissPending.current || dismissed.current) return;
    const owner = requestKey;
    if (!dirtyIdentity || completed) { finishDismiss(owner, stepId); return; }
    dismissPending.current = true;
    const serial = ++dismissSerial.current;
    try {
      const discard = await requestConfirmation({
        title: t("react.workflows.convert_discard_title", "Discard child workflow draft?"),
        body: t("react.workflows.convert_discard_body", "Your changes to the child workflow name or ID will be lost."),
        cancelLabel: t("react.workflows.convert_keep_editing", "Keep editing"),
        confirmLabel: t("react.workflows.convert_discard_draft", "Discard draft"),
        destructive: true,
      });
      if (discard && dismissSerial.current === serial) finishDismiss(owner, stepId);
    } finally {
      if (dismissSerial.current === serial) dismissPending.current = false;
    }
  }

  const seenRevision = useRef(revision);
  useEffect(() => {
    if (seenRevision.current === revision) return;
    seenRevision.current = revision;
    setReviewed(null);
    setIssues([]);
    if (!completed) setStale(true);
  }, [completed, revision]);

  async function requestConversion(apply: boolean) {
    if (unavailable || inFlight.current || (apply && !preview?.can_convert)) return;
    const requestScope = scopeKey;
    inFlight.current = true;
    setBusy(true);
    setRequestPhase(apply ? "apply" : "preview");
    setError("");
    setIssues([]);
    try {
      const result = await convertWorkflowGroup(workflowId, groupId, {
        child_workflow_id: childId,
        display_name: name.trim(),
        expected_revision: revision,
        dry_run: !apply,
        ...(apply ? { preview_digest: preview?.preview_digest } : {}),
      });
      if (!mounted.current || latestOwner.current.scopeKey !== requestScope) return;
      if (!apply && latestOwner.current.requestKey !== requestKey) { setStale(true); return; }
      if (apply) {
        if (!result.applied) throw new Error(t("react.workflows.convert_not_applied", "The conversion was not applied. Preview it again."));
        setCompleted(result);
        setReviewed(null);
        try { await latestOwner.current.onApplied(); }
        catch {
          if (mounted.current && latestOwner.current.scopeKey === requestScope) setError(t("react.workflows.convert_reload_failed", "The child was created, but the canvas could not reload. Reload the page to see the updated parent."));
        }
      } else {
        setReviewed({ key: requestKey, result });
        setIssues(result.issues);
        setStale(false);
      }
    } catch (error_) {
      if (!mounted.current || latestOwner.current.scopeKey !== requestScope) return;
      setReviewed(null);
      setIssues(workflowChangeErrorIssues(error_));
      if (error_ instanceof HttpError && error_.status === 409) {
        setStale(true);
        setError(t("react.workflows.convert_stale", "The workflow or conversion plan changed. Preview the conversion again before applying."));
        try { await latestOwner.current.onReload(); }
        catch {
          if (mounted.current && latestOwner.current.scopeKey === requestScope) setError(t("react.workflows.convert_conflict_reload_failed", "The workflow changed and could not reload. Reload the page before previewing again."));
        }
      } else {
        setError(error_ instanceof Error ? error_.message : t("react.workflows.convert_failed", "Conversion failed."));
      }
    } finally {
      inFlight.current = false;
      if (mounted.current) {
        setBusy(false);
        setRequestPhase(null);
      }
    }
  }

  // The check runs as the dialog opens, so a reason it cannot convert shows at once.
  const previewedOnOpen = useRef(false);
  useEffect(() => {
    if (previewedOnOpen.current || unavailable) return;
    previewedOnOpen.current = true;
    void requestConversion(false);
  });

  const title = t("react.workflows.convert_group_to_child", "Convert group to child workflow");
  const childHref = completed ? `/registry/workflows/${encodeURIComponent(completed.child_workflow_id)}/builder${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}` : "";
  return (<>
    <DsDialog open busy={busy} title={title} eyebrow={t("react.workflows.canvas", "Workflow canvas")} description={t("react.workflows.convert_explanation", "Preview the child's inputs and outputs. Applying creates the child and replaces this group with a workflow call together.")} onClose={() => { void dismiss(); }}>
      <div className="my-4 grid gap-3 sm:grid-cols-2">
        <label className="text-sm">{t("react.workflows.child_workflow_name", "Child workflow name")}
          <input className="fs-input mt-1 w-full" disabled={busy || Boolean(completed)} maxLength={200} onChange={(event) => { setName(event.target.value); setReviewed(null); setIssues([]); setStale(true); }} value={name} />
        </label>
        <label className="text-sm">{t("react.workflows.child_workflow_id", "Child workflow ID")}
          <input aria-describedby="child-id-hint" className="fs-input mt-1 w-full font-mono" disabled={busy || Boolean(completed)} onChange={(event) => { setChildId(event.target.value); setReviewed(null); setIssues([]); setStale(true); }} value={childId} />
        </label>
      </div>
      <p className="text-xs text-[var(--fs-text-tertiary)]" id="child-id-hint">{t("react.workflows.child_id_hint", "This ID is suggested from the workflow and group IDs. It is independent of the child name; changing the name will not change this ID. Use a lowercase letter first, then lowercase letters, digits or underscores.")}</p>
      {error ? <p className="mt-3 text-sm text-[var(--fs-status-danger)]" role="alert">{error}</p> : null}
      {issues.length ? <div className="my-3">
        <DsAlert tone="danger" title={t("react.workflows.convert_blocked_title", "Can't create the child workflow yet")}>
          <ul className="space-y-2" aria-label={t("react.workflows.conversion_issues", "Conversion issues")}>
            {issues.map((issue, index) => {
              const place = issuePlace(issue.path);
              const name = place ? stepName?.(place) || place : "";
              const field = issueField(issue.path);
              return <li key={`${issue.path}:${issue.code}:${index}`}>
                <p>{groupExtractionIssueText(issue, t, { step: name, field })}</p>
                {place ? <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--fs-text-tertiary)]">
                  <span>{t("react.workflows.convert_issue_place", "In step {step}", { step: name })}{field ? ` · ${field}` : ""}</span>
                  {onOpenStep ? <Button size="sm" variant="secondary" onClick={() => { void dismiss(place); }}>{t("react.workflows.convert_issue_open_step", "Open step")}</Button> : null}
                </div> : null}
              </li>;
            })}
          </ul>
        </DsAlert>
      </div> : null}
      {!completed ? <section aria-label={t("react.workflows.convert_preview_region", "Conversion preview")} className="my-4 min-h-[10rem]">
        <p aria-label={t("react.workflows.convert_preview_status_label", "Conversion preview status")} className="mb-3 text-sm text-[var(--fs-text-secondary)]" role="status">{previewStatus}</p>
        <div className="grid gap-4 sm:grid-cols-2">
          {preview ? <>
            <BoundaryFields title={t("react.workflows.inputs", "Inputs")} fields={preview.interface.expected_inputs || []} mappings={preview.input_mapping} />
            <BoundaryFields title={t("react.workflows.outputs", "Outputs")} fields={preview.interface.expected_outputs || []} mappings={preview.output_mapping} />
          </> : <>
            <section className="min-h-20 space-y-2"><h3 className="font-medium">{t("react.workflows.inputs", "Inputs")}</h3><p className="text-sm text-[var(--fs-text-tertiary)]">{t("react.workflows.convert_preview_waiting", "Awaiting preview")}</p></section>
            <section className="min-h-20 space-y-2"><h3 className="font-medium">{t("react.workflows.outputs", "Outputs")}</h3><p className="text-sm text-[var(--fs-text-tertiary)]">{t("react.workflows.convert_preview_waiting", "Awaiting preview")}</p></section>
          </>}
        </div>
      </section> : null}
      {completed ? <p className="my-4" role="status">{t("react.workflows.child_created", "Child workflow created.")}{" "}<a className="text-[var(--fs-text-brand)] underline" href={childHref}>{t("react.workflows.open_child_workflow", "Open child workflow")}</a></p> : null}
      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <Button disabled={busy} onClick={() => { void dismiss(); }} variant="secondary">{t("common.close", "Close")}</Button>
        {!completed ? <>
          <Button disabled={unavailable} onClick={() => void requestConversion(false)} variant="secondary">{t("react.workflows.preview_conversion", "Preview conversion")}</Button>
          <Button aria-describedby={createReason ? "conversion-create-reason" : undefined} disabled={unavailable || !preview?.can_convert} title={createReason || undefined} onClick={() => void requestConversion(true)}>{t("react.workflows.create_child_workflow", "Create child workflow")}</Button>
        </> : null}
      </div>
      {!completed ? <p aria-live="polite" className="min-h-5 text-right text-sm text-[var(--fs-text-secondary)]" id="conversion-create-reason">{createReason}</p> : null}
    </DsDialog>
    <ConfirmDialog {...confirmDialog} />
  </>);
}
