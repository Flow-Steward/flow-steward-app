import { DsAlert } from "@/components/shared/design-system-primitives";
import { DsFormSelect } from "@/components/shared/design-system-selects";
import { useI18n } from "@/lib/i18n/useI18n";
import { ConnectionSecretApprovalStatus } from "@/pages/extensions/extension-connection-secret-catalog";
import { stringFrom } from "@/pages/extensions/extension-manifest-utils";

import type { ExtensionPageConnection } from "./use-extension-page-connection";

/**
 * The connection an extension page reads its data through (ADR 0067). Host
 * chrome beside the project selector, built only from design-system parts.
 */
export function ExtensionPageConnectionScope({
  connection,
  onOpenPage,
}: Readonly<{ connection: ExtensionPageConnection; onOpenPage: (pageId: string) => void }>) {
  const { t } = useI18n();
  if (!connection.types.length || !connection.projectId) return null;
  if (!connection.canLoad) {
    return (
      <DsAlert title={t("react.extensions.page_connection.operator_only", "Only an operator or administrator can load this page's data.")} tone="warning" />
    );
  }
  if (connection.loading) return null;
  if (!connection.candidates.length) {
    return (
      <DsAlert title={t("react.extensions.page_connection.none", "No {type} connection in this project.", { type: connection.typeLabel })} tone="warning">
        <p>{t("react.extensions.page_connection.none_help", "Create one, then come back to this page.")}</p>
        {connection.setupPageId ? (
          <button className="fs-button sm" onClick={() => onOpenPage(connection.setupPageId)} type="button">
            {t("react.extensions.page_connection.open_setup", "Open connections")}
          </button>
        ) : null}
      </DsAlert>
    );
  }
  const selectedRow = connection.candidates.find(
    (row) => stringFrom(row.connection_id) === connection.selectedId,
  );
  return (
    <div className="fs-field fs-extension-detail-scope" data-testid="extension-page-connection">
      <span className="fs-field-label">{t("react.extensions.page_connection.label", "Connection")}</span>
      <DsFormSelect
        ariaLabel={t("react.extensions.page_connection.label", "Connection")}
        name="extension_page_connection"
        onChange={connection.select}
        options={connection.candidates.map((row) => ({
          value: stringFrom(row.connection_id),
          label: stringFrom(row.display_name || row.connection_id),
        }))}
        placeholder={t("react.extensions.page_connection.select", "Select a connection")}
        value={connection.selectedId}
      />
      <span className="fs-field-helper">
        {connection.selectedId
          ? t("react.extensions.page_connection.help", "This page reads its data through the selected connection.")
          : t("react.extensions.page_connection.choose", "Choose the connection this page reads its data through.")}
      </span>
      {selectedRow ? <ConnectionSecretApprovalStatus projectId={connection.projectId} row={selectedRow} /> : null}
    </div>
  );
}
