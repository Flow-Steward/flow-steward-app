import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { ExtensionRecord } from "@/pages/extensions/api";
import { isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import type { DsBadgeTone } from "@/components/shared/design-system";

export type ManifestAction = {
  actionId: string;
  queryId: string;
  label: string;
  kind: "action" | "query";
  scope?: string;
  visibleIf?: Array<Record<string, unknown>>;
};

export function extensionHasUi(extension: ExtensionRecord): boolean {
  if (extension.has_ui) {
    return true;
  }
  const compiledManifest =
    extension.compiled_manifest && typeof extension.compiled_manifest === "object"
      ? (extension.compiled_manifest as Record<string, unknown>)
      : {};
  let uiManifest: Record<string, unknown> = {};
  if (extension.ui_manifest && typeof extension.ui_manifest === "object") {
    uiManifest = extension.ui_manifest as Record<string, unknown>;
  } else if (compiledManifest.ui_manifest && typeof compiledManifest.ui_manifest === "object") {
    uiManifest = compiledManifest.ui_manifest as Record<string, unknown>;
  }
  return Array.isArray(uiManifest.pages) && uiManifest.pages.length > 0;
}

export function normalizeActiveTab(hash: string): "installed" | "marketplace" {
  return hash.replace(/^#/, "").trim() === "marketplace" ? "marketplace" : "installed";
}

export function objectOrEmpty(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function listFromUnknown(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === "object" && !Array.isArray(row))
    : [];
}

export function focusManifestAnchor(pageId: string, anchor?: string): void {
  const targetId = manifestAnchorDomId(pageId, anchor);
  if (!targetId) return;
  globalThis.setTimeout(() => {
    const target = globalThis.document?.getElementById(targetId);
    if (!(target instanceof HTMLElement)) return;
    try {
      target.scrollIntoView?.({ block: "start" });
    } catch {
      // Embedded browsers may not implement scrolling.
    }
    try {
      target.focus({ preventScroll: true });
    } catch {
      target.focus();
    }
  }, 0);
}

export function stringListFromUnknown(value: unknown): string[] {
  return Array.isArray(value)
    ? value
        .map((item) => stringFrom(item))
        .filter((item, index, rows) => Boolean(item) && rows.indexOf(item) === index)
    : [];
}

export type ContractActionInput = {
  name: string;
  type: string;
  required: boolean;
  schema: Record<string, unknown>;
};

function contractActionInput(item: Record<string, unknown>): ContractActionInput {
  const nestedSchema = objectOrEmpty(item.schema);
  const schemaTypes = Array.isArray(nestedSchema.type)
    ? nestedSchema.type.map((value) => stringFrom(value).toLowerCase())
    : [];
  const declaredType = stringFrom(item.type || item.value_type || nestedSchema.type).toLowerCase()
    || (schemaTypes.includes("string") ? "string" : "");
  const type = declaredType === "text" ? "string" : declaredType;
  const schema = { ...item, ...nestedSchema };
  delete schema.name;
  delete schema.required;
  delete schema.value_type;
  delete schema.schema;
  if (schema.type === undefined) schema.type = type;
  return {
    name: stringFrom(item.name),
    type,
    required: item.required === true,
    schema,
  };
}

function schemaList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function schemaPatternAllowsEmptyString(patternValue: unknown): boolean {
  const pattern = typeof patternValue === "string" ? patternValue : "";
  if (!pattern) return true;
  try {
    return new RegExp(pattern, "u").test("");
  } catch {
    return false;
  }
}

function schemaStringConstraintsAllowEmpty(schema: Record<string, unknown>): boolean {
  if (Array.isArray(schema.enum) && !schema.enum.includes("")) return false;
  if (Object.hasOwn(schema, "const") && schema.const !== "") return false;
  const rawMinLength = schema.minLength ?? schema.min_length;
  if (rawMinLength !== undefined && Number(rawMinLength) > 0) return false;
  const rawMaxLength = schema.maxLength ?? schema.max_length;
  if (rawMaxLength !== undefined && Number(rawMaxLength) < 0) return false;
  if (stringFrom(schema.format)) return false;
  return schemaPatternAllowsEmptyString(schema.pattern);
}

function schemaCompositionAllowsEmpty(schema: Record<string, unknown>): boolean {
  const allOf = schemaList(schema.allOf);
  if (allOf.some((candidate) => !schemaAllowsEmptyString(candidate))) return false;
  const anyOf = schemaList(schema.anyOf);
  if (anyOf.length && !anyOf.some(schemaAllowsEmptyString)) return false;
  const oneOf = schemaList(schema.oneOf);
  if (oneOf.length && oneOf.filter(schemaAllowsEmptyString).length !== 1) return false;
  if (schema.not !== undefined && schemaAllowsEmptyString(schema.not)) return false;
  if (schema.if === undefined) return true;
  const branch = schemaAllowsEmptyString(schema.if) ? schema.then : schema.else;
  return branch === undefined || schemaAllowsEmptyString(branch);
}

function schemaAllowsEmptyString(schemaValue: unknown): boolean {
  if (schemaValue === false) return false;
  if (schemaValue === true) return true;
  const schema = objectOrEmpty(schemaValue);
  if (schema.$ref) return false;

  const rawType = schema.type;
  const types = Array.isArray(rawType)
    ? rawType.map((value) => stringFrom(value).toLowerCase())
    : [stringFrom(rawType).toLowerCase()].filter(Boolean);
  const supportsString = !types.length
    || types.some((type) => type === "string" || type === "text");
  return supportsString
    && schemaStringConstraintsAllowEmpty(schema)
    && schemaCompositionAllowsEmpty(schema);
}

export function contractActionInputAllowsEmptyString(input: ContractActionInput): boolean {

  return input.type === "string" && schemaAllowsEmptyString(input.schema);
}

export function contractActionInputs(value: unknown, actionId: string): ContractActionInput[] | null {
  const root = objectOrEmpty(value);
  const compiledManifest = objectOrEmpty(root.compiled_manifest);
  const candidates = [
    { operation_manifest: root.operation_manifest },
    { operation_manifest: compiledManifest.operation_manifest },
    root.extension_contract_v2_normalized,
    compiledManifest.extension_contract_v2_normalized,
    objectOrEmpty(root.runtime).extension_contract_v2_normalized,
    objectOrEmpty(compiledManifest.runtime).extension_contract_v2_normalized,
  ];
  for (const candidate of candidates) {
    const contract = objectOrEmpty(candidate);
    const operation = listFromUnknown(contract.operation_manifest).find(
      (row) => stringFrom(row.operation_id) === actionId,
    );
    if (!operation) {
      continue;
    }
    const inputs = listFromUnknown(operation.inputs || operation.public_inputs)
      .map(contractActionInput)
      .filter((item) => Boolean(item.name));
    return inputs.filter(
      (item, index) => inputs.findIndex((candidate) => candidate.name === item.name) === index,
    );
  }
  for (const manifest of [root, compiledManifest]) {
    if (Number(manifest.manifest_version) !== 2) {
      continue;
    }
    const action = listFromUnknown(manifest.action_definitions).find(
      (row) => stringFrom(row.action_id || row.id) === actionId,
    );
    if (!action || !Array.isArray(action.parameters)) {
      continue;
    }
    const inputs = listFromUnknown(action.parameters)
      .map(contractActionInput)
      .filter((item) => Boolean(item.name));
    return inputs.filter(
      (item, index) => inputs.findIndex((candidate) => candidate.name === item.name) === index,
    );
  }
  return null;
}

export function contractActionInputNames(value: unknown, actionId: string): string[] | null {
  return contractActionInputs(value, actionId)?.map((item) => item.name) ?? null;
}

export function stringFrom(value: unknown, fallback = "") {
  return stringFromUnknownValue(value ?? fallback).trim();
}

export function manifestUiPages(value: unknown): Array<Record<string, unknown>> {
  const root = objectOrEmpty(value);
  const candidates = [
    root.ui_manifest,
    objectOrEmpty(root.compiled_manifest).ui_manifest,
  ];
  for (const candidate of candidates) {
    const ui = objectOrEmpty(candidate);
    const pages = listFromUnknown(ui.pages);
    if (pages.length) {
      return pages;
    }
  }
  return [];
}

export function optionRows(value: unknown): Array<{ value: string; label: string; disabled?: boolean }> {
  const seen = new Set<string>();
  return listFromUnknown(value)
    .map((row) => ({
      value: stringFrom(row.value || row.id || row.key),
      label: stringFrom(row.label ?? row.title ?? row.value ?? row.id ?? row.key),
      ...(typeof row.disabled === "boolean" ? { disabled: row.disabled } : {}),
    }))
    .filter((row) => {
      if (!row.value || !row.label || seen.has(row.value)) {
        return false;
      }
      seen.add(row.value);
      return true;
    });
}

export function manifestQueryValues(payload: unknown): Record<string, unknown> {
  const entity = objectOrEmpty(payload);
  const direct = objectOrEmpty(entity.values);
  if (Object.keys(direct).length) {
    return direct;
  }
  const nestedResult = objectOrEmpty(objectOrEmpty(entity.result).values);
  if (Object.keys(nestedResult).length) {
    return nestedResult;
  }
  const queryResponse = objectOrEmpty(objectOrEmpty(objectOrEmpty(entity.query).response).values);
  if (Object.keys(queryResponse).length) {
    return queryResponse;
  }
  return objectOrEmpty(objectOrEmpty(objectOrEmpty(entity.action).response).values);
}

export function fieldOptions(field: Record<string, unknown>) {
  const props = objectOrEmpty(field.props);
  const direct = optionRows(field.options);
  return direct.length ? direct : optionRows(props.options);
}

export function componentQueryId(component: Record<string, unknown>) {
  return stringFrom(
    component.query_id || objectOrEmpty(component.data).load_query || component.load_query,
  );
}

export function manifestPageRequiresProject(
  page: Record<string, unknown> | undefined,
  extensionProjectRequired: boolean,
) {
  if (page?.project_scoped === false) {
    return false;
  }
  if (!extensionProjectRequired) {
    return false;
  }
  if (page?.project_scoped === true) {
    return true;
  }
  return listFromUnknown(page?.components).some((component) => {
    const type = stringFrom(component.type).toLowerCase();
    return type !== "markdown" || Boolean(componentQueryId(component));
  });
}

export function componentQueryParamsForComponent(component: Record<string, unknown>) {
  return objectOrEmpty(component.query_params || objectOrEmpty(component.data).query_params);
}

export function booleanFrom(value: unknown, fallback = false) {
  if (typeof value === "boolean") {
    return value;
  }
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  const normalized = stringFrom(value).toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off"].includes(normalized)) {
    return false;
  }
  return fallback;
}

export function extensionState(extension: ExtensionRecord): "active" | "disabled" | "broken" {
  if (extension.broken) {
    return "broken";
  }
  return extension.active ? "active" : "disabled";
}

export function pathSegments(path: string) {
  return stringFrom(path)
    .split(".")
    .map((part) => part.trim())
    .filter(isTruthy);
}

export function valueAtPath(source: Record<string, unknown>, path: string) {
  const direct = source[path];
  if (direct !== undefined) {
    return direct;
  }
  return pathSegments(path).reduce<unknown>((current, part) => {
    if (!current || typeof current !== "object" || Array.isArray(current)) {
      return undefined;
    }
    return (current as Record<string, unknown>)[part];
  }, source);
}

export function cloneWithPathValue(source: Record<string, unknown>, path: string, value: unknown) {
  const segments = pathSegments(path);
  if (!segments.length) {
    return source;
  }
  const next = structuredClone(source || {}) as Record<string, unknown>;
  let cursor: Record<string, unknown> = next;
  for (const segment of segments.slice(0, -1)) {
    const nested = cursor[segment];
    if (!nested || typeof nested !== "object" || Array.isArray(nested)) {
      cursor[segment] = {};
    }
    cursor = cursor[segment] as Record<string, unknown>;
  }
  cursor[segments.at(-1) ?? ""] = value;
  return next;
}

export function normalizedCompareValue(value: unknown) {
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "number") {
    return stringFromUnknownValue(value);
  }
  return stringFrom(value).toLowerCase();
}

