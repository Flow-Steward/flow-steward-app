import { isInstallOwned } from "./dataset-ownership";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { appHref } from "@/lib/routing/app-href";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import type { DatasetRecord } from "@/pages/registry/datasets/api";

type DatasetGridRow = DatasetRecord & {
  _configuration_mutable: boolean;
  _can_enable: boolean;
  _can_disable: boolean;
  _enabled_label: string;
  _mode_label: string;
  _projects_label: string;
  _tags_label: string;
};

export type PlatformGridElement = HTMLElement & {
  config?: Record<string, unknown>;
  setData?: (payload: { items: DatasetGridRow[]; total: number }) => void;
};

export function buildDatasetRows(datasets: DatasetRecord[], t: I18nContextValue["t"] = fallbackContext.t): DatasetGridRow[] {
  return datasets.map((dataset) => {
    const projectCount = dataset.project_ids?.length || 0;
    let availabilityLabel = t("react.registry.specific_only", "specific only");
    if (dataset.availability_mode === "all_workspace_projects") {
      availabilityLabel = t("react.registry.display_all_workspace_projects", "all workspace projects");
    } else if (projectCount) {
      availabilityLabel = stringFromUnknownValue(projectCount);
    }
    return {
      ...dataset,
      _configuration_mutable: !isInstallOwned(dataset),
      _can_enable: !isInstallOwned(dataset) && !dataset.enabled,
      _can_disable: !isInstallOwned(dataset) && dataset.enabled,
      _enabled_label: dataset.enabled ? "enabled" : "disabled",
      _mode_label: dataset.read_only ? "read only" : "read/write",
      _projects_label: availabilityLabel,
    _tags_label: dataset.tags?.length ? dataset.tags.join(", ") : t("react.registry.none", "None"),
    };
  });
}

export function datasetGridConfig(readOnly = false, t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  return {
    id_field: "dataset_id",
    title: "",
    search: { enabled: true, placeholder: t("react.registry.filter_datasets", "Filter datasets...") },
    filters_schema: {
      fields: [
        {
          filter_id: "query_language",
          label: t("react.registry.query", "Query"),
          widget: "select",
          props: {
            options: [
              { value: "sql", label: "SQL" },
              { value: "graphql", label: "GraphQL" },
              { value: "rest", label: "REST" },
            ],
          },
        },
        {
          filter_id: "_mode_label",
          label: t("registry.workflow_detail.run_lab.test_run.mode", "Mode"),
          widget: "select",
          props: {
            options: [
              { value: "read only", label: t("settings.access.service_accounts.form.level.read", "Read only") },
              { value: "read/write", label: t("react.registry.read_write", "Read/write") },
            ],
          },
        },
        {
          filter_id: "_enabled_label",
          label: t("extensions.runtime.status", "Status"),
          widget: "select",
          props: {
            options: [
              { value: "enabled", label: t("settings.mcp.status.enabled", "Enabled") },
              { value: "disabled", label: t("react.registry.disabled", "Disabled") },
            ],
          },
        },
      ],
    },
    pagination: { page_size_default: 25, page_size_options: [25, 50, 100, 200] },
    empty_state: { title: t("react.registry.no_datasets_yet", "No datasets yet"), description: t("react.registry.create_one_using_the_button_above", "Create one using the button above.") },
    columns: [
      readOnly
        ? { key: "display_name", label: t("settings.mcp.fields.name", "Name"), type: "text" }
        : { key: "display_name", label: t("settings.mcp.fields.name", "Name"), type: "link", link_href: appHref("/registry/datasets/{dataset_id}") },
      { key: "dataset_id", label: "ID", type: "id", width: "180px" },
      { key: "connector_id", label: t("react.registry.connector", "Connector"), type: "text", truncate: true, truncate_width: "14rem" },
      { key: "query_language", label: t("react.registry.query", "Query"), type: "badge", width: "110px" },
      { key: "_mode_label", label: t("registry.workflow_detail.run_lab.test_run.mode", "Mode"), type: "text", width: "120px" },
      { key: "_projects_label", label: t("shell.nav.projects", "Projects"), type: "text", width: "100px" },
      { key: "_tags_label", label: t("registry.tools.field.tags", "Tags"), type: "text", truncate: true, truncate_width: "16rem" },
      {
        key: "_enabled_label",
        label: t("extensions.runtime.status", "Status"),
        type: "badge",
        width: "110px",
        badge_map: { enabled: "green", disabled: "neutral" },
      },
    ],
    row_actions: readOnly
      ? []
      : [
          { action_id: "view", label: t("react.registry.view", "View"), variant: "ghost", visible_if: { field: "_configuration_mutable", eq: false } },
          { action_id: "edit", label: t("extensions.repeater.edit", "Edit"), variant: "ghost", visible_if: { field: "_configuration_mutable", eq: true } },
          { action_id: "toggle-on", label: t("extensions.detail.enable", "Enable"), variant: "ghost", visible_if: { field: "_can_enable", eq: true } },
          { action_id: "toggle-off", label: t("extensions.detail.disable", "Disable"), variant: "ghost", visible_if: { field: "_can_disable", eq: true } },
          { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger", visible_if: { field: "_configuration_mutable", eq: true } },
        ],
  };
}

export type DatasetFilterField = {
  key: string;
  label: string;
  options: Array<{ value: string; label: string }>;
};

/**
 * The toolbar's filter fields, read out of the grid config rather than retyped,
 * so the filter list and the column that renders the value cannot drift apart.
 */
export function datasetFilterFields( t: I18nContextValue["t"] = fallbackContext.t): DatasetFilterField[] {
  const config = datasetGridConfig( undefined, t);
  const schema = config.filters_schema;
  const fields =
    schema && typeof schema === "object" && Array.isArray((schema as { fields?: unknown }).fields)
      ? ((schema as { fields: unknown[] }).fields as Array<Record<string, unknown>>)
      : [];
  return fields.flatMap((field) => {
    const key = stringFromUnknownValue(field.filter_id).trim();
    if (!key) {
      return [];
    }
    const props = field.props;
    const rawOptions =
      props && typeof props === "object" && Array.isArray((props as { options?: unknown }).options)
        ? ((props as { options: unknown[] }).options as Array<Record<string, unknown>>)
        : [];
    return [
      {
        key,
        label: stringFromUnknownValue(field.label).trim() || key,
        options: rawOptions.map((option) => ({
          value: stringFromUnknownValue(option.value),
          label: stringFromUnknownValue(option.label) || stringFromUnknownValue(option.value),
        })),
      },
    ];
  });
}

/** The grid config's own empty-state copy, so the page does not retype it. */
export function datasetEmptyState( t: I18nContextValue["t"] = fallbackContext.t): { title: string; description: string } {
  const empty = datasetGridConfig( undefined, t).empty_state;
  const payload = empty && typeof empty === "object" ? (empty as Record<string, unknown>) : {};
  return {
    title: stringFromUnknownValue(payload.title) || t("react.registry.no_datasets_yet", "No datasets yet"),
    description: stringFromUnknownValue(payload.description),
  };
}
