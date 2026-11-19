/* eslint-disable @typescript-eslint/no-explicit-any */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";

import { DsDialog, DsSelect } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { fetchJson } from "@/lib/http/client";
import { useI18n } from "@/lib/i18n/useI18n";
import { localizedWORKFLOW_INTERFACE_FIELD_TYPES, objectOrEmpty, textOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import { createWorkflowInterfaceFieldDraft } from "@/pages/workflows/detail/detail-interface";
import {
  fieldsFromWorkflowInputs,
  REVIEW_FIELD_TYPES,
  reviewDraftFromConfig,
  reviewIssuesByPlace,
  reviewPayload,
  sourceSuggestions,
  type ReviewDraft,
  type ReviewFieldDraft,
  type ReviewIssue,
} from "./detail-approval-review-model";
import { ContractBlock } from "./detail-contract-block";
import { ContractFieldControls } from "./detail-contract-field-controls";

type Ctx = Record<string, any>;

type CallableWorkflow = { workflow_id?: string; id?: string; display_name?: string; description?: string; expected_inputs?: unknown };

function Issues({ issues }: Readonly<{ issues: readonly ReviewIssue[] | undefined }>) {
  if (!issues?.length) return null;
  return <ul className="space-y-1">{issues.map((issue) => <li className="fs-field-helper error" key={`${issue.code}:${issue.path}`} role="alert">{issue.message}</li>)}</ul>;
}

/** «Take fields from a workflow»: pick a callable workflow; its inputs become the review fields. */
function FieldsFromWorkflowDialog({ workflowId, current, onClose, onPick }: Readonly<{ workflowId: string; current: string; onClose: () => void; onPick: (workflow: CallableWorkflow) => void }>) {
  const { t } = useI18n();
  const [chosen, setChosen] = useState(current);
  // The list Invoke steps read: the same request, cached under the same key.
  const query = useQuery({
    queryKey: ["workflows", "meta", "callable-workflows", workflowId],
    queryFn: () => fetchJson<{ workflows?: CallableWorkflow[] }>("/api/workflows/meta/callable-workflows", { method: "GET" }),
  });
  const rows = (query.data?.workflows ?? []).filter((row) => textOrEmpty(row.workflow_id || row.id) && textOrEmpty(row.workflow_id || row.id) !== workflowId);
  const picked = rows.find((row) => textOrEmpty(row.workflow_id || row.id) === chosen);
  const inputs = Array.isArray(picked?.expected_inputs) ? picked.expected_inputs.map(objectOrEmpty) : [];
  return (
    <DsDialog
      description={t("react.workflows.review.from_workflow_help", "The inputs of the chosen workflow become the review fields, with their types, requirements and options.")}
      eyebrow={t("react.workflows.review.fields", "Review fields")}
      footer={<>
        <Button onClick={onClose} type="button" variant="secondary">{t("common.cancel", "Cancel")}</Button>
        <Button disabled={!picked} onClick={() => picked && onPick(picked)} type="button">{t("react.workflows.review.take_fields", "Take fields")}</Button>
      </>}
      onClose={onClose}
      open
      title={t("react.workflows.review.from_workflow", "Take fields from a workflow")}
    >
      <div className="space-y-3">
        <DsSelect
          ariaLabel={t("react.workflows.workflow_variant_2e2d5c56", "Workflow")}
          emptyLabel={query.isPending ? t("registry.workflow_detail.loading", "Loading…") : undefined}
          onChange={setChosen}
          options={rows.map((row) => ({ value: textOrEmpty(row.workflow_id || row.id), label: textOrEmpty(row.display_name) || textOrEmpty(row.workflow_id || row.id), description: textOrEmpty(row.description) }))}
          searchable
          value={chosen}
        />
        {query.isError ? <p className="fs-field-helper error" role="alert">{t("react.workflows.review.workflows_unavailable", "The workflows could not be loaded.")}</p> : null}
        {picked ? (
          inputs.length ? (
            <ul className="space-y-1 text-xs" data-testid="review-workflow-inputs">
              {inputs.map((input) => <li key={textOrEmpty(input.name)}><code className="fs-value-chip">{textOrEmpty(input.name)}</code>{" "}<span className="fs-ink-tertiary">{textOrEmpty(input.type)}{input.required ? ` · ${t("project_edit.fields.required", "Required")}` : ""}</span></li>)}
            </ul>
          ) : <p className="fs-field-helper">{t("react.workflows.review.workflow_no_inputs", "This workflow declares no inputs.")}</p>
        ) : null}
      </div>
    </DsDialog>
  );
}

/**
 * «Review fields» of an Approval step (ADR 0075): the typed values a person
 * checks and may correct before the workflow goes on. Each field is described
 * with the contract editor's controls; its initial value comes from an earlier
 * step or input, with matches by name and type suggested.
 */
export function ApprovalReviewFields({ ctx }: Readonly<{ ctx: Ctx }>) {
  const { t } = useI18n();
  const idPrefix = useId();
  const issueId = useId();
  const stored = objectOrEmpty(ctx.stepKindConfig).review;
  const basis = JSON.stringify(stored ?? null);
  const [local, setLocal] = useState<{ basis: string; draft: ReviewDraft; error: string }>(() => ({ basis, draft: reviewDraftFromConfig(stored), error: "" }));
  const [dialogOpen, setDialogOpen] = useState(false);
  let { draft, error } = local;
  if (local.basis !== basis) {
    draft = reviewDraftFromConfig(stored);
    error = "";
    setLocal({ basis, draft, error });
  }
  const onValidation = ctx.onStepInputValidationChange;
  useEffect(() => {
    onValidation?.(issueId, error);
    return () => onValidation?.(issueId, "");
  }, [onValidation, error, issueId]);
  const groups = ctx.availableWorkflowVariableSuggestions;
  const issues = reviewIssuesByPlace(ctx.stepValidationErrors, textOrEmpty(ctx.stepAuthoredId));

  function commit(next: ReviewDraft) {
    let payload: Record<string, unknown> | undefined;
    try {
      payload = reviewPayload(next, t);
    } catch (failure) {
      // An unfinished shape (JSON being typed) stays on screen until it reads.
      setLocal({ basis, draft: next, error: failure instanceof Error ? failure.message : String(failure) });
      return;
    }
    setLocal({ basis: JSON.stringify(payload ?? null), draft: next, error: "" });
    ctx.replaceStepKindConfigField("approval", "review", payload);
  }
  const updateField = (index: number, patch: Partial<ReviewFieldDraft>) => commit({ ...draft, fields: draft.fields.map((row, at) => at === index ? { ...row, ...patch } : row) });
  const typeOptions = localizedWORKFLOW_INTERFACE_FIELD_TYPES(t).filter((option) => (REVIEW_FIELD_TYPES as readonly string[]).includes(option.value));

  // Files the reviewer compares with: file inputs, and file results of earlier steps.
  const inputRows: Array<Record<string, unknown>> = Array.isArray(ctx.workflowInterfaceInputsDraft) ? ctx.workflowInterfaceInputsDraft : [];
  const originalChoices = [
    ...inputRows
      .filter((row) => textOrEmpty(row.name) && (row.type === "file" || (row.type === "array" && row.itemType === "file")))
      .map((row) => ({ value: `state.inputs.${textOrEmpty(row.name)}`, label: textOrEmpty(row.label) || textOrEmpty(row.name) })),
    ...(Array.isArray(groups) ? groups : []).flatMap((group: any) => (group.options ?? [])
      .filter((option: any) => ["file", "artifact_handle"].includes(String(option.valueType ?? "")) && !String(option.value).startsWith("state.inputs."))
      .map((option: any) => ({ value: String(option.value), label: String(option.label || option.value) }))),
  ].filter((choice, index, all) => all.findIndex((other) => other.value === choice.value) === index);

  return (
    <div className="space-y-3" data-testid="approval-review-fields">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.review.fields", "Review fields")}</p>
        <Button onClick={() => setDialogOpen(true)} size="sm" type="button" variant="secondary">{t("react.workflows.review.from_workflow", "Take fields from a workflow")}</Button>
      </div>
      <p className="fs-field-helper">{t("react.workflows.review.help", "Typed values the reviewer checks and may correct. On approval they become this step's outputs, for example for the Invoke step that follows.")}</p>
      {draft.fieldsFromWorkflow ? <p className="fs-field-helper">{t("react.workflows.review.from_workflow_hint", "Fields taken from {workflow}.", { workflow: draft.fieldsFromWorkflow })}</p> : null}
      <Issues issues={issues.general} />
      <ContractBlock
        addLabel={t("react.workflows.review.add_field", "Add field")}
        countText={t("react.workflows.contract_count_fields", "{count} fields", { count: draft.fields.length })}
        emptyText={t("react.workflows.review.no_fields", "No review fields: the reviewer approves or rejects without editing values.")}
        onAdd={() => commit({ ...draft, fields: [...draft.fields, { ...createWorkflowInterfaceFieldDraft({ type: "paragraph" }), editable: true, from: "" }] })}
        rows={draft.fields.map((row, index) => {
          const rowKey = row.draftId;
          const name = textOrEmpty(row.name);
          const controlId = `${idPrefix}-review-${rowKey}`;
          const rowIssues = issues.fields.get(index);
          const suggestions = sourceSuggestions(row, groups).filter((suggestion) => suggestion.value !== row.from);
          return {
            key: rowKey,
            name: name || t("react.workflows.review.unnamed_field", "Unnamed field"),
            unnamed: !name,
            required: row.required,
            type: row.type,
            summary: [row.editable ? t("react.workflows.review.editable", "Editable") : t("react.workflows.review.read_only", "Read only"), row.from].filter(Boolean).join(" · "),
            invalid: Boolean(rowIssues?.length),
            content: <>
              <Issues issues={rowIssues} />
              <ContractFieldControls
                controlId={controlId}
                controlName={`approval_review_${index}`}
                direction="input"
                labelPlaceholder={t("react.workflows.review.label_placeholder", "Total amount")}
                descriptionPlaceholder={t("react.workflows.review.description_placeholder", "Shown to the reviewer under the field")}
                nameId={`${controlId}-name`}
                nameMissing={!name && Boolean(row.nameTouched)}
                namePlaceholder="total"
                row={row}
                typeOptions={typeOptions}
                updateRow={(patch) => updateField(index, patch)}
              >
                <label className="flex min-h-8 items-center gap-2 text-xs text-[var(--fs-text-primary)]">
                  <input checked={row.editable} className="h-4 w-4 accent-[var(--fs-action-primary)]" id={`${controlId}-editable`} onChange={(event) => updateField(index, { editable: event.target.checked })} type="checkbox" />
                  <span>{t("react.workflows.review.editable_label", "The reviewer may correct it")}</span>
                </label>
                <div className="space-y-1">
                  <span className="block text-xs text-[var(--fs-text-primary)]">{t("react.workflows.review.source", "Initial value from")}</span>
                  {ctx.renderWorkflowVariableInput?.({
                    ariaLabel: t("react.workflows.review.source_of", "Initial value of {name}", { name: name || String(index + 1) }),
                    className: "fs-input w-full",
                    onChange: (value: string) => updateField(index, { from: value }),
                    panelId: `${controlId}-from`,
                    placeholder: t("react.workflows.review.source_placeholder", "An earlier step's result or an input; empty starts blank"),
                    value: row.from,
                  })}
                  {suggestions.length ? (
                    <p className="flex flex-wrap items-center gap-1 text-xs" data-testid={`review-source-suggestions-${index}`}>
                      <span className="fs-ink-tertiary">{t("react.workflows.review.suggested", "Matches:")}</span>
                      {suggestions.slice(0, 4).map((suggestion) => (
                        <Button key={suggestion.value} onClick={() => updateField(index, { from: suggestion.value })} size="sm" title={suggestion.value} type="button" variant="ghost">
                          {suggestion.label}{suggestion.fit === "unknown" ? ` (${t("react.workflows.review.type_unknown", "type unknown")})` : ""}
                        </Button>
                      ))}
                    </p>
                  ) : null}
                </div>
              </ContractFieldControls>
              <div>
                <Button onClick={() => commit({ ...draft, fields: draft.fields.filter((_, at) => at !== index) })} size="sm" type="button" variant="ghost-danger">
                  {t("react.workflows.review.remove_field", "Remove field")}
                </Button>
              </div>
            </>,
          };
        })}
        testId="approval-review-field-list"
        title={t("react.workflows.review.fields", "Review fields")}
      />
      <div className="space-y-1">
        <span className="block text-xs text-[var(--fs-text-secondary)]">{t("react.workflows.review.original", "Original shown beside the fields")}</span>
        {originalChoices.length ? (
          <DsSelect
            ariaLabel={t("react.workflows.review.original", "Original shown beside the fields")}
            onChange={(original) => commit({ ...draft, original })}
            options={[{ value: "", label: t("react.workflows.review.no_original", "No original") }, ...originalChoices, ...(draft.original && !originalChoices.some((choice) => choice.value === draft.original) ? [{ value: draft.original, label: draft.original }] : [])]}
            value={draft.original}
          />
        ) : ctx.renderWorkflowVariableInput?.({
          ariaLabel: t("react.workflows.review.original", "Original shown beside the fields"),
          className: "fs-input w-full",
          onChange: (original: string) => commit({ ...draft, original }),
          panelId: `${idPrefix}-review-original`,
          placeholder: "state.inputs.files",
          value: draft.original,
        })}
        <p className="fs-field-helper">{t("react.workflows.review.original_help", "A file input or a file of an earlier step; several files are paged.")}</p>
        <Issues issues={issues.original} />
      </div>
      {error ? <p className="fs-field-helper error" id={issueId} role="alert">{error}</p> : null}
      {dialogOpen ? (
        <FieldsFromWorkflowDialog
          current={draft.fieldsFromWorkflow}
          onClose={() => setDialogOpen(false)}
          onPick={(workflow) => {
            setDialogOpen(false);
            const inputs = Array.isArray(workflow.expected_inputs) ? workflow.expected_inputs.map(objectOrEmpty) : [];
            commit({ ...draft, fieldsFromWorkflow: textOrEmpty(workflow.workflow_id || workflow.id), fields: fieldsFromWorkflowInputs(inputs, draft.fields, groups) });
          }}
          workflowId={textOrEmpty(ctx.workflowId)}
        />
      ) : null}
    </div>
  );
}

