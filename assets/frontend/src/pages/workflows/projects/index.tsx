import { useI18n } from "@/lib/i18n/useI18n";
import type { I18nContextValue } from "@/lib/i18n/context";
import { useEffect, useMemo, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";

import { AssignmentSelector, type AssignmentSelectorRow } from "@/components/shared/assignment-selector";
import { DsAlert, DsBadge, PageContainer } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { queryKeys } from "@/lib/query/keys";
import { fetchProjectsList, type ProjectListItem } from "@/pages/projects/list/api";
import {
  fetchProjectWorkflowBindings,
  saveProjectWorkflowBindings,
  type ProjectWorkflowBindings,
} from "@/pages/projects/workflows/api";
import {
  fetchWorkflowDetail,
  fetchWorkflowValidation,
} from "@/pages/workflows/shared/api";
import { useWorkflowHeaderMutations } from "@/pages/workflows/shared/header-actions";
import { WorkflowHeaderMutationDialogs } from "@/pages/workflows/shared/header-mutation-dialogs";
import { WorkflowSummaryHeader } from "@/pages/workflows/shared/header";
import { workflowPageTabsForDetail } from "@/pages/workflows/shared/workflow-page-tabs";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

function workflowSummaryLoadError(t: I18nContextValue["t"], ...errors: unknown[]): string {
  const resolvedError = errors.find((error) => error instanceof Error);
  return resolvedError instanceof Error
    ? resolvedError.message
    : t("react.workflows.display_failed_summary", "Failed to load workflow summary.");
}

type WorkflowProjectBindingRow = {
  defaultWorkflowId: string;
  linked: boolean;
  projectId: string;
  projectName: string;
  workflowIds: string[];
};

type ProjectBindingStatusFilter = "all" | "linked" | "available" | "default";

const EMPTY_PROJECT_BINDING_ROWS: WorkflowProjectBindingRow[] = [];

function textOrEmpty(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  return stringFromUnknownValue(value).trim();
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => textOrEmpty(item)).filter(isTruthy);
}

async function fetchAllProjectsForWorkflowBindings(): Promise<ProjectListItem[]> {
  const perPage = 200;
  const firstPage = await fetchProjectsList({
    page: 1,
    perPage,
    search: "",
    includeAllWorkspaces: true,
  });
  const projects = [...(firstPage.projects || [])];
  const totalPages = Number(firstPage.total_pages || 1);
  for (let page = 2; page <= totalPages; page += 1) {
    const payload = await fetchProjectsList({
      page,
      perPage,
      search: "",
      includeAllWorkspaces: true,
    });
    projects.push(...(payload.projects || []));
  }
  return projects;
}

function workflowIdsFromProjectBindingPayload(payload: ProjectWorkflowBindings): string[] {
  return stringList(payload.workflow_ids);
}

function defaultWorkflowIdFromProjectBindingPayload(payload: ProjectWorkflowBindings): string {
  return textOrEmpty(payload.operator_default_workflow_id);
}

