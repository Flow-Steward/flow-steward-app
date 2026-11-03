import type { DatasetConnector, DatasetRecord } from "../api";
import { isRecord } from "./dataset-detail-helpers";

const text = (value: unknown): string => typeof value === "string" ? value.trim() : "";
const bridgePart = (value: string): string => value.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "");

/** Mirror the immutable projected bridge checks used by saved-query validation. */
export function projectedQueryConnector(
  dataset: DatasetRecord | undefined,
  connectors: DatasetConnector[],
): DatasetConnector | undefined {
  const metadata = dataset?.metadata_jsonb;
  const bridge = metadata?.projection_bridge;
  if (!dataset || !isRecord(bridge)) return undefined;
  const installId = text(bridge.install_id);
  const artifactId = text(bridge.artifact_id);
  const accountId = text(dataset.account_id) || text(metadata?.account_id);
  const installPart = bridgePart(installId);
  const artifactPart = bridgePart(artifactId);
  if (
    bridge.immutable !== true || bridge.artifact_kind !== "dataset" ||
    !installId || !artifactId || !text(bridge.release_id) || !accountId ||
    !installPart || !artifactPart ||
    text(dataset.source_install_id) !== installId ||
    dataset.dataset_id !== `extension.${installPart}.${artifactPart}` ||
    [dataset.account_id, metadata?.account_id].some(value => text(value) && text(value) !== accountId)
  ) return undefined;

  const reference = isRecord(metadata?.dataset_connector) ? metadata.dataset_connector : {};
  const connectorId = text(dataset.connector_id) || text(reference.connector_id);
  const matches = connectors.filter(connector => {
    const localId = text(connector.local_connector_id);
    return text(connector.source_install_id) === installId &&
      text(connector.account_id) === accountId && Boolean(localId) &&
      connector.connector_id === `extension.${installId}.${localId}` &&
      [localId, connector.connector_id].includes(connectorId);
  });
  return matches.length === 1 ? matches[0] : undefined;
}
