import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";

import { AssignmentSelector, type AssignmentSelectorRow } from "@/components/shared/assignment-selector";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  fetchProjectDatasetCatalog,
  updateProjectDatasets,
  type DatasetRecord,
} from "@/pages/registry/datasets/api";
import { isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

type ProjectDatasetsEditorProps = {
  projectId: string;
};

function textOrEmpty(value: unknown): string {
  return value === null || value === undefined ? "" : stringFromUnknownValue(value).trim();
}

function datasetSearchText(dataset: DatasetRecord): string {
  return [
    dataset.dataset_id,
    dataset.display_name,
    dataset.description,
    dataset.connector_id,
    dataset.query_language,
    ...(dataset.tags || []),
  ].map((value) => stringFromUnknownValue(value)).join(" ")
    .toLowerCase();
}

function datasetAssignmentBadge(
  workspaceWide: boolean,
  selected: boolean,
): NonNullable<AssignmentSelectorRow["badges"]>[number] {
  if (workspaceWide) {
    return { label: "workspace-wide", tone: "info" };
  }
  return selected
    ? { label: "linked", tone: "linked" }
    : { label: "available", tone: "available" };
}

export function ProjectDatasetsEditor({ projectId }: Readonly<ProjectDatasetsEditorProps>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [localDatasetIds, setLocalDatasetIds] = useState<string[]>([]);
  const [selectionDirty, setSelectionDirty] = useState(false);
  const [datasetSearch, setDatasetSearch] = useState("");
  const [message, setMessage] = useState("");

  const datasetsQuery = useQuery({
    queryKey: ["projects", projectId, "datasets"],
    queryFn: () => fetchProjectDatasetCatalog(projectId),
    enabled: Boolean(projectId),
  });

  const availableDatasets = useMemo(
    () => datasetsQuery.data?.available_datasets || [],
    [datasetsQuery.data?.available_datasets],
  );
  const serverDatasetIds = datasetsQuery.data?.dataset_ids || [];
  const explicitDatasetIds = useMemo(
    () => datasetsQuery.data?.explicit_dataset_ids || [],
    [datasetsQuery.data?.explicit_dataset_ids],
  );
  const selectedDatasetIds = selectionDirty ? localDatasetIds : serverDatasetIds;
  const selectedDatasetIdSet = useMemo(
    () => new Set(selectedDatasetIds),
    [selectedDatasetIds],
  );
  const selectorRows = useMemo<AssignmentSelectorRow[]>(() => {
    return availableDatasets.map((dataset) => {
      const datasetId = textOrEmpty(dataset.dataset_id);
      const workspaceWide = dataset.availability_mode === "all_workspace_projects";
      const explicitLinked = explicitDatasetIds.includes(datasetId);
      const selected = workspaceWide || selectedDatasetIdSet.has(datasetId);
      return {
        id: datasetId,
        title: dataset.display_name || datasetId,
        subtitle: datasetId,
        details: [dataset.connector_id || "unknown", dataset.workspace_id ? `workspace ${dataset.workspace_id}` : ""].filter(isTruthy),
        badges: [
          datasetAssignmentBadge(workspaceWide, selected),
          !dataset.enabled ? { label: "disabled", tone: "disabled" as const } : null,
          explicitLinked && !workspaceWide ? { label: "explicit", tone: "default" as const } : null,
        ].filter(isTruthy) as AssignmentSelectorRow["badges"],
        disabled: workspaceWide || !dataset.enabled,
        selected,
        filterKeys: [
          "all",
          selected ? "linked" : "available",
          !dataset.enabled ? "disabled" : "",
          workspaceWide ? "workspace-wide" : "",
        ].filter(isTruthy),
        searchText: datasetSearchText(dataset),
      };
    });
  }, [availableDatasets, explicitDatasetIds, selectedDatasetIdSet]);
  const filterItems = useMemo(
    () => [
      { key: "all", label: t("project_detail.jobs.all", "All"), count: selectorRows.length },
      { key: "linked", label: t("react.projects.linked", "Linked"), count: selectorRows.filter((row) => row.selected).length },
      {
        key: "available",
        label: t("react.projects.available", "Available"),
        count: selectorRows.filter((row) => !row.selected).length,
      },
      {
        key: "workspace-wide",
        label: t("react.projects.workspace_wide", "Workspace-wide"),
        count: selectorRows.filter((row) => (row.filterKeys || []).includes("workspace-wide")).length,
      },
      {
        key: "disabled",
        label: t("account_settings.status.disabled", "Disabled"),
        count: selectorRows.filter((row) => (row.filterKeys || []).includes("disabled")).length,
      },
    ],
    [selectorRows, t],
  );
  const [datasetFilter, setDatasetFilter] = useState("all");

  const saveMutation = useMutation({
    mutationFn: () => updateProjectDatasets(projectId, selectedDatasetIds),
    onSuccess: async () => {
      setSelectionDirty(false);
      setMessage(t("react.projects.project_datasets_updated", "Project datasets updated."));
      await queryClient.invalidateQueries({ queryKey: ["projects", projectId, "datasets"] });
      await queryClient.invalidateQueries({ queryKey: ["registry", "datasets"] });
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : t("react.projects.failed_to_update_project_datasets", "Failed to update project datasets.")),
  });

  function toggleDataset(datasetId: string) {
    setLocalDatasetIds((current) => {
      const source = selectionDirty ? current : selectedDatasetIds;
      if (source.includes(datasetId)) {
        return source.filter((item) => item !== datasetId);
      }
      return [...source, datasetId].sort((left, right) => left.localeCompare(right));
    });
    setSelectionDirty(true);
  }

  return (
    <section className="space-y-6">
      <Card className="fs-card">
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-lg">{t("react.projects.datasets", "Datasets")}</CardTitle>
              <CardDescription>
                {t("react.projects.link_datasets_to_this_project_dataset_definitions_stay_reusable_across_projects", "Link datasets to this project. Dataset definitions stay reusable across projects.")} </CardDescription>
            </div>
            <div className="flex flex-wrap gap-2 text-xs fs-ink-secondary">
              <span className="rounded-full border border-selected fs-surface-selected px-2.5 py-1 fs-ink-brand">
                {t("react.projects.dataset_linked_count", "{count} linked", { count: selectedDatasetIds.length })}
              </span>
              <span className="rounded-full border border-subtle px-2.5 py-1">
                {t("react.projects.dataset_total_count", "{count} total", { count: availableDatasets.length })}
              </span>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <AssignmentSelector
            activeFilter={datasetFilter}
            emptyState={
              <>
                {t("react.projects.no_datasets_are_available_yet_create_one_in", "No datasets are available yet. Create one in")}{" "}
                <Link className="fs-ink-brand hover:fs-ink-brand" to="/registry/datasets">
                  {t("react.projects.registry_datasets", "Registry / Datasets")} </Link>
                .
              </>
            }
            filters={filterItems}
            loading={datasetsQuery.isLoading}
            loadingLabel={t("react.projects.loading_datasets", "Loading datasets...")}
            message={message}
            onFilterChange={setDatasetFilter}
            onSave={() => saveMutation.mutate()}
            onSearchChange={setDatasetSearch}
            onToggleRow={toggleDataset}
            rows={selectorRows}
            saveLabel={t("react.projects.save_dataset_links", "Save dataset links")}
            saving={saveMutation.isPending}
            searchPlaceholder={t("react.projects.dataset_name_connector_or_tag", "Dataset name, connector, or tag")}
            searchValue={datasetSearch}
            selectedCount={selectedDatasetIds.length}
            totalCount={availableDatasets.length}
          />
        </CardContent>
      </Card>

      {datasetsQuery.isError ? (
        <Card className="border-selected fs-surface-attention">
          <CardContent className="text-sm fs-ink-attention">
            {datasetsQuery.error instanceof Error ? datasetsQuery.error.message : t("react.projects.failed_to_load_project_datasets", "Failed to load project datasets.")}
          </CardContent>
        </Card>
      ) : null}
    </section>
  );
}
