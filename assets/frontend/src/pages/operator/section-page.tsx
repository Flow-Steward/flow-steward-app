import { useQuery } from "@tanstack/react-query";
import { Settings2, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useParams } from "react-router";

import { DsAlert, DsCheckbox, PageContainer } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { DsPopover } from "@/components/shared/popover";
import { useHasCapability } from "@/lib/auth/capabilities";
import { useWorkflowLimitAllows } from "@/lib/auth/project-permissions";
import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";
import { JobsListPage, type JobsListScope } from "@/pages/jobs/list";

import { fetchOperatorSection } from "./api";
import { hiddenColumnsKey, readHiddenColumns, writeHiddenColumns } from "./column-visibility";
import { OperatorCell } from "./section-cell";
import { operatorJobPath, rowContext } from "./queue";
import { operatorFilterFields, operatorFiltersParam } from "./section-filters";
import { OperatorSubmitDialog } from "./submit-dialog";
import type { OperatorSectionDetail } from "./types";
import "./operator.css";

/** Show or hide columns for this person; at least one column stays. */
function ColumnPicker({
  columns,
  hidden,
  onChange,
}: Readonly<{ columns: Array<{ key: string; label: string }>; hidden: ReadonlySet<string>; onChange: (hidden: string[]) => void }>) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const shownCount = columns.filter((column) => !hidden.has(column.key)).length;
  return (
    <DsPopover
      label={t("react.operator.columns", "Columns")}
      onOpenChange={setOpen}
      open={open}
      portal
      trigger={(props) => (
        <button {...props} className="fs-filter-chip" type="button">
          <Settings2 aria-hidden="true" />
          {t("react.operator.columns", "Columns")}
        </button>
      )}
    >
      <div className="fs-operator-column-picker" role="group" aria-label={t("react.operator.shown_columns", "Shown columns")}>
        {columns.map((column) => {
          const checked = !hidden.has(column.key);
          return (
            <DsCheckbox
              checked={checked}
              disabled={checked && shownCount <= 1}
              key={column.key}
              onChange={(next) => onChange(columns.map((item) => item.key).filter((key) => (key === column.key ? !next : hidden.has(key))))}
            >
              {column.label}
            </DsCheckbox>
          );
        })}
      </div>
    </DsPopover>
  );
}

function SectionList({ section }: Readonly<{ section: OperatorSectionDetail }>) {
  const { t } = useI18n();
  const canRun = useHasCapability("run_workflows");
  // ADR 0076 §8b: a limited person runs here only where the workflow's place allows it.
  const runAllowed = useWorkflowLimitAllows(section.project_id, section.workflow_id, "workflows.run");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogGeneration, setDialogGeneration] = useState(0);
  const storageKey = hiddenColumnsKey(section.project_id, section.workflow_id);
  const [hidden, setHidden] = useState<string[]>(() => readHiddenColumns(storageKey));
  const operator = section.operator;
  const runnable = section.runnable && operator.runnable;
  const configuredColumns = useMemo(() => operator.settings?.list?.columns ?? [], [operator.settings]);
  const filterFields = useMemo(() => operatorFilterFields(operator.filters ?? [], t), [operator.filters, t]);
  const pickable = useMemo(
    () => [
      { key: "status", label: t("account_settings.card.status", "Status") },
      { key: "_started_display", label: t("project_detail.jobs.grid.started", "Started") },
      { key: "_completed_display", label: t("jobs.detail.step.completed", "Completed") },
      { key: "_duration", label: t("jobs.columns.duration", "Duration") },
      ...configuredColumns.map((column) => ({ key: column.id, label: column.label || column.path })),
    ],
    [configuredColumns, t],
  );
  const hiddenSet = useMemo(() => new Set(hidden), [hidden]);
  const reasons = section.blocked_reasons?.length
    ? section.blocked_reasons
    : (operator.issues ?? []).filter((issue) => (issue.severity ?? "error") === "error");

  const scope: JobsListScope = {
    key: `${section.project_id}:${section.workflow_id}`,
    projectId: section.project_id,
    workflowId: section.workflow_id,
    title: section.title,
    description: "",
    eyebrow: section.project_name,
    action:
      canRun && runnable ? (
        <button
          className="fs-button primary"
          data-testid="operator-new-submission"
          disabled={!runAllowed}
          onClick={() => {
            setDialogGeneration((current) => current + 1);
            setDialogOpen(true);
          }}
          title={runAllowed ? undefined : t("react.auth.project_action_not_allowed", "Your access to this project does not include this action.")}
          type="button"
        >
          <Plus aria-hidden="true" />
          {t("react.operator.new", "New")}
        </button>
      ) : null,
    notice:
      canRun && !runnable ? (
        <DsAlert title={t("react.operator.blocked", "New submissions are paused")} tone="warning">
          {reasons.length ? (
            <ul>
              {reasons.map((issue) => (
                <li key={`${issue.path ?? ""}-${issue.code}`}>{issue.message}</li>
              ))}
            </ul>
          ) : (
            <p>{t("react.operator.not_runnable", "An administrator has to finish this workflow's settings before it accepts new submissions.")}</p>
          )}
        </DsAlert>
      ) : null,
    toolbar: (
      <ColumnPicker
        columns={pickable}
        hidden={hiddenSet}
        onChange={(next) => {
          setHidden(next);
          writeHiddenColumns(storageKey, next);
        }}
      />
    ),
    columns: configuredColumns.map((column) => ({
      key: column.id,
      label: column.label || column.path,
      cell: (row: Record<string, unknown>) => <OperatorCell column={column} row={row} />,
    })),
    hiddenColumns: hiddenSet,
    filterFields,
    filtersParam: (values) => operatorFiltersParam(operator.filters ?? [], values),
    // A row opens its job with this list in the address: the review queue.
    jobPath: (jobId, list) =>
      operatorJobPath(section.project_id, section.workflow_id, jobId, rowContext(list.params, list.page, list.perPage)),
  };

  return (
    <>
      <JobsListPage key={scope.key} scope={scope} />
      {canRun && runnable ? (
        <OperatorSubmitDialog
          key={dialogGeneration}
          onClose={() => setDialogOpen(false)}
          onSent={() => {
            const bridge = (globalThis as unknown as { refreshJobs?: () => Promise<void> }).refreshJobs;
            void bridge?.();
          }}
          open={dialogOpen}
          section={section}
        />
      ) : null}
    </>
  );
}

/**
 * A published workflow in one project (ADR 0075): the existing Jobs list with
 * the project and workflow fixed, the admin's columns and filters, and, for
 * people who may run it, a New button whose dialog holds the form. A viewer
 * reads the same list without it.
 */
export function OperatorSectionPage() {
  const { t } = useI18n();
  const { projectId = "", workflowId = "" } = useParams();
  const section = useQuery({
    queryKey: queryKeys.operatorSection(projectId, workflowId),
    queryFn: () => fetchOperatorSection(projectId, workflowId),
    enabled: Boolean(projectId && workflowId),
    retry: false,
  });
  if (section.isLoading) {
    return <PageLoadingState label={t("react.operator.loading", "Opening the section")} />;
  }
  if (!section.data) {
    return (
      <PageContainer width="wide">
        <DsAlert title={t("react.operator.not_found", "This section is not available.")} tone="warning" />
      </PageContainer>
    );
  }
  return <SectionList key={`${projectId}:${workflowId}`} section={section.data} />;
}