export function visibleIfRows(value: unknown): Array<Record<string, unknown>> {
  if (Array.isArray(value)) {
    return listFromUnknown(value);
  }
  const entity = objectOrEmpty(value);
  return Object.keys(entity).length ? [entity] : [];
}

export function matchesVisibleIf(row: Record<string, unknown>, condition: Record<string, unknown>) {
  const path = stringFrom(condition.path || condition.field || condition.key);
  if (!path) {
    return true;
  }
  const actual = normalizedCompareValue(valueAtPath(row, path));
  const eq = condition.eq;
  if (eq !== undefined) {
    return actual === normalizedCompareValue(eq);
  }
  const neq = condition.neq;
  if (neq !== undefined) {
    return actual !== normalizedCompareValue(neq);
  }
  const inValues = Array.isArray(condition.in) ? condition.in : undefined;
  if (inValues) {
    return inValues.map((item) => normalizedCompareValue(item)).includes(actual);
  }
  const notInValues = Array.isArray(condition.not_in) ? condition.not_in : undefined;
  if (notInValues) {
    return !notInValues.map((item) => normalizedCompareValue(item)).includes(actual);
  }
  return true;
}

export function actionVisibleForRow(action: ManifestAction, row: Record<string, unknown>) {
  if (!action.visibleIf?.length) {
    return true;
  }
  return action.visibleIf.every((condition) => matchesVisibleIf(row, condition));
}

