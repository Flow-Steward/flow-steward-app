import {
  type ExternalResourcePickerBridge,
  type ExternalResourcePickerHostDeclaration,
} from "@/components/extensions/external-resource-picker";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import type { ExtensionCallbackRegistration } from "@/pages/extensions/api";
import {
  matchesVisibleIf,
  objectOrEmpty,
  stringFrom,
  visibleIfRows,
  type ComponentError,
  type ManifestAction,
} from "@/pages/extensions/extension-manifest-utils";

import "@/pages/extensions/extension-manifest.css";

/** The account whose Disconnect is waiting on its inline confirmation. */
export type PendingConnectionDisconnect = {
  connectionId: string;
  pending: boolean;
  error: string;
};

export type ManifestRenderContext = {
  busy: boolean;
  /** The platform's translator for its own labels; extension text arrives translated. */
  t?: (key: string, defaultText?: string) => string;
  /** Switch to `pageId` and focus the component anchored as `anchorId` there. */
  onNavigateToAnchor?: (pageId: string, anchorId: string) => void;
  canManageProject: boolean;
  connectionCreationBlockedReason: string;
  callbacks: ExtensionCallbackRegistration[];
  callbackStatuses: Record<string, Record<string, unknown>>;
  componentErrors: Record<string, ComponentError>;
  componentQueryParams: Record<string, Record<string, unknown>>;
  componentQueryData: Record<string, Record<string, unknown>>;
  extensionId: string;
  extensionInstallId: string;
  extensionReleaseId: string;
  extensionDescriptorSha256: string;
  formValuesByComponent: Record<string, Record<string, unknown>>;
  externalResourcePickerBridgeFactory?: (
    declaration: ExternalResourcePickerHostDeclaration,
    executeBootstrap: (actionId: string) => Promise<unknown>,
  ) => ExternalResourcePickerBridge;
  oauthConnectionTypes: string[];
  oauthConnectionProviderIds?: string[];
  projectId: string;
  projectConnections: Array<Record<string, unknown>>;
  projectState: Array<Record<string, unknown>>;
  onCallbackSetup: (component: Record<string, unknown>, action: "configure" | "status") => void;
  onComponentQueryParamsChange: (
    componentId: string,
    nextParams: Record<string, unknown>,
  ) => void;
  onFieldChange: (componentId: string, fieldName: string, value: unknown) => void;
  onExternalResourcePickerBootstrap: (
    actionId: string,
    component: Record<string, unknown>,
  ) => Promise<unknown>;
  onManifestAction: (
    action: ManifestAction,
    form: HTMLFormElement | null,
    component: Record<string, unknown>,
    overrides?: {
      target?: Record<string, unknown>;
      values?: Record<string, unknown>;
      data?: Record<string, unknown>;
    },
  ) => void;
  onConnectionSave: (
    component: Record<string, unknown>,
    values: Record<string, unknown>,
  ) => void;
  onConnectionDelete: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) => void;
  onConnectionOAuth: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
    intent: string,
  ) => void;
  onConnectionOAuthDisconnect: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) => void;
  /** Set while one account's Disconnect waits on its inline confirmation. */
  pendingDisconnect?: PendingConnectionDisconnect | null;
  onConnectionOAuthDisconnectConfirm?: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) => void;
  onConnectionOAuthDisconnectCancel?: () => void;
  onConnectionSelect: (
    component: Record<string, unknown>,
    row: Record<string, unknown> | null,
  ) => void;
  onProjectStateSave: (
    component: Record<string, unknown>,
    values: Record<string, unknown>,
  ) => void;
  onProjectStateDelete: (
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) => void;
  onProjectStateSelect: (
    component: Record<string, unknown>,
    row: Record<string, unknown> | null,
  ) => void;
};

export function extensionBrowserAssetUrl({
  extensionId,
  extensionInstallId,
  extensionReleaseId,
  extensionDescriptorSha256,
  projectId,
  assetRef,
}: {
  extensionId: string;
  extensionInstallId: string;
  extensionReleaseId: string;
  extensionDescriptorSha256: string;
  projectId: string;
  assetRef: string;
}): string {
  if (
    !extensionId
    || !extensionInstallId
    || !extensionReleaseId
    || !extensionDescriptorSha256
    || !projectId
  ) {
    return "";
  }
  const encodedAssetRef = assetRef
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  const query = new URLSearchParams({
    extension_install_id: extensionInstallId,
    release_id: extensionReleaseId,
    descriptor_sha256: extensionDescriptorSha256,
    project_id: projectId,
  });
  return `/api/extensions/${encodeURIComponent(extensionId)}/assets/${encodedAssetRef}?${query.toString()}`;
}

export function connectionTypeField(data: Record<string, unknown>): string {
  return stringFrom(data.connection_type_field || data.protocol_field || "");
}

export function connectionTypeVariants(data: Record<string, unknown>): Record<string, unknown> {
  return objectOrEmpty(data.connection_types || data.variants);
}

export function connectionSelectedType(
  data: Record<string, unknown>,
  defaults: Record<string, unknown>,
  values: Record<string, unknown>,
): string {
  const typeField = connectionTypeField(data);
  const variants = connectionTypeVariants(data);
  return stringFrom(
    (typeField ? values[typeField] || defaults[typeField] : "")
      || data.connection_type
      || data.default_connection_type
      || Object.keys(variants)[0],
  );
}

