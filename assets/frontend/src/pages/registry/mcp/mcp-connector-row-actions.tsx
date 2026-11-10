import { RowActionMenu } from "@/components/shared/design-system";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { ButtonBusyContent } from "@/components/ui/button";
import { type I18nContextValue } from "@/lib/i18n/context";
import { type McpConnectorRecord } from "@/pages/registry/mcp/api";

import { type ConnectorColumnsOptions, type McpRowBusy } from "./mcp-list-page-helpers";

/* One running action per row: only the pressed item is busy; the other items
   keep their normal look and ignore presses until it finishes. */
export function ConnectorRowActions({
  busyAction,
  canManageProject,
  onDelete,
  onHealth,
  onRowMenuChange,
  onSync,
  onView,
  row,
  rowMenu,
  t,
}: Readonly<Omit<ConnectorColumnsOptions, "busyConnector" | "projectName"> & {
  busyAction: McpRowBusy["action"] | null;
  row: McpConnectorRecord;
  t: I18nContextValue["t"];
}>) {
  const connectorId = row.connector_id;
  const syncBusy = busyAction === "sync";
  const healthBusy = busyAction === "health";
  const deleteBusy = busyAction === "delete-plan" || busyAction === "delete";
  const syncShown = useLoaderDelay(syncBusy);
  const healthShown = useLoaderDelay(healthBusy);
  const deleteShown = useLoaderDelay(deleteBusy);
  return (
    <RowActionMenu
      label={t("react.registry.actions_for_value0", "Actions for {value0}", { value0: row.display_name })}
      onOpenChange={(open) => onRowMenuChange(open ? connectorId : null)}
      open={rowMenu === connectorId}
    >
      <div className="fs-row-menu-group">
        <button onClick={() => onView(connectorId)} role="menuitem" type="button">{t("react.registry.view", "View")}</button>
        {canManageProject ? null : <span className="fs-row-menu-label">{t("settings.access.service_accounts.form.level.read", "Read only")}</span>}
        {canManageProject ? (
          <>
            <button
              aria-busy={syncBusy || undefined}
              onClick={() => {
                if (busyAction) return;
                onSync(connectorId);
                onRowMenuChange(null);
              }}
              role="menuitem"
              type="button"
            ><ButtonBusyContent busyLabel={t("react.registry.mcp_final_syncing", "Syncing…")} shown={syncShown}>{t("react.registry.sync", "Sync")}</ButtonBusyContent></button>
            <button
              aria-busy={healthBusy || undefined}
              onClick={() => {
                if (busyAction) return;
                onHealth(connectorId);
                onRowMenuChange(null);
              }}
              role="menuitem"
              type="button"
            ><ButtonBusyContent busyLabel={t("react.extensions.checking", "Checking…")} shown={healthShown}>{t("settings.mcp.health", "Health")}</ButtonBusyContent></button>
          </>
        ) : null}
      </div>
      {canManageProject ? (
        <>
          <div className="fs-row-menu-separator" />
          <div className="fs-row-menu-group">
            <button
              aria-busy={deleteBusy || undefined}
              className="danger"
              onClick={() => {
                if (busyAction) return;
                onDelete(connectorId, row.display_name);
                onRowMenuChange(null);
              }}
              role="menuitem"
              type="button"
            ><ButtonBusyContent busyLabel={busyAction === "delete" ? t("react.registry.deleting", "Deleting…") : t("react.shared.opening", "Opening…")} shown={deleteShown}>{t("settings.model_providers.delete", "Delete")}</ButtonBusyContent></button>
          </div>
        </>
      ) : null}
    </RowActionMenu>
  );
}