/**
 * A component-level error, and the field it belongs to when one is known.
 *
 * A message on its own leaves the user scanning the form for the control it
 * refers to, so the failing field travels with it.
 */
export type ComponentError = { message: string; fieldPath: string };

const MANIFEST_ANCHOR_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/u;

/** Stable DOM identity for extension-declared, compiler-validated help targets. */
export function manifestAnchorDomId(pageId: unknown, anchorId: unknown): string {
  const page = stringFrom(pageId);
  const anchor = stringFrom(anchorId);
  return MANIFEST_ANCHOR_ID.test(page) && MANIFEST_ANCHOR_ID.test(anchor)
    ? `extension-anchor-${page}-${anchor}`
    : "";
}

/** The secret fields a saved connection already holds, by field path. */
export function connectionSecretRefs(value: unknown): Record<string, string> {
  const refs = objectOrEmpty(objectOrEmpty(value).refs);
  return Object.fromEntries(
    Object.entries(refs)
      .map(([key, ref]) => [stringFrom(key), stringFrom(ref)] as const)
      .filter(([key, ref]) => Boolean(key && ref)),
  );
}

export function manifestActionOutcome(payload: unknown, t: I18nContextValue["t"] = fallbackContext.t) {
  const entity = objectOrEmpty(payload);
  const action = objectOrEmpty(entity.action);
  const response = objectOrEmpty(action.response);
  const result = objectOrEmpty(response.result);
  const status = stringFrom(action.status).toLowerCase();
  const ok = status === "succeeded" && response.ok !== false;
  const providedMessage = stringFrom(response.error || response.message || result.error || action.detail || entity.message);
  const message = stringFrom(
    providedMessage,
    ok ? t("react.extensions.action_completed", "Action completed.") : t("react.extensions.extension_action_failed", "Extension action failed."),
  );
  return { ok, message, response, result, hasProvidedMessage: Boolean(providedMessage) };
}