export function connectionVariantData(data: Record<string, unknown>, connectionType: string) {
  const variants = connectionTypeVariants(data);
  return objectOrEmpty(variants[connectionType]);
}

export function fieldVisibleForValues(field: Record<string, unknown>, values: Record<string, unknown>) {
  const conditions = visibleIfRows(field.visible_if || field.visibleIf);
  return !conditions.length || conditions.every((condition) => matchesVisibleIf(values, condition));
}

export function manifestActions(value: unknown): ManifestAction[] {
  const actions = objectOrEmpty(value);
  const parsed: Array<ManifestAction | null> = Object.entries(actions)
    .map(([key, rawAction]) => {
      if (!rawAction || typeof rawAction !== "object" || Array.isArray(rawAction)) {
        return null;
      }
      const action = rawAction as Record<string, unknown>;
      const actionId = stringFrom(action.action_id || action.id || key);
      const queryId = stringFrom(action.query_id);
      const kind = stringFrom(action.kind || action.type).toLowerCase() === "query" || queryId ? "query" : "action";
      const label = stringFrom(action.label || action.title || key, key);
      return {
        actionId,
        queryId,
        label,
        kind,
        scope: stringFrom(action.scope),
      } satisfies ManifestAction;
    });
  return parsed.filter(
    (action): action is ManifestAction => Boolean(action && (action.actionId || action.queryId)),
  );
}

export function manifestFormActions(component: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t): ManifestAction[] {
  const uiSchema = objectOrEmpty(component.ui_schema);
  const componentData = objectOrEmpty(component.data);
  const configured = manifestActions(uiSchema.actions);
  const actions: ManifestAction[] = [...configured];
  const seen = new Set(actions.map((item) => `${item.kind}:${item.actionId || item.queryId}`));

  const saveActionId = stringFrom(componentData.save_action);
  if (saveActionId) {
    const key = `action:${saveActionId}`;
    if (!seen.has(key)) {
      actions.unshift({
        actionId: saveActionId,
        queryId: "",
        label: stringFrom(objectOrEmpty(uiSchema.actions).save_label, t("users.modal.save", "Save")),
        kind: "action",
      });
      seen.add(key);
    }
  }

  const testActionId = stringFrom(componentData.test_action);
  if (testActionId) {
    const key = `action:${testActionId}`;
    if (!seen.has(key)) {
      actions.push({
        actionId: testActionId,
        queryId: "",
        label: stringFrom(objectOrEmpty(uiSchema.actions).test_label, t("react.registry.test", "Test")),
        kind: "action",
      });
    }
  }

  return actions;
}

export function manifestSummaryEntries(summaryValue: unknown): Array<{ label: string; value: string }> {
  const summary = objectOrEmpty(summaryValue);
  return Object.entries(summary)
    .filter(([key, value]) => {
      if (key === "provider" || key === "project_id" || value === null || value === undefined || value === "") {
        return false;
      }
      return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
    })
    .map(([key, value]) => {
      let displayValue = stringFromUnknownValue(value);
      if (typeof value === "boolean") {
        displayValue = value ? "Yes" : "No";
      }
      return {
        label: key.replaceAll("_", " "),
        value: displayValue,
      };
    });
}

export function connectionSaveLabel(
  selectedRow: unknown,
  oauthConnection: boolean,
  accountLabel: string, t: I18nContextValue["t"] = fallbackContext.t
): string {
  if (selectedRow) {
    return oauthConnection ? t("react.extensions.save_account_label", "Save account label") : t("react.extensions.save_connection", "Save connection");
  }
  return oauthConnection ? t("react.extensions.add_value0", "Add {value0}", { value0: accountLabel }) : t("react.extensions.create_connection", "Create connection");
}

export function firstPopulatedItems(
  responseItems: Record<string, unknown>[],
  resultItems: Record<string, unknown>[],
  componentItems: Record<string, unknown>[],
): Record<string, unknown>[] {
  if (responseItems.length) {
    return responseItems;
  }
  if (resultItems.length) {
    return resultItems;
  }
  return componentItems;
}

export function platformText(context: ManifestRenderContext, key: string, defaultText: string): string {
  return context.t ? context.t(key, defaultText) : defaultText;
}

export function webhookConfigurationLabel(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (typeof value !== "boolean") {
    return t("project_detail.connections.status.unknown", "unknown");
  }
  return value ? t("react.workflows.configured", "configured") : t("react.workflows.not_configured", "not configured");
}

export function collectManifestFormValues(form: HTMLFormElement | null): Record<string, unknown> {
  if (!form) {
    return {};
  }
  const values: Record<string, unknown> = {};
  const formData = new FormData(form);
  formData.forEach((value, key) => {
    if (values[key] === undefined) {
      values[key] = value;
      return;
    }
    const previous = values[key];
    values[key] = Array.isArray(previous) ? [...previous, value] : [previous, value];
  });
  Array.from(form.elements).forEach((element) => {
    if (!(element instanceof HTMLInputElement) || element.type !== "checkbox" || !element.name) {
      return;
    }
    values[element.name] = element.checked;
  });
  return values;
}
