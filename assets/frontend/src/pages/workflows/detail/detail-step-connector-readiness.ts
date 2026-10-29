import { isHttpError } from "@/lib/http/errors";
import { textOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import { readableMetadata } from "./detail-usage-labels";

export function resolveExtensionInstallAvailability(
  isError: boolean,
  error: unknown,
  installState: unknown,
): string {
  if (isError) {
    return isHttpError(error) && error.kind === "not_found" ? "removed" : "unavailable";
  }
  return textOrEmpty(installState) === "disabled" ? "disabled" : "active";
}

export function connectorIsExtension(
  connectorId: string,
  connectorRow: Record<string, unknown>,
  stepConfig: Record<string, unknown>,
): boolean {
  return (
    textOrEmpty(connectorRow.source) === "extension" ||
    connectorId.startsWith("extension.") ||
    Boolean(textOrEmpty(connectorRow.extension_install_id)) ||
    Boolean(textOrEmpty(stepConfig.extension_id))
  );
}

export function connectorCatalogOptionLabel(connectorRow: Record<string, unknown>): string {
  return readableMetadata(connectorRow.display_name, [
    connectorRow.connector_id,
    connectorRow.extension_id,
    connectorRow.connector_install_id,
    connectorRow.extension_install_id,
  ]);
}

export function extensionInstallIdFor(
  isExtension: boolean,
  connectorRow: Record<string, unknown>,
  stepConfig: Record<string, unknown>,
): string {
  if (!isExtension) return "";
  return textOrEmpty(
    stepConfig.connector_install_id ||
      connectorRow.extension_install_id ||
      connectorRow.connector_install_id ||
      connectorRow.install_id,
  );
}

export function connectorOperationIsStale({
  availability,
  installId,
  isExtension,
  isPending,
  isSuccess,
  operationId,
  operationRows,
}: {
  availability: string;
  installId: string;
  isExtension: boolean;
  isPending: boolean;
  isSuccess: boolean;
  operationId: string;
  operationRows: Array<Record<string, unknown>>;
}): boolean {
  if (!isExtension || isPending || !operationId) return false;
  if (availability !== "active") return true;
  if (!isSuccess) return false;
  if (!installId) return true;
  return !operationRows.some(
    (row) =>
      textOrEmpty(row.operation_id || row.id || row.key || row.name) === operationId,
  );
}

export function connectorConnectionState({
  availability,
  connectionId,
  connectionRequired,
  connectionRows,
  installId,
  isExtension,
  isPending,
  isSuccess,
  projectId,
}: {
  availability: string;
  connectionId: string;
  connectionRequired: boolean;
  connectionRows: Array<Record<string, unknown>>;
  installId: string;
  isExtension: boolean;
  isPending: boolean;
  isSuccess: boolean;
  projectId: string;
}) {
  const resolvable =
    isExtension &&
    connectionRequired &&
    !isPending &&
    availability === "active" &&
    isSuccess;
  const stale =
    resolvable &&
    Boolean(installId) &&
    Boolean(connectionId) &&
    !connectionRows.some(
      (row) =>
        textOrEmpty(
          row.connection_id || row.connector_install_id || row.extension_install_id,
        ) === connectionId,
    );
  const missing = resolvable && !stale && !connectionId;
  const unavailable = missing && !connectionRows.length;
  return {
    needsProjectLink: unavailable && !textOrEmpty(projectId),
    missing,
    stale,
    unavailable,
  };
}
