import { useId, useMemo, useRef, useState } from "react";

import { DsAlert, DsCheckbox, DsDialog } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { initialFormValues, type FormFiles, type FormProblem, type FormValues } from "@/components/workflow-inputs/form-field";
import { WorkflowInputFields } from "@/components/workflow-inputs/workflow-input-fields";
import { useRunUploadLimits } from "@/lib/auth/run-upload-limits";
import { useI18n } from "@/lib/i18n/useI18n";
import { readWorkflowRunFile, runBuilderWorkflow } from "@/pages/workflows/shared/run-api";

import { describeRunError } from "./run-errors";
import {
  OPERATOR_RUN_CONCURRENCY,
  prepareOperatorRuns,
  runBreaksUploadLimits,
  runWithConcurrency,
  type PreparedRun,
  type SubmissionProblem,
} from "./submission";
import type { OperatorSectionDetail } from "./types";

type ItemState = { run: PreparedRun; state: "ready" | "sending" | "accepted" | "failed"; jobId?: string; error?: string };

function withoutKey(record: Record<string, string>, key: string): Record<string, string> {
  if (!(key in record)) return record;
  const next = { ...record };
  delete next[key];
  return next;
}

function newSubmissionId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * What "Send again" fills the form with: an earlier job's values and its files.
 * `missing` names the files that could not be loaded, `expected` how many files
 * each file input had, so a shorter set is never sent without the operator's say.
 */
export type SubmissionPrefill = {
  values: FormValues;
  files: FormFiles;
  missing?: { field: string; label: string; filename: string }[];
  expected?: Record<string, number>;
};

/**
 * The New dialog of a section (ADR 0075): the form the admin configured, sent
 * as one run (Bulk) or one run per item (For each) with limited concurrency,
 * "N of M accepted" and a retry of the failed ones under their own keys.
 * With `prefill` it is "Send again": the same form, filled with an earlier
 * job's values and files, sent as a new submission.
 */
