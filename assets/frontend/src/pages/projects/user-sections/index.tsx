import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router";

import {
  DsAlert,
  DsCard,
  DsPageHeader,
  DsTable,
  PageContainer,
  Pagination,
  type DsTableColumn,
} from "@/components/shared/design-system";
import { LocalizedStatusBadge } from "@/components/shared/localized-status-badge";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { WorkflowInputFields } from "@/components/workflow-inputs/workflow-input-fields";
import { exceedsRunUploadLimits, useRunUploadLimits } from "@/lib/auth/run-upload-limits";
import { useHasCapability } from "@/lib/auth/capabilities";
import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";
import { formatDateTime } from "@/lib/time/date-time-format";
import { readWorkflowRunFile, runBuilderWorkflow } from "@/pages/workflows/shared/run-api";

import { fetchSectionJobs, fetchSectionProject } from "./api";
import { buildInputFields } from "./schema";
import { prepareSectionRuns, runWithConcurrency, type PreparedRun } from "./submission";
import type { ProjectUserSectionView, SectionColumnConfig, SectionJobRow } from "./types";

/** Requests in flight at once for a For each submission; not a server capacity. */
export const SECTION_RUN_CONCURRENCY = 3;
const PAGE_SIZE = 25;
const ACTIVE_STATUSES = new Set(["pending", "queued", "running", "in_progress", "waiting_approval"]);

type ItemState = { run: PreparedRun; state: "ready" | "sending" | "accepted" | "failed"; jobId?: string; error?: string };

function statusTone(status: string) {
  if (["completed", "succeeded", "success"].includes(status)) return "green" as const;
  if (["failed", "cancelled", "stuck", "orphaned", "max_iterations"].includes(status)) return "danger" as const;
  if (ACTIVE_STATUSES.has(status)) return "orange" as const;
  return "neutral" as const;
}

function newSubmissionId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function displayValue(value: unknown, format: string | undefined): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) {
    return format === "count" ? String(value.length) : value.map((item) => displayValue(item, undefined)).join(", ");
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.filename === "string") return record.filename;
    if (typeof record.id === "string") return record.id;
    return JSON.stringify(record);
  }
  if ((format === "date" || format === "datetime") && typeof value === "string") return formatDateTime(value);
  if (format === "currency" && typeof value === "number") return value.toFixed(2);
  return String(value);
}

function defaultColumns(): SectionColumnConfig[] {
  return [
    { id: "created_at", source: "job", path: "created_at", format: "datetime" },
    { id: "status", source: "job", path: "status", format: "status" },
    { id: "submitted_by", source: "job", path: "submitted_by" },
  ];
}

function SectionForm({
  projectId,
  section,
  onSubmitted,
}: Readonly<{ projectId: string; section: ProjectUserSectionView; onSubmitted: () => void }>) {
  const { t } = useI18n();
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

  const send = async (runs: ItemState[]) => {
    const current = ++generation.current;
    setBusy(true);
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
          update(item.run.itemId, { state: "failed", error: error instanceof Error ? error.message : failureText });
          throw error;
        }
      }),
      SECTION_RUN_CONCURRENCY,
      // Leaving the page stops sending what is not sent yet; accepted runs stay.
      () => generation.current !== current,
    );
    setBusy(false);
    onSubmitted();
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

  const failed = items.filter((item) => item.state === "failed");
  const accepted = items.filter((item) => item.state === "accepted").length;

  return (
    <DsCard title={t("react.user_sections.new_submission", "New submission")}>
      <form
        className="grid gap-4 md:grid-cols-2"
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
        {problem ? (
          <div className="md:col-span-2">
            <DsAlert title={problem} tone="warning" />
          </div>
        ) : null}
        <div className="flex flex-wrap items-center gap-2 md:col-span-2">
          <button className="fs-button primary" disabled={busy} type="submit">
            {t("react.user_sections.send", "Send")}
          </button>
          {failed.length && !busy ? (
            <button className="fs-button" onClick={() => void send(failed)} type="button">
              {t("react.user_sections.retry_failed", "Retry {count} failed", { count: String(failed.length) })}
            </button>
          ) : null}
          {items.length ? (
            <span className="text-sm text-muted" role="status">
              {t("react.user_sections.submission_progress", "{accepted} of {total} accepted", {
                accepted: String(accepted),
                total: String(items.length),
              })}
            </span>
          ) : null}
        </div>
        {failed.length ? (
          <ul className="md:col-span-2 text-sm">
            {failed.map((item) => (
              <li key={item.run.itemId}>
                {item.run.label}: {item.error}
              </li>
            ))}
          </ul>
        ) : null}
      </form>
    </DsCard>
  );
}

