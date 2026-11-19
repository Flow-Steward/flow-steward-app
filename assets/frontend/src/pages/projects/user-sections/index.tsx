import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useParams } from "react-router";

import { DsAlert, DsDialog, PageContainer } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { Button } from "@/components/ui/button";
import { WorkflowInputFields } from "@/components/workflow-inputs/workflow-input-fields";
import { exceedsRunUploadLimits, useRunUploadLimits } from "@/lib/auth/run-upload-limits";
import { useHasCapability } from "@/lib/auth/capabilities";
import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";
import { formatDateTime } from "@/lib/time/date-time-format";
import { JobsListPage, type JobsListScope } from "@/pages/jobs/list";
import { readWorkflowRunFile, runBuilderWorkflow } from "@/pages/workflows/shared/run-api";

import { fetchSectionProject } from "./api";
import { buildInputFields } from "./schema";
import { prepareSectionRuns, runWithConcurrency, type PreparedRun } from "./submission";
import type { ProjectUserSectionView, SectionColumnConfig } from "./types";

/** Requests in flight at once for a For each submission; not a server capacity. */
export const SECTION_RUN_CONCURRENCY = 3;

type ItemState = { run: PreparedRun; state: "ready" | "sending" | "accepted" | "failed"; jobId?: string; error?: string };

function newSubmissionId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** The jobs list refreshes itself through its published bridge after a submission. */
function refreshJobsList(): void {
  const bridge = (globalThis as unknown as { refreshJobs?: () => Promise<void> }).refreshJobs;
  void bridge?.();
}

function fileLink(jobId: string, value: Record<string, unknown>): ReactNode {
  const name = typeof value.filename === "string" ? value.filename : "";
  const artifactId = typeof value.artifact_id === "string" ? value.artifact_id : "";
  if (!artifactId) return name;
  return (
    <a
      download={name || undefined}
      href={`/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}`}
      onClick={(event) => event.stopPropagation()}
    >
      {name || artifactId}
    </a>
  );
}

/** One configured column's value, as the admin chose to show it. */
export function sectionCell(column: SectionColumnConfig, row: Record<string, unknown>): ReactNode {
  const values = row.section_values && typeof row.section_values === "object" ? (row.section_values as Record<string, unknown>) : {};
  const value = values[column.id];
  const jobId = String(row.job_id ?? row.id ?? "");
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) {
    if (column.format === "count") return String(value.length);
    return value.map((item, index) => (
      <span key={index}>
        {index ? ", " : ""}
        {item && typeof item === "object" ? fileLink(jobId, item as Record<string, unknown>) : String(item)}
      </span>
    ));
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.filename === "string") return fileLink(jobId, record);
    if (typeof record.id === "string") return record.id;
    return JSON.stringify(record);
  }
  if ((column.format === "date" || column.format === "datetime") && typeof value === "string") return formatDateTime(value);
  if (column.format === "currency" && typeof value === "number") return value.toFixed(2);
  return String(value);
}

