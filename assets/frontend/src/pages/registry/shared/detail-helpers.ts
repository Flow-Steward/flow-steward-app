import { useState } from "react";
import { useI18n } from "@/lib/i18n/useI18n";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router";

import { fetchJson } from "@/lib/http/client";
import {
  fetchRegistryDetail,
  patchRegistryEntity,
  toggleRegistryEntity,
  updateRegistryEntity,
} from "@/pages/registry/shared/api";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type RegistryEntity = Record<string, unknown>;

export function objectOrEmpty(value: unknown): RegistryEntity {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as RegistryEntity)
    : {};
}

export function normalizePrimitive(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return stringFromUnknownValue(value);
  }
  if (Array.isArray(value)) {
    return `${value.length} items`;
  }
  // AC17-R100: "object" is not a value a reader can act on, and it reads like
  // content rather than like absence. A field whose shape we cannot render is
  // empty as far as the page is concerned, so it gets the same marker as empty.
  return "\u2014";
}

export function normalizeStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => firstStringFromUnknownValue([item, ""]).trim())
    .filter(isTruthy);
}

export function normalizeRecordList(value: unknown): RegistryEntity[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is RegistryEntity => Boolean(item) && typeof item === "object" && !Array.isArray(item),
  );
}

export function firstRecordArray(entity: RegistryEntity, keys: string[]): RegistryEntity[] {
  for (const key of keys) {
    const rows = normalizeRecordList(entity[key]);
    if (rows.length) {
      return rows;
    }
  }
  return [];
}

export function firstStringArray(entity: RegistryEntity, keys: string[]): string[] {
  for (const key of keys) {
    const rows = normalizeStringList(entity[key]);
    if (rows.length) {
      return rows;
    }
  }
  return [];
}

export function firstObject(entity: RegistryEntity, keys: string[]): RegistryEntity {
  for (const key of keys) {
    const value = objectOrEmpty(entity[key]);
    if (Object.keys(value).length) {
      return value;
    }
  }
  return {};
}

export function parseCommaSeparated(value: string): string[] {
  return firstStringFromUnknownValue([value, ""])
    .split(",")
    .map((item) => item.trim())
    .filter(isTruthy);
}

export function historyRowsFromPayload(payload: unknown): RegistryEntity[] {
  if (Array.isArray(payload)) {
    return payload.filter(
      (row): row is RegistryEntity => Boolean(row) && typeof row === "object" && !Array.isArray(row),
    );
  }
  if (payload && typeof payload === "object") {
    for (const value of Object.values(payload as RegistryEntity)) {
      if (Array.isArray(value)) {
        return value.filter(
          (row): row is RegistryEntity => Boolean(row) && typeof row === "object" && !Array.isArray(row),
        );
      }
    }
  }
  return [];
}

type UseRegistryEntityDetailArgs = {
  endpoint: string;
  idParam: string;
  idKey: string;
  title: string;
  updateMode?: "patch" | "replace";
};

export function useRegistryEntityDetail({
  endpoint,
  idParam,
  idKey,
  title,
  updateMode = "replace",
}: UseRegistryEntityDetailArgs) {
  const { t } = useI18n();
  const params = useParams();
  const routeEntityId = firstStringFromUnknownValue([params[idParam], ""]).trim();
  // Success and failure are separate on purpose. A rejected save used to land
  // in `saveStatus` and render as the same neutral header badge as "Saved.",
  // so an error read as a status and a long server reason squeezed the title.
  const [saveStatus, setSaveStatus] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);

  /** Clearing the status (pages do this on edit) clears a pending error too. */
  function updateSaveStatus(value: string) {
    setSaveStatus(value);
    if (!value) setSaveError("");
  }

  function failSave(message: string) {
    setSaveStatus("");
    setSaveError(message);
  }

  const detailQuery = useQuery({
    queryKey: [endpoint, "detail", routeEntityId],
    queryFn: () => fetchRegistryDetail(endpoint, routeEntityId),
    enabled: Boolean(routeEntityId),
  });

  const historyQuery = useQuery({
    queryKey: [endpoint, "history", routeEntityId],
    queryFn: async () => {
      const payload = await fetchJson<unknown>(
        `${endpoint}/${encodeURIComponent(routeEntityId)}/history`,
      );
      return historyRowsFromPayload(payload);
    },
    enabled: Boolean(routeEntityId),
    retry: false,
  });

  const entity = objectOrEmpty(detailQuery.data);
  const resolvedEntityId = firstStringFromUnknownValue([entity[idKey], entity.id, routeEntityId]).trim();
  const entityName = firstStringFromUnknownValue([entity.display_name, entity.name, entity[`${idKey}`], resolvedEntityId, title]).trim();

  async function saveEntityOperation(operation: () => Promise<unknown>) {
    if (!resolvedEntityId) {
      failSave(t("react.registry.display_select_valid_entity", "Select a valid entity before saving."));
      return false;
    }
    updateSaveStatus("");
    setSaving(true);
    try {
      const result = await operation();
      const resultStatus =
        result && typeof result === "object" && !Array.isArray(result)
          ? (result as RegistryEntity).status
          : undefined;
      updateSaveStatus(resultStatus === "no_change" ? "No changes." : "Saved.");
      await detailQuery.refetch();
      await historyQuery.refetch();
      return true;
    } catch (error) {
      failSave(error instanceof Error ? error.message : t("react.registry.display_failed_save_changes", "Failed to save changes."));
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function saveEntityPatch(patch: RegistryEntity) {
    await saveEntityOperation(() =>
      updateMode === "patch"
        ? patchRegistryEntity(endpoint, resolvedEntityId, patch)
        : updateRegistryEntity(endpoint, resolvedEntityId, { ...entity, ...patch }),
    );
  }

  async function toggleEntityEnabled() {
    if (!resolvedEntityId) {
      failSave(t("react.registry.display_entity_id_missing", "Entity id is missing."));
      return;
    }
    updateSaveStatus("");
    setSaving(true);
    try {
      await toggleRegistryEntity(endpoint, resolvedEntityId);
      updateSaveStatus("Enabled state updated.");
      await detailQuery.refetch();
      await historyQuery.refetch();
    } catch (error) {
      failSave(error instanceof Error ? error.message : t("react.registry.failed_to_toggle_entity", "Failed to toggle entity."));
    } finally {
      setSaving(false);
    }
  }

  return {
    detailQuery,
    entity,
    entityName,
    historyQuery,
    resolvedEntityId,
    routeEntityId,
    saveEntityOperation,
    saveEntityPatch,
    saveError,
    saveStatus,
    saving,
    setSaveStatus: updateSaveStatus,
    toggleEntityEnabled,
  };
}