export function OperatorSubmitDialog({
  section,
  open,
  onClose,
  onSent,
  prefill,
}: Readonly<{
  section: OperatorSectionDetail;
  open: boolean;
  onClose: () => void;
  /** The jobs this send started. */
  onSent: (jobIds: string[]) => void;
  prefill?: SubmissionPrefill;
}>) {
  const { t } = useI18n();
  const formId = useId();
  const uploadLimits = useRunUploadLimits();
  const operator = section.operator;
  const fields = operator.form_fields;
  const submission = operator.settings?.submission;
  const initialValues = useMemo(
    () => ({ ...initialFormValues(fields, operator.input_schema), ...prefill?.values }),
    [fields, operator.input_schema, prefill],
  );
  const initialFiles = useMemo<FormFiles>(() => prefill?.files ?? {}, [prefill]);
  const [values, setValues] = useState<FormValues>(initialValues);
  const [files, setFiles] = useState<FormFiles>(initialFiles);
  const [items, setItems] = useState<ItemState[]>([]);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<{ title: string; reasons: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [sendShorter, setSendShorter] = useState(false);
  const generation = useRef(0);
  const target = submission?.mode === "foreach" ? submission.target_input ?? "" : "";
  const multipleFiles = useMemo(() => new Set(target ? [target] : []), [target]);
  const fieldNames = useMemo(() => fields.map((field) => field.name), [fields]);
  const failureText = t("react.operator.send_failed", "Could not send");
  const failed = items.filter((item) => item.state === "failed");
  const accepted = items.filter((item) => item.state === "accepted").length;
  // Once a run of this batch is accepted, its values are frozen: Send would start a
  // new submission id and send the accepted items again. Only the failed ones are
  // retried under their own keys, or the operator starts a new submission.
  const batchLocked = accepted > 0;
  const labelOf = (name: string) => fields.find((field) => field.name === name)?.label ?? name;
  // Files of the earlier job that could not be loaded, while their input still has fewer files than it had.
  const unresolvedMissing = (prefill?.missing ?? []).filter(
    (entry) => (files[entry.field]?.length ?? 0) < (prefill?.expected?.[entry.field] ?? 0),
  );

  const reset = () => {
    setValues(initialValues);
    setFiles(initialFiles);
    setItems([]);
    setFieldErrors({});
    setProblem(null);
    setSendShorter(false);
  };

  const problemText = (entry: SubmissionProblem | FormProblem): string => {
    switch (entry.code) {
      case "required":
        return t("react.operator.field_required", "Fill in {field}.", { field: labelOf(entry.field) });
      case "min_items":
        return t("react.operator.field_min_items", "Add at least {count}.", { count: String(entry.limit) });
      case "max_items":
        return t("react.operator.field_max_items", "Add at most {count}.", { count: String(entry.limit) });
      case "invalid_number":
        return t("react.operator.field_invalid_number", "Enter a number.");
      case "row_required":
      case "row_invalid_number": {
        const sub = fields.find((field) => field.name === entry.field)?.sub_fields?.find((item) => item.name === entry.subField);
        const vars = { field: sub?.label ?? entry.subField, row: String(entry.row) };
        return entry.code === "row_required"
          ? t("react.operator.row_field_required", "Fill in {field} in row {row}.", vars)
          : t("react.operator.row_field_invalid_number", "Enter a number in {field} in row {row}.", vars);
      }
      case "foreach_target_unavailable":
        return t("react.operator.foreach_target_unavailable", "This form cannot be sent: the input it sends one by one is not shown. Ask an admin to check the section.");
      default:
        return t("react.operator.nothing_to_send", "Add at least one item to send.");
    }
  };

  const send = async (runs: ItemState[]) => {
    const current = ++generation.current;
    setBusy(true);
    setProblem(null);
    let failures = 0;
    const started: string[] = [];
    const single = runs.length === 1 && items.length <= 1;
    const update = (itemId: string, patch: Partial<ItemState>) =>
      setItems((previous) => previous.map((item) => (item.run.itemId === itemId ? { ...item, ...patch } : item)));
    await runWithConcurrency(
      runs.map((item) => async () => {
        update(item.run.itemId, { state: "sending", error: undefined });
        try {
          const uploads = await Promise.all(item.run.files.map(({ inputName, file }) => readWorkflowRunFile(inputName, file, failureText)));
          const summary = item.run.label ? `${section.title}: ${item.run.label}` : section.title;
          const result = await runBuilderWorkflow(
            section.workflow_id,
            {
              project_id: section.project_id,
              expected_revision: section.workflow_revision,
              // The admin acknowledged real effects when publishing; the operator
              // confirms them by pressing Send.
              acknowledged: true,
              summary: summary.slice(0, 10000),
              inputs: item.run.inputs,
              files: uploads,
            },
            item.run.idempotencyKey,
          );
          update(item.run.itemId, { state: "accepted", jobId: result.job_id });
          started.push(result.job_id);
        } catch (error) {
          failures += 1;
          const view = describeRunError(error, fieldNames, failureText);
          update(item.run.itemId, { state: "failed", error: view.message });
          if (view.field) {
            setFieldErrors((currentErrors) => ({ ...currentErrors, [view.field]: view.message }));
          } else if (single || view.reasons.length) {
            setProblem({ title: view.message, reasons: view.reasons });
          }
          throw error;
        }
      }),
      OPERATOR_RUN_CONCURRENCY,
      // Closing the dialog stops what is not sent yet; accepted runs stay.
      () => generation.current !== current,
    );
    if (generation.current !== current) return;
    setBusy(false);
    onSent(started);
    if (!failures) {
      reset();
      onClose();
    }
  };

  const submit = () => {
    if (batchLocked) return;
    setProblem(null);
    setFieldErrors({});
    if (unresolvedMissing.length && !sendShorter) {
      setProblem({ title: t("react.operator_job.resend_missing_block", "Add the missing files again, or confirm sending without them."), reasons: [] });
      return;
    }
    const { runs, problems } = prepareOperatorRuns({
      keyPrefix: `${section.project_id}:${section.workflow_id}`,
      submission,
      fields,
      values,
      files,
      submissionId: newSubmissionId(),
    });
    const unavailable = problems.find((entry) => entry.code === "foreach_target_unavailable");
    if (unavailable) {
      // The target is not on the form, so there is no field to mark; nothing is sent.
      setProblem({ title: problemText(unavailable), reasons: [] });
      return;
    }
    if (problems.length) {
      const errors: Record<string, string> = {};
      for (const entry of problems) errors[entry.field] ??= problemText(entry);
      setFieldErrors(errors);
      setProblem({ title: t("react.operator.check_the_form", "Check the highlighted fields."), reasons: [] });
      return;
    }
    if (runs.some((run) => runBreaksUploadLimits(run, uploadLimits))) {
      setProblem({
        title: t("react.operator.files_too_large", "A file is empty, or the files are more or larger than this installation accepts."),
        reasons: uploadLimits
          ? [
              t("react.operator.upload_limits", "Up to {files} files, each up to {size} MB.", {
                files: String(uploadLimits.max_files),
                size: String(Math.floor(uploadLimits.max_file_bytes / (1024 * 1024))),
              }),
            ]
          : [],
      });
      return;
    }
    const next = runs.map((run) => ({ run, state: "ready" as const }));
    setItems(next);
    void send(next);
  };

  return (
    <DsDialog
      busy={busy}
      closeLabel={t("react.operator.close_form", "Close the form")}
      description={
        target
          ? t("react.operator.foreach_hint", "Each file or item becomes its own run in the list.")
          : t("react.operator.bulk_hint", "Everything you send becomes one run in the list.")
      }
      eyebrow={section.title}
      footer={
        <>
          {items.length > 1 ? (
            <span className="text-sm text-muted" role="status">
              {t("react.operator.submission_progress", "{accepted} of {total} accepted", {
                accepted: String(accepted),
                total: String(items.length),
              })}
            </span>
          ) : null}
          {failed.length && !busy ? (
            <button className="fs-button" onClick={() => void send(failed)} type="button">
              {t("react.operator.retry_failed", "Retry {count} failed", { count: String(failed.length) })}
            </button>
          ) : null}
          <button
            className="fs-button"
            onClick={() => {
              generation.current += 1;
              setBusy(false);
              onClose();
            }}
            type="button"
          >
            {t("common.cancel", "Cancel")}
          </button>
          {batchLocked ? (
            <Button disabled={busy} onClick={reset} type="button" variant="primary">
              {t("react.operator.new_submission", "New submission")}
            </Button>
          ) : (
            <Button busy={busy} busyLabel={t("react.operator.sending_now", "Sending…")} form={formId} type="submit" variant="primary">
              {t("react.operator.send", "Send")}
            </Button>
          )}
        </>
      }
      onClose={() => {
        generation.current += 1;
        setBusy(false);
        onClose();
      }}
      open={open}
      title={prefill ? t("react.operator_job.send_again", "Send again") : t("react.operator.new_submission", "New submission")}
    >
      <form
        className="fs-dialog-form"
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy && !batchLocked) submit();
        }}
      >
        {unresolvedMissing.length ? (
          <DsAlert title={t("react.operator_job.resend_missing", "These files of the earlier job could not be loaded:")} tone="warning">
            <ul data-testid="resend-missing-files">
              {unresolvedMissing.map((entry, index) => (
                <li key={`${entry.field}-${entry.filename}-${index}`}>
                  {entry.label}: {entry.filename || "—"}
                </li>
              ))}
            </ul>
            <DsCheckbox checked={sendShorter} onChange={setSendShorter}>
              {t("react.operator_job.resend_without_missing", "Send without these files")}
            </DsCheckbox>
          </DsAlert>
        ) : null}
        {batchLocked ? (
          <p className="text-sm text-muted">
            {t("react.operator.batch_locked", "Part of this submission was accepted. Retry the failed items, or start a new submission.")}
          </p>
        ) : null}
        <WorkflowInputFields
          disabled={busy || batchLocked}
          errors={fieldErrors}
          fields={fields}
          files={files}
          multipleFiles={multipleFiles}
          onChange={(name, value) => {
            setValues((current) => ({ ...current, [name]: value }));
            setFieldErrors((current) => withoutKey(current, name));
          }}
          onFiles={(name, selected) => {
            setFiles((current) => ({ ...current, [name]: selected }));
            setFieldErrors((current) => withoutKey(current, name));
          }}
          values={values}
        />
        {problem ? (
          <DsAlert title={problem.title} tone="warning">
            {problem.reasons.length ? (
              <ul>
                {problem.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            ) : null}
          </DsAlert>
        ) : null}
        {failed.length && items.length > 1 ? (
          <ul className="text-sm" aria-label={t("react.operator.failed_items", "Not sent")}>
            {failed.map((item) => (
              <li key={item.run.itemId}>
                {item.run.label}: {item.error}
              </li>
            ))}
          </ul>
        ) : null}
      </form>
    </DsDialog>
  );
}