function SectionHistory({ projectId, section }: Readonly<{ projectId: string; section: ProjectUserSectionView }>) {
  const { t } = useI18n();
  const [page, setPage] = useState(1);
  const params = { page, perPage: PAGE_SIZE };
  const jobs = useQuery({
    queryKey: queryKeys.sectionJobs(projectId, section.id, params),
    queryFn: () => fetchSectionJobs(projectId, section.id, params),
    // Results appear as runs finish.
    refetchInterval: (query) =>
      (query.state.data?.jobs ?? []).some((row) => ACTIVE_STATUSES.has(String(row.status ?? ""))) ? 5000 : false,
  });
  const configured = section.columns.length ? [...section.columns].sort((a, b) => (a.order ?? 0) - (b.order ?? 0)) : defaultColumns();
  const columns: DsTableColumn<SectionJobRow>[] = configured.map((column) => ({
    key: column.id,
    header: column.label || column.path,
    cell: (row) => {
      if (column.source === "job" && column.path === "status") {
        const status = String(row.status ?? "");
        return (
          <span className="flex flex-col gap-1">
            <LocalizedStatusBadge tone={statusTone(status)} value={status} />
            {statusTone(status) === "danger" && row.last_error ? <span className="text-sm text-muted">{row.last_error}</span> : null}
          </span>
        );
      }
      const value =
        column.source === "job" && column.path === "submitted_by"
          ? row.submitted_by?.id
          : column.source === "job" && column.path === "created_at" && !section.columns.length
            ? row.queued_at ?? row.started_at
            : row.section_values?.[column.id];
      return displayValue(value, column.format);
    },
  }));
  columns.push({
    key: "open",
    header: "",
    ariaLabel: t("react.user_sections.open_run", "Open run"),
    cell: (row) => <Link to={`/jobs/${encodeURIComponent(row.job_id)}`}>{t("react.user_sections.open", "Open")}</Link>,
  });
  const data = jobs.data;
  return (
    <DsCard title={t("react.user_sections.history", "History")}>
      <DsTable
        ariaLabel={t("react.user_sections.history", "History")}
        busy={jobs.isFetching}
        columns={columns}
        rowKey={(row) => row.job_id}
        rows={data?.jobs ?? []}
        state={
          jobs.isError ? (
            <DsAlert title={t("react.user_sections.history_failed", "History could not be loaded.")} tone="danger" />
          ) : data && !data.jobs.length ? (
            <p>{t("react.user_sections.history_empty", "No runs yet.")}</p>
          ) : null
        }
      />
      {data && data.total > PAGE_SIZE ? (
        <Pagination onPageChange={setPage} page={data.page} perPage={PAGE_SIZE} showPerPage={false} total={data.total} />
      ) : null}
    </DsCard>
  );
}

export function ProjectSectionPage() {
  const { t } = useI18n();
  const { projectId = "", sectionId = "" } = useParams();
  const queryClient = useQueryClient();
  const canRun = useHasCapability("run_workflows");
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
  return (
    <PageContainer width="wide">
      <DsPageHeader
        description={section.description || project.data?.name || ""}
        eyebrow={project.data?.name}
        title={section.title}
      />
      <div className="flex flex-col gap-4">
        {canRun && !section.runtime.runnable ? (
          <DsAlert title={t("react.user_sections.blocked", "New submissions are paused")} tone="warning">
            {blocked.length ? blocked.map((issue) => <p key={`${issue.path}-${issue.code}`}>{issue.message}</p>) : (
              <p>{t("react.user_sections.not_published", "An administrator has not confirmed this section's real effects yet.")}</p>
            )}
          </DsAlert>
        ) : null}
        {canRun && section.runtime.runnable ? (
          <SectionForm
            onSubmitted={() => void queryClient.invalidateQueries({ queryKey: ["projects", "sections", projectId, section.id] })}
            projectId={projectId}
            section={section}
          />
        ) : null}
        <SectionHistory projectId={projectId} section={section} />
      </div>
    </PageContainer>
  );
}