function WorkflowProjectBindingsPanel({ workflowId }: Readonly<{ workflowId: string }>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [selectedProjectIdsDraft, setSelectedProjectIdsDraft] = useState<string[] | null>(null);
  const [projectSearch, setProjectSearch] = useState("");
  const [projectStatusFilter, setProjectStatusFilter] = useState<ProjectBindingStatusFilter>("all");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);

  const projectBindingsQuery = useQuery({
    queryKey: ["workflows", workflowId, "project-bindings"],
    queryFn: async (): Promise<WorkflowProjectBindingRow[]> => {
      const projects = await fetchAllProjectsForWorkflowBindings();
      const rows = await Promise.all(
        projects.map(async (project) => {
          const projectId = firstStringFromUnknownValue([project.id, ""]).trim();
          const bindings = projectId
            ? await fetchProjectWorkflowBindings(projectId)
            : {
                project_id: "",
                workflow_ids: [],
                operator_default_workflow_id: "",
                project_workflows: [],
              };
          const workflowIds = workflowIdsFromProjectBindingPayload(bindings);
          return {
            defaultWorkflowId: defaultWorkflowIdFromProjectBindingPayload(bindings),
            linked: workflowIds.includes(workflowId),
            projectId,
            projectName: textOrEmpty(project.name) || `Project ${projectId}`,
            workflowIds,
          };
        }),
      );
      return rows.filter((row) => Boolean(row.projectId));
    },
    enabled: Boolean(workflowId),
  });

  const projectRows = projectBindingsQuery.data ?? EMPTY_PROJECT_BINDING_ROWS;
  const visibleProjectIds = useMemo(() => new Set(projectRows.map((row) => row.projectId)), [projectRows]);
  const selectedProjectIds = (
    selectedProjectIdsDraft ?? projectRows.filter((row) => row.linked).map((row) => row.projectId)
  ).filter((projectId) => visibleProjectIds.has(projectId));
  const selectedProjectIdSet = useMemo(() => new Set(selectedProjectIds), [selectedProjectIds]);
  const linkedProjectCount = projectRows.filter((project) => project.linked).length;
  const defaultProjectCount = projectRows.filter((project) => project.defaultWorkflowId === workflowId).length;
  const availableProjectCount = projectRows.filter(
    (project) => !project.linked,
  ).length;
  const selectorRows = useMemo<AssignmentSelectorRow[]>(() => {
    return projectRows
      .map((project) => {
        const selected = selectedProjectIdSet.has(project.projectId);
        const defaultForWorkflow = project.defaultWorkflowId === workflowId;
        return {
          id: project.projectId,
          title: project.projectName,
          subtitle: `#${project.projectId}`,
          badges: [
            project.linked
              ? { label: t("react.workflows.linked_variant_2272bea6", "linked"), tone: "linked" as const }
              : { label: t("react.workflows.available_variant_ddd9818a", "available"), tone: "available" as const },
            defaultForWorkflow ? { label: t("react.workflows.default_variant_37a8eec1", "default"), tone: "default" as const } : null,
            selected !== project.linked ? { label: t("project_edit.dirty.unsaved_changes", "Unsaved changes"), tone: "info" as const } : null,
          ].filter(isTruthy) as AssignmentSelectorRow["badges"],
          selected,
          filterKeys: [
            "all",
            project.linked ? "linked" : "available",
            defaultForWorkflow ? "default" : "",
          ].filter(isTruthy),
          searchText: `${project.projectName} ${project.projectId}`.toLowerCase(),
          rightMeta: t("react.workflows.project_workflow_count", "{count} workflows", { count: project.workflowIds.length }),
        };
      })
      .sort((left, right) => {
        if (Boolean(left.selected) !== Boolean(right.selected)) {
          return left.selected ? -1 : 1;
        }
        return left.title.localeCompare(right.title);
      });
  }, [projectRows, selectedProjectIdSet, workflowId, t]);
  const projectFilterItems: Array<{ key: ProjectBindingStatusFilter; label: string; count: number }> = [
    { key: "all", label: t("users.filter.all_states", "All"), count: selectorRows.length },
    { key: "linked", label: t("react.workflows.linked", "Linked"), count: linkedProjectCount },
    { key: "available", label: t("react.workflows.available", "Available"), count: availableProjectCount },
    { key: "default", label: t("react.workflows.default_variant_21b111cb", "Default"), count: defaultProjectCount },
  ];

  useEffect(() => {
    setSelectedProjectIdsDraft(null);
    setMessage("");
  }, [workflowId]);

  function toggleProject(projectId: string) {
    setSelectedProjectIdsDraft((current) => {
      const source = current ?? selectedProjectIds;
      if (source.includes(projectId)) {
        return source.filter((item) => item !== projectId);
      }
      return [...source, projectId];
    });
  }

  async function saveProjectBindings() {
    setMessage("");
    const selected = new Set(selectedProjectIds);
    const changedRows = projectRows.filter((row) => row.linked !== selected.has(row.projectId));
    if (!changedRows.length) {
      setMessage(t("react.workflows.no_project_workflow_changes_to_save", "No project workflow changes to save."));
      return;
    }
    setSaving(true);
    try {
      const outcomes = await Promise.allSettled(
        changedRows.map(async (row) => {
          const shouldLink = selected.has(row.projectId);
          const nextWorkflowIds = shouldLink
            ? Array.from(new Set([...row.workflowIds, workflowId]))
            : row.workflowIds.filter((item) => item !== workflowId);
          await saveProjectWorkflowBindings(row.projectId, { workflow_ids: nextWorkflowIds });
        }),
      );
      const savedRows = changedRows.filter((_, index) => outcomes[index].status === "fulfilled");
      const invalidations = savedRows.flatMap((row) => [
        queryClient.invalidateQueries({ queryKey: ["projects", row.projectId, "workflows"] }),
        queryClient.invalidateQueries({ queryKey: ["projects", "workflow-bindings", row.projectId] }),
        queryClient.invalidateQueries({ queryKey: ["projects", row.projectId, "schedule-workflows"] }),
      ]);
      if (savedRows.length) {
        invalidations.push(
          queryClient.invalidateQueries({ queryKey: ["workflows", workflowId, "linked-project-id"] }),
          queryClient.invalidateQueries({ queryKey: ["workflows", workflowId, "schedule-projects"] }),
        );
      }
      const refreshOutcomes = await Promise.allSettled(invalidations);
      const failureDetails = outcomes.flatMap((outcome, index) => outcome.status === "rejected"
        ? [`${changedRows[index].projectName}${outcome.reason instanceof Error ? `: ${outcome.reason.message}` : ""}`]
        : []);
      const refreshFailures = refreshOutcomes.filter((outcome): outcome is PromiseRejectedResult => outcome.status === "rejected");
      const refreshed = await projectBindingsQuery.refetch();
      if (failureDetails.length || refreshFailures.length || refreshed.isError) {
        const refreshDetails = refreshFailures.flatMap((outcome) => outcome.reason instanceof Error ? [outcome.reason.message] : []);
        if (refreshed.isError && refreshed.error instanceof Error) refreshDetails.push(refreshed.error.message);
        const descriptions = [...failureDetails, ...refreshDetails];
        const detail = descriptions.length ? ` ${descriptions.join("; ")}` : "";
        setMessage(
          t("react.workflows.changes_were_not_fully_saved_some_changes_may_have_saved_unsaved_changes_remain_value0", "Changes were not fully saved. Some changes may have saved; unsaved changes remain.{value0}", { value0: detail }),
        );
        return;
      }
      setSelectedProjectIdsDraft(null);
      setMessage(t("react.workflows.project_workflow_bindings_saved", "Project workflow bindings saved."));
    } catch (error) {
      const detail = error instanceof Error ? ` ${error.message}` : "";
      setMessage(
        t("react.workflows.changes_were_not_fully_saved_some_changes_may_have_saved_unsaved_changes_remain_value0", "Changes were not fully saved. Some changes may have saved; unsaved changes remain.{value0}", { value0: detail }),
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <article className="fs-card" id="workflow-project-bindings">
      <header className="fs-card-header flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="fs-card-title">{t("shell.nav.projects", "Projects")}</h2>
          <p className="fs-card-description">{t("react.workflows.bind_this_workflow_to_projects_that_can_run_it", "Bind this workflow to projects that can run it.")}</p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <DsBadge>{linkedProjectCount} {" "}{t("react.workflows.linked_variant_2272bea6", "linked")}</DsBadge>
          <DsBadge>{projectRows.length} {" "}{t("react.workflows.total", "total")}</DsBadge>
        </div>
      </header>
      <div className="fs-card-body">
        <AssignmentSelector
          activeFilter={projectStatusFilter}
          emptyState={t("react.workflows.display_no_projects_available", "No projects are available.")}
          filters={projectFilterItems}
          loading={projectBindingsQuery.isLoading}
          loadingLabel={t("react.workflows.loading_projects", "Loading projects...")}
          message={message}
          onFilterChange={(value) => setProjectStatusFilter(value as ProjectBindingStatusFilter)}
          onSave={() => ignoreAsyncResult(saveProjectBindings())}
          onSearchChange={setProjectSearch}
          onToggleRow={toggleProject}
          renderRowAction={(row) => (
            <Link
              className="fs-button"
              to={`/projects/${row.id}?tab=workflows`}
            >
              {t("react.workflows.open_project", "Open project")}</Link>
          )}
          rows={selectorRows}
          saveLabel={t("react.workflows.display_save_project_bindings", "Save project bindings")}
          saveDisabled={projectRows.every((row) => row.linked === selectedProjectIdSet.has(row.projectId))}
          saving={saving || projectBindingsQuery.isLoading}
          searchPlaceholder={t("react.workflows.project_name_or_id", "Project name or ID")}
          searchValue={projectSearch}
          selectedCount={selectedProjectIds.length}
          selectedLabel={t("project_history.selected", "selected")}
          totalCount={projectRows.length}
        />
        {projectBindingsQuery.isError ? (
          <DsAlert title={t("react.workflows.failed_to_load_project_workflow_bindings_variant_274011f6", "Failed to load project workflow bindings")} tone="danger">
            {projectBindingsQuery.error instanceof Error
              ? projectBindingsQuery.error.message
              : t("react.workflows.failed_to_load_project_workflow_bindings", "Failed to load project workflow bindings.")}
          </DsAlert>
        ) : null}
      </div>
    </article>
  );
}

export function WorkflowsProjectsPage() {
  const { t } = useI18n();
  const { workflowId = "" } = useParams();
  const canAuthor = useIsAdmin();

  const detailQuery = useQuery({
    queryKey: queryKeys.workflowDetail(workflowId),
    queryFn: () => fetchWorkflowDetail(workflowId),
    enabled: Boolean(workflowId),
  });
  const validationQuery = useQuery({
    queryKey: queryKeys.workflowValidation(workflowId),
    queryFn: () => fetchWorkflowValidation(workflowId),
    enabled: Boolean(workflowId),
  });

  const workflowTitle = firstStringFromUnknownValue([detailQuery.data?.display_name, workflowId, "Workflow"]);
  const workflowDescription = firstStringFromUnknownValue([detailQuery.data?.description, t("react.workflows.display_manage_projects", "Manage which projects can run this workflow.")]);
  const workflowEnabled = Boolean(detailQuery.data?.enabled);
  const stepCount = useMemo(() => {
    const steps = detailQuery.data?.steps;
    return Array.isArray(steps) ? steps.length : 0;
  }, [detailQuery.data]);
  const validation = validationQuery.data?.validation;
  const isValid = Boolean(validation?.valid);
  const errorCount = Array.isArray(validation?.errors) ? validation.errors.length : 0;
  const warningCount = Array.isArray(validation?.warnings) ? validation.warnings.length : 0;
  const headerMutations = useWorkflowHeaderMutations({
    workflowId,
    onChanged: async () => {
      await Promise.allSettled([
        detailQuery.refetch(),
        validationQuery.refetch(),
      ]);
    },
  });

  // Wait for the definition and validation instead of printing placeholder state.
  if (detailQuery.isLoading || validationQuery.isLoading) {
    return (
      <PageContainer width="standard">
        <PageLoadingState label={t("react.workflows.preparing_workflow_projects", "Preparing the workflow's projects")} />
      </PageContainer>
    );
  }

  return (
    <PageContainer width="standard">
    <section className="fs-page-sections">
      <WorkflowSummaryHeader
        activePage="projects"
        description={workflowDescription}
        enabled={workflowEnabled}
        errorCount={errorCount}
        extraTabs={workflowPageTabsForDetail(detailQuery.data, canAuthor, t)}
        isValid={isValid}
        onCloneWorkflow={headerMutations.cloneWorkflow}
        onDeleteWorkflow={headerMutations.deleteWorkflow}
        onToggleEnabled={() => ignoreAsyncResult(headerMutations.toggleEnabled())}
        stepCount={stepCount}
        title={workflowTitle}
        togglingEnabled={headerMutations.togglingEnabled}
        warningCount={warningCount}
        portability={detailQuery.data?.portability}
        workflowId={workflowId}
      >
        <WorkflowHeaderMutationDialogs controller={headerMutations} workflowId={workflowId} />


        {(detailQuery.isError || validationQuery.isError) ? (
          <DsAlert title={t("react.workflows.failed_to_load_workflow_summary_variant_5d9f5c76", "Failed to load workflow summary")} tone="danger">
            {workflowSummaryLoadError(
              t,
              detailQuery.error,
              validationQuery.error,
            )}
          </DsAlert>
        ) : null}

        <WorkflowProjectBindingsPanel workflowId={workflowId} />
      </WorkflowSummaryHeader>
    </section>
    </PageContainer>
  );
}