/** The manifest's colour words, mapped onto the published badge tones. */
export function badgeTone(value: string, badgeMap: Record<string, unknown>): DsBadgeTone {
  const tone = stringFrom(badgeMap[value] || badgeMap.default).toLowerCase();
  if (tone === "green") return "green";
  if (tone === "red") return "danger";
  if (tone === "amber" || tone === "yellow" || tone === "orange") return "orange";
  return "neutral";
}

export function formatGridValue(value: unknown, column: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t) {
  const type = stringFrom(column.type || "text").toLowerCase();
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (type === "datetime") {
    const raw = stringFrom(value);
    if (!raw) {
      return "—";
    }
    const parsed = new Date(raw);
    return Number.isNaN(parsed.getTime()) ? raw : parsed.toLocaleString();
  }
  if (typeof value === "boolean") {
    return value ? t("extensions.repeater.yes", "Yes") : t("extensions.repeater.no", "No");
  }
  return stringFrom(value, "—");
}

export function deriveHealthStatusFromChecks(
  explicitStatus: string,
  checks: Array<Record<string, unknown>>,
) {
  const normalizedExplicit = stringFrom(explicitStatus).toLowerCase();
  if (normalizedExplicit && normalizedExplicit !== "unknown") {
    return normalizedExplicit;
  }
  const checkStatuses = checks
    .map((check) => stringFrom(check.status).toLowerCase())
    .filter(isTruthy);
  const failedStatuses = new Set(["failed", "error", "unhealthy"]);
  const warningStatuses = new Set(["warning", "degraded"]);
  if (checkStatuses.some((status) => failedStatuses.has(status))) {
    return "failed";
  }
  if (checkStatuses.some((status) => warningStatuses.has(status))) {
    return "degraded";
  }
  if (checkStatuses.includes("healthy")) {
    return "healthy";
  }
  return normalizedExplicit || "unknown";
}

export function isProjectScopedExtension(manifest: unknown) {
  const root = objectOrEmpty(manifest);
  const accountScope =
    objectOrEmpty(root.account_scope).scope_type
      ? objectOrEmpty(root.account_scope)
      : objectOrEmpty(objectOrEmpty(root.compiled_manifest).account_scope);
  return stringFrom(accountScope.scope_type).toLowerCase() === "project";
}

export function resolveExtensionDetailPath(extension: ExtensionRecord, projectId: string) {
  const params = new URLSearchParams();
  const normalizedProjectId = stringFrom(projectId);
  if (isProjectScopedExtension(extension) && normalizedProjectId) {
    params.set("project_id", normalizedProjectId);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return `/extensions/${encodeURIComponent(extension.extension_id)}${suffix}#installed`;
}