/** The New dialog: the section's form; nothing on the page moves for it. */
function SectionSubmitDialog({
  projectId,
  section,
  open,
  onClose,
}: Readonly<{ projectId: string; section: ProjectUserSectionView; open: boolean; onClose: () => void }>) {
  const { t } = useI18n();
  const formId = useId();
  const uploadLimits = useRunUploadLimits();
  const fields = useMemo(() => buildInputFields(section.runtime.input_schema, section), [section]);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [files, setFiles] = useState<Record<string, File[]>>({});
  const [items, setItems] = useState<ItemState[]>([]);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const target = section.submission.mode === "foreach" ? section.submission.target_input ?? "" : "";
  const multipleFiles = useMemo(() => new Set(target ? [target] : []), [target]);
  const failureText = t("react.user_sections.send_failed", "Could not send");
  const failed = items.filter((item) => item.state === "failed");
  const accepted = items.filter((item) => item.state === "accepted").length;

  const reset = () => {
    setValues({});
    setFiles({});
    setItems([]);
    setProblem("");
  };

  const send = async (runs: ItemState[]) => {
    const current = ++generation.current;
    setBusy(true);
    let failures = 0;
    const update = (itemId: string, patch: Partial<ItemState>) =>
      setItems((previous) => previous.map((item) => (item.run.itemId === itemId ? { ...item, ...patch } : item)));
    await runWithConcurrency(
      runs.map((item) => async () => {
        update(item.run.itemId, { state: "sending", error: undefined });
        try {
          const uploads = await Promise.all(
            item.run.files.map(({ inputName, file }) => readWorkflowRunFile(inputName, file, failureText)),
          );
          const result = await runBuilderWorkflow(
            section.workflow_id,
            {
              project_id: projectId,
              expected_revision: section.runtime.workflow_revision,
              // The admin acknowledged real effects when publishing the section;
              // the operator confirms them by pressing Send.
              acknowledged: true,
              summary: `${section.title}: ${item.run.label}`.slice(0, 10000),
              inputs: item.run.inputs,
              files: uploads,
            },
            item.run.idempotencyKey,
          );
          update(item.run.itemId, { state: "accepted", jobId: result.job_id });
        } catch (error) {
          failures += 1;
          update(item.run.itemId, { state: "failed", error: error instanceof Error ? error.message : failureText });
          throw error;
        }
      }),
      SECTION_RUN_CONCURRENCY,
      // Closing the dialog stops what is not sent yet; accepted runs stay.
      () => generation.current !== current,
    );
    setBusy(false);
    refreshJobsList();
    if (!failures && generation.current === current) {
      reset();
      onClose();
    }
  };

  const submit = () => {
    setProblem("");
    const sizes = Object.values(files).flat().map((file) => file.size);
    if (sizes.some((size) => !size) || exceedsRunUploadLimits(target ? [Math.max(0, ...sizes)] : sizes, uploadLimits)) {
      setProblem(t("react.user_sections.files_too_large", "A file is empty or larger than this installation allows."));
      return;
    }
    const { runs, problems } = prepareSectionRuns({ section, fields, values, files, submissionId: newSubmissionId() });
    if (problems.length) {
      const first = problems[0];
      setProblem(
        first.code === "required"
          ? t("react.user_sections.field_required", "Fill in {field}.", {
              field: fields.find((field) => field.name === first.field)?.label ?? first.field,
            })
          : t("react.user_sections.nothing_to_send", "Add at least one item to send."),
      );
      return;
    }
    const next = runs.map((run) => ({ run, state: "ready" as const }));
    setItems(next);
    void send(next);
  };

  return (
    <DsDialog
      busy={busy}
      closeLabel={t("react.user_sections.close_form", "Close the form")}
      description={section.description || t("react.user_sections.new_submission_hint", "Each file or item becomes its own run in the list.")}
      eyebrow={section.title}
      footer={
        <>
          {items.length ? (
            <span className="text-sm text-muted" role="status">
              {t("react.user_sections.submission_progress", "{accepted} of {total} accepted", {
                accepted: String(accepted),
                total: String(items.length),
              })}
            </span>
          ) : null}
          {failed.length && !busy ? (
            <button className="fs-button" onClick={() => void send(failed)} type="button">
              {t("react.user_sections.retry_failed", "Retry {count} failed", { count: String(failed.length) })}
            </button>
          ) : null}
          <button className="fs-button" disabled={busy} onClick={onClose} type="button">
            {t("extensions.repeater.cancel", "Cancel")}
          </button>
          <Button busy={busy} busyLabel={t("react.user_sections.sending_now", "Sending…")} form={formId} type="submit" variant="primary">
            {t("react.user_sections.send", "Send")}
          </Button>
        </>
      }
      onClose={() => {
        generation.current += 1;
        onClose();
      }}
      open={open}
      title={t("react.user_sections.new_submission", "New submission")}
    >
      <form
        className="fs-dialog-form"
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <WorkflowInputFields
          disabled={busy}
          fields={fields}
          files={files}
          multipleFiles={multipleFiles}
          onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
          onFiles={(name, selected) => setFiles((current) => ({ ...current, [name]: selected }))}
          values={values}
        />
        {problem ? <DsAlert title={problem} tone="warning" /> : null}
        {failed.length ? (
          <ul className="text-sm">
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

/**
 * A user section (ADR 0075): the Jobs list with the section's project and
 * workflow fixed in the background, the columns the admin chose, and a New
 * button whose dialog holds the form.
 */
export function ProjectSectionPage() {
  const { t } = useI18n();
  const { projectId = "", sectionId = "" } = useParams();
  const canRun = useHasCapability("run_workflows");
  const [dialogOpen, setDialogOpen] = useState(false);
  const project = useQuery({
    queryKey: queryKeys.projectSections(projectId),
    queryFn: () => fetchSectionProject(projectId),
    enabled: Boolean(projectId),
  });
  if (project.isLoading) {
    return <PageLoadingState label={t("react.user_sections.loading", "Opening the section")} />;
  }
  const section = project.data?.user_sections.find((item) => item.id === sectionId);
  if (!section) {
    return (
      <PageContainer width="wide">
        <DsAlert title={t("react.user_sections.not_found", "This section is not available.")} tone="warning" />
      </PageContainer>
    );
  }
  const blocked = section.runtime.issues.filter((issue) => (issue.severity ?? "error") === "error");
  const scope: JobsListScope = {
    key: `${projectId}:${section.id}`,
    projectId,
    workflowId: section.workflow_id,
    sectionId: section.id,
    title: section.title,
    description: section.description || "",
    eyebrow: project.data?.name ?? "",
    action: canRun ? (
      <button className="fs-button primary" disabled={!section.runtime.runnable} onClick={() => setDialogOpen(true)} type="button">
        <Plus aria-hidden="true" />
        {t("react.user_sections.new", "New")}
      </button>
    ) : null,
    notice:
      canRun && !section.runtime.runnable ? (
        <DsAlert title={t("react.user_sections.blocked", "New submissions are paused")} tone="warning">
          {blocked.length ? (
            blocked.map((issue) => <p key={`${issue.path}-${issue.code}`}>{issue.message}</p>)
          ) : (
            <p>{t("react.user_sections.not_published", "An administrator has not confirmed this section's real effects yet.")}</p>
          )}
        </DsAlert>
      ) : null,
    columns: [...section.columns]
      .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
      .map((column) => ({
        key: column.id,
        label: column.label || column.path,
        cell: (row: Record<string, unknown>) => sectionCell(column, row),
      })),
  };
  return (
    <>
      <JobsListPage key={scope.key} scope={scope} />
      {canRun && section.runtime.runnable ? (
        <SectionSubmitDialog onClose={() => setDialogOpen(false)} open={dialogOpen} projectId={projectId} section={section} />
      ) : null}
    </>
  );
}
