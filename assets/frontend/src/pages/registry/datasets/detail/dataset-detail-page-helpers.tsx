import { isInstallOwned } from "../dataset-ownership";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { DsBadge, RowActionMenu, type DsTableColumn } from "@/components/shared/design-system";

import { type DatasetQueryTemplate } from "@/pages/registry/datasets/api";

export type DatasetTabKey = "dataset" | "saved-queries";

export const TAB_KEYS = new Set<string>(["dataset", "saved-queries"]);

export function localizedTABS(t: I18nContextValue["t"] = fallbackContext.t): Array<{ key: DatasetTabKey; label: string }> {
  return [
  { key: "dataset", label: t("react.registry.dataset_scope", "Dataset scope") },
  { key: "saved-queries", label: t("react.registry.saved_queries", "Saved queries") },
];
}

export const TABS: Array<{ key: DatasetTabKey; label: string }> = localizedTABS();

export type QueryPreviewDraft = {
  error: string;
  mode: "preview" | "validate";
  paramsJson: string;
  projectId: string;
  query: DatasetQueryTemplate;
};

export type SavedQueryColumnActions = Readonly<{
  activeRowMenuId: string | null;
  closeRowMenu: () => void;
  openEdit: (query: DatasetQueryTemplate) => void;
  openPreview: (query: DatasetQueryTemplate, mode?: QueryPreviewDraft["mode"]) => void;
  requestDelete: (query: DatasetQueryTemplate) => void;
  setRowMenuOpen: (query: DatasetQueryTemplate, label: string, open: boolean) => void;
  toggleQuery: (queryTemplateId: string) => void;
}>;

export function buildSavedQueryColumns(
  actions: SavedQueryColumnActions, t: I18nContextValue["t"] = fallbackContext.t
): DsTableColumn<DatasetQueryTemplate>[] {
  return [
    {
      key: "display_name",
      header: t("settings.mcp.fields.name", "Name"),
      cell: (row) => (
        <>
          <span>{row.display_name}</span>
          {/* Canon reserves the code face for identifiers. */}
          <br />
          <code className="fs-code-value">{row.query_template_id}</code>
        </>
      ),
    },
    { key: "query_language", header: t("react.registry.display_query_language", "Language"), cell: (row) => row.query_language },
    { key: "version", header: t("registry.data_contracts.column.version", "Version"), align: "right", cell: (row) => row.version },
    {
      key: "enabled",
      header: t("extensions.runtime.status", "Status"),
      cell: (row) => (
        <DsBadge tone={row.enabled ? "green" : "neutral"}>
          {row.enabled ? t("settings.extensions.enabled", "enabled") : t("settings.notifications.status.disabled", "disabled")}
        </DsBadge>
      ),
    },
    {
      key: "actions",
      header: "",
      ariaLabel: t("extensions.table.actions", "Actions"),
      cell: (row) => {
        const label = t("react.registry.actions_for_value0", "Actions for {value0}", { value0: row.display_name });
        return (
          <RowActionMenu
            label={label}
            onOpenChange={(open) => actions.setRowMenuOpen(row, label, open)}
            open={actions.activeRowMenuId === row.query_template_id}
          >
            <div className="fs-row-menu-group">
              <button
                onClick={() => {
                  actions.closeRowMenu();
                  actions.openEdit(row);
                }}
                role="menuitem"
                type="button"
              >{isInstallOwned(row) ? t("react.registry.view", "View") : t("extensions.repeater.edit", "Edit")}</button>
              <button
                disabled={!row.enabled}
                onClick={() => {
                  actions.closeRowMenu();
                  actions.openPreview(row);
                }}
                role="menuitem"
                type="button"
              >{t("registry.workflow_detail.run_lab.tab.preview", "Preview")}</button>
              <button
                disabled={!row.enabled}
                onClick={() => {
                  actions.closeRowMenu();
                  actions.openPreview(row, "validate");
                }}
                role="menuitem"
                type="button"
              >{t("react.registry.validate", "Validate")}</button>
              {!isInstallOwned(row) ? <button
                onClick={() => {
                  actions.closeRowMenu();
                  actions.toggleQuery(row.query_template_id);
                }}
                role="menuitem"
                type="button"
              >
                {row.enabled ? t("extensions.detail.disable", "Disable") : t("extensions.detail.enable", "Enable")}
              </button> : null}
            </div>
            {!isInstallOwned(row) ? <>
            <div className="fs-row-menu-separator" />
            <div className="fs-row-menu-group">
              <button
                className="danger"
                onClick={() => {
                  actions.closeRowMenu();
                  actions.requestDelete(row);
                }}
                role="menuitem"
                type="button"
              >{t("settings.model_providers.delete", "Delete")}</button>
            </div>
            </> : null}
          </RowActionMenu>
        );
      },
    },
  ];
}
