import { useI18n } from "@/lib/i18n/useI18n";
import { useId, useState } from "react";


import { DsSelect, SensitiveInput } from "@/components/shared/design-system";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { isHttpError } from "@/lib/http/errors";
import { isBasicEmailAddress } from "@/lib/utils/string-normalization";
import "@/pages/extensions/extension-manifest.css";
import {
  importExtensionOAuthRefreshToken,
  startExtensionOAuthAuthorization,
  type ExtensionOAuthScope,
  type OAuthConfigurationView,
} from "@/pages/extensions/extension-oauth-api";
import {
  isRefreshTokenImportProvider,
  type OAuthConnectionTypeDeclaration,
  type OAuthIntentInputDeclaration,
  type OAuthProviderDeclaration,
} from "@/pages/extensions/extension-oauth-types";
import { ConnectionSchemaField } from "@/pages/registry/data-connections/connection-schema-field";
import {
  EMPTY_DRAFT,
  draftValue,
  valueForSchemaField,
} from "@/pages/registry/data-connections/connection-page-helpers";
import {
  schemaFields,
  type ConnectionFormField,
} from "@/pages/registry/data-connections/data-connection-helpers";

import {
  authorizationStartGuidance,
  configurationGuidance,
  controlSegment,
  errorCode,
  humanize,
  isConfigurationReady,
  postConnectRetryGuidance,
  record,
  text,
  withInstalledRelease,
} from "./extension-oauth-setup-model";
type ConnectionComposerProps = {
  allowedIntentIds?: readonly string[];
  configuration?: OAuthConfigurationView;
  connectionTypes: readonly OAuthConnectionTypeDeclaration[];
  onAuthorization?: (authorizationUrl: string) => void;
  /** Leaves the replacement form (refresh-token import only). */
  onCancel?: () => void;
  onConnectionsChanged?: () => void | Promise<void>;
  provider: OAuthProviderDeclaration;
  /** Replace this connection's refresh token instead of adding a connection. */
  replaceConnection?: Record<string, unknown>;
  scope: ExtensionOAuthScope;
  returnPageId: string;
};

type ConnectionFormErrors = Record<string, string>;

/** Mirrors the server bound; the server refuses anything longer anyway. */
const REFRESH_TOKEN_MAX_LENGTH = 8192;

/** The connection type's settings, drawn by the same fields a data connection uses. */
function importSettingsFields(
  connectionType: OAuthConnectionTypeDeclaration | undefined,
  values: Record<string, string>,
): ConnectionFormField[] {
  return schemaFields(connectionType?.config_schema || {}, { values })
    .map((field) => ({ ...field, storage: "connection" as const }));
}

/** A replaced connection's current settings; the Core-owned `oauth` part is not a setting. */
function storedImportSettings(row: Record<string, unknown> | undefined): Record<string, string> {
  return Object.fromEntries(
    Object.entries(record(row?.connection_config))
      .filter(([key]) => key !== "oauth")
      .map(([key, value]) => [key, draftValue(value)]),
  );
}

function refreshTokenImportGuidance(error: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  const code = errorCode(error);
  if (code === "oauth_import_conflict_token_rotated") {
    return t("react.extensions.oauth_import_conflict_token_rotated", "The account was not connected because the configuration changed meanwhile, and the provider may have replaced this refresh token. If trying again fails, issue a new refresh token.");
  }
  if (code === "oauth_connection_revision_conflict" || code === "oauth_import_conflict") {
    return t("react.extensions.oauth_import_conflict", "This connection changed while you were editing. Refresh the page and try again.");
  }
  if (code === "oauth_invalid_client") {
    return t("react.extensions.oauth_import_invalid_client", "The provider did not accept this configuration's client credentials. Repair the configuration in Setup, then try again.");
  }
  if (code === "oauth_provider_unavailable") {
    return t("react.extensions.oauth_import_provider_unavailable", "The provider could not be reached. Try again later.");
  }
  if (code === "oauth_scope_required") {
    return t("react.extensions.oauth_import_scope_required", "This refresh token does not grant the access this connection needs. Issue a new one with the required access.");
  }
  return authorizationStartGuidance(
    error,
    t("react.extensions.oauth_import_failed", "The account could not be connected. Check the values and try again."),
    t,
  );
}

function validEmail(value: string): boolean {
  return isBasicEmailAddress(value);
}

export function ConnectionComposer({
  allowedIntentIds,
  configuration,
  connectionTypes,
  onAuthorization,
  onCancel,
  onConnectionsChanged,
  provider,
  replaceConnection,
  returnPageId,
  scope,
}: Readonly<ConnectionComposerProps>) {
  const { t } = useI18n();
  const instanceId = useId().replace(/[^A-Za-z0-9_-]/gu, "-");
  // A provider whose console issues the refresh token connects by pasting it
  // (ADR 0058); every other provider redirects to authorize.
  const imported = isRefreshTokenImportProvider(provider);
  const firstType = text(replaceConnection?.connection_type) || connectionTypes[0]?.connection_type_id || "";
  const intents = provider.authorization_intents.filter((row) =>
    !allowedIntentIds?.length || allowedIntentIds.includes(row.intent_id),
  );
  const firstIntent = intents[0]?.intent_id || "";
  const [connectionType, setConnectionType] = useState(firstType);
  const [intentId, setIntentId] = useState(firstIntent);
  const [displayName, setDisplayName] = useState(text(replaceConnection?.display_name));
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [refreshToken, setRefreshToken] = useState("");
  const [settings, setSettings] = useState<Record<string, string>>(() => storedImportSettings(replaceConnection));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [errors, setErrors] = useState<ConnectionFormErrors>({});
  const intent = intents.find((row) => row.intent_id === intentId) || intents[0];
  const ready = isConfigurationReady(configuration);
  // A replacement must name the revision it was opened at; without a valid one
  // the server could not tell a stale form from a current one, so none is sent.
  const replacementRevision = Number(replaceConnection?.credential_revision);
  const replacementRevisionValid = !replaceConnection
    || (Number.isInteger(replacementRevision) && replacementRevision >= 1);
  const settingsFields = imported
    ? importSettingsFields(connectionTypes.find((row) => row.connection_type_id === connectionType), settings)
    : [];
  const controlId = (fieldId: string) => `extension-oauth-connection-${instanceId}-${controlSegment(fieldId)}`;
  const errorId = (fieldId: string) => `${controlId(fieldId)}-error`;

  function validate(): ConnectionFormErrors {
    const next: ConnectionFormErrors = {};
    if (!displayName.trim()) {
      next.display_name = t("react.extensions.oauth_final_connection_required", "Connection name is required.");
    }
    if (imported && !refreshToken.trim()) {
      next.refresh_token = t("react.extensions.oauth_import_refresh_token_required", "Refresh token is required.");
    }
    for (const input of intent?.user_inputs || []) {
      const value = (inputs[input.input_id] || "").trim();
      if (!value) {
        continue;
      }
      if (input.validation?.max_length && value.length > input.validation.max_length) {
        next[input.input_id] = t("react.extensions.oauth_final_field_maximum", "{label} must be at most {maximum} characters.", { label: humanize(input.input_id), maximum: input.validation.max_length });
      } else if (input.validation?.type === "email" && !validEmail(value)) {
        next[input.input_id] = t("react.extensions.oauth_final_field_email", "{label} must be a valid email address.", { label: humanize(input.input_id) });
      }
    }
    return next;
  }

  function applyErrors(next: ConnectionFormErrors) {
    setErrors(next);
    const first = Object.keys(next)[0];
    if (first) {
      globalThis.document?.getElementById(controlId(first))?.focus();
    }
  }

  /** The typed settings, or the field errors that stop the submit. */
  function importSettingsPayload(next: ConnectionFormErrors): Record<string, unknown> {
    const payload: Record<string, unknown> = {};
    for (const field of settingsFields) {
      try {
        const value = valueForSchemaField(settings[field.key] || "", field, t);
        if (value !== undefined) payload[field.key] = value;
      } catch (error_) {
        next[field.key] = error_ instanceof Error ? error_.message : humanize(field.key);
      }
    }
    return payload;
  }

  async function importRefreshToken() {
    const nextErrors = validate();
    const connectionConfig = importSettingsPayload(nextErrors);
    setNotice("");
    if (Object.keys(nextErrors).length) {
      setError("");
      applyErrors(nextErrors);
      return;
    }
    if (!configuration || !ready || !connectionType || busy || !replacementRevisionValid) {
      return;
    }
    setBusy(true);
    setError("");
    setErrors({});
    try {
      const result = await withInstalledRelease(scope, configuration.configuration_id, () => importExtensionOAuthRefreshToken(scope, {
        configuration_id: configuration.configuration_id,
        connection_type: connectionType,
        display_name: displayName.trim(),
        refresh_token: refreshToken.trim(),
        connection_config: connectionConfig,
        ...(replaceConnection
          ? {
            existing_connection_id: text(replaceConnection.connection_id),
            connection_credential_revision: replacementRevision,
          }
          : {}),
      }));
      // The token is write-only: once sent it is never kept in the page.
      setRefreshToken("");
      await onConnectionsChanged?.();
      if (result.outcome === "connected") {
        if (replaceConnection) {
          onCancel?.();
          return;
        }
        setDisplayName("");
        setSettings({});
        setNotice(t("react.extensions.oauth_import_connected", "Account connected."));
      } else {
        setError(postConnectRetryGuidance(result.outcome, t));
      }
    } catch (error_) {
      const payload = isHttpError(error_) ? record(error_.payload) : {};
      const code = text(payload.error);
      const field = text(payload.field).replace(/^connection_config\./u, "");
      if (code === "invalid_oauth_refresh_token" || code === "oauth_refresh_token_rejected") {
        setError("");
        applyErrors({
          refresh_token: code === "invalid_oauth_refresh_token"
            ? t("react.extensions.oauth_import_refresh_token_format", "Paste the refresh token exactly as issued, without spaces.")
            : t("react.extensions.oauth_import_refresh_token_rejected", "The provider did not accept this refresh token. Check it and try again."),
        });
      } else if (code === "invalid_connection_config" && settingsFields.some((row) => row.key === field)) {
        const label = settingsFields.find((row) => row.key === field)?.label || humanize(field);
        setError("");
        applyErrors({ [field]: t("react.extensions.oauth_final_field_invalid", "{label} is invalid. Check this field and try again.", { label }) });
      } else {
        setError(refreshTokenImportGuidance(error_, t));
      }
    } finally {
      setBusy(false);
    }
  }

  async function connect(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (imported) {
      await importRefreshToken();
      return;
    }
    const normalizedDisplayName = displayName.trim();
    const nextErrors = validate();
    if (Object.keys(nextErrors).length) {
      setError("");
      applyErrors(nextErrors);
      return;
    }
    if (!configuration || !ready || !connectionType || !intent?.intent_id || busy) {
      return;
    }
    setBusy(true);
    setError("");
    setErrors({});
    try {
      const result = await withInstalledRelease(scope, configuration.configuration_id, () => startExtensionOAuthAuthorization(scope, {
        configuration_id: configuration.configuration_id,
        connection_type: connectionType,
        intent_id: intent.intent_id,
        display_name: normalizedDisplayName,
        return_page_id: returnPageId,
        intent_inputs: Object.fromEntries(
          (intent.user_inputs || [])
            .map((input) => [input.input_id, (inputs[input.input_id] || "").trim()])
            .filter(([, value]) => Boolean(value)),
        ),
      }));
      if (!result.authorization_url) {
        throw new Error("missing_authorization_url");
      }
      (onAuthorization || ((url: string) => globalThis.window.location.assign(url)))(result.authorization_url);
    } catch (error_) {
      const payload = isHttpError(error_) ? record(error_.payload) : {};
      const fieldId = text(payload.field);
      const declaredField = payload.error === "invalid_oauth_intent_input_value"
        && (intent.user_inputs || []).some((input) => input.input_id === fieldId);
      if (declaredField) {
        setError("");
        applyErrors({ [fieldId]: t("react.extensions.oauth_final_field_invalid", "{label} is invalid. Check this field and try again.", { label: humanize(fieldId) }) });
      } else {
        setError(authorizationStartGuidance(error_, undefined, t));
      }
    } finally {
      setBusy(false);
    }
  }

  if (!connectionTypes.length || (!imported && !intents.length)) {
    return null;
  }

  const formTitle = replaceConnection
    ? t("react.extensions.oauth_import_replace_value0", "Replace refresh token for {value0}", { value0: displayName || text(replaceConnection.connection_id) })
    : t("react.extensions.connect_an_account", "Connect an account");
  let submitLabel = t("react.extensions.connect", "Connect");
  if (busy) {
    submitLabel = imported
      ? t("react.extensions.oauth_import_connecting", "Connecting…")
      : t("react.extensions.starting", "Starting…");
  } else if (replaceConnection) {
    submitLabel = t("react.extensions.oauth_import_replace", "Replace refresh token");
  }

  return (
    <form aria-label={formTitle} className="fs-manifest-inset" noValidate onSubmit={(event) => void connect(event)}>
      <h3 className="fs-manifest-section-title">{formTitle}</h3>
      <div className="fs-manifest-fields">
      {connectionTypes.length > 1 && !replaceConnection ? (
        <div className="fs-field">
          <label className="fs-field-label" htmlFor={controlId("connection_type")}>{t("react.extensions.connection_type", "Connection type")}</label>
          <DsSelect
            ariaLabel={t("react.extensions.connection_type", "Connection type")}
            id={controlId("connection_type")}
            onChange={setConnectionType}
            options={connectionTypes.map((row) => ({
              value: row.connection_type_id,
              label: row.display_name || humanize(row.connection_type_id),
            }))}
            value={connectionType}
          />
        </div>
      ) : null}
      {!imported && intents.length > 1 ? (
        <div className="fs-field">
          <label className="fs-field-label" htmlFor={controlId("intent")}>{t("react.extensions.authorization_intent", "Authorization intent")}</label>
          <DsSelect
            ariaLabel={t("react.extensions.authorization_intent", "Authorization intent")}
            id={controlId("intent")}
            onChange={setIntentId}
            options={intents.map((row) => ({ value: row.intent_id, label: humanize(row.intent_id) }))}
            value={intentId}
          />
        </div>
      ) : null}
      <div className="fs-field">
        <span className="fs-required-label">
          <label className="fs-field-label" htmlFor={controlId("display_name")}>{t("react.extensions.connection_name", "Connection name")}</label>
          <span aria-hidden="true" className="fs-required">*</span>
        </span>
        <input
          aria-describedby={errors.display_name ? errorId("display_name") : undefined}
          aria-invalid={errors.display_name ? "true" : undefined}
          aria-required="true"
          className="fs-input"
          id={controlId("display_name")}
          maxLength={200}
          onChange={(event) => {
            setDisplayName(event.target.value);
            setErrors((current) => ({ ...current, display_name: "" }));
            setError("");
          }}
          required
          value={displayName}
        />
        {errors.display_name ? <p className="fs-field-helper error" id={errorId("display_name")} role="alert">{errors.display_name}</p> : null}
      </div>
      {(intent?.user_inputs || []).map((input: OAuthIntentInputDeclaration) => (
        <div className="fs-field" key={input.input_id}>
          <label className="fs-field-label" htmlFor={controlId(input.input_id)}>{humanize(input.input_id)}</label>
          <input
            aria-describedby={errors[input.input_id] ? errorId(input.input_id) : undefined}
            aria-invalid={errors[input.input_id] ? "true" : undefined}
            className="fs-input"
            id={controlId(input.input_id)}
            maxLength={input.validation?.max_length}
            onChange={(event) => {
              setInputs((current) => ({ ...current, [input.input_id]: event.target.value }));
              setErrors((current) => ({ ...current, [input.input_id]: "" }));
              setError("");
            }}
            type={input.validation?.type === "email" ? "email" : "text"}
            value={inputs[input.input_id] || ""}
          />
          {errors[input.input_id] ? <p className="fs-field-helper error" id={errorId(input.input_id)} role="alert">{errors[input.input_id]}</p> : null}
        </div>
      ))}
      {imported ? (
        <div className="fs-field">
          <span className="fs-required-label">
            <label className="fs-field-label" htmlFor={controlId("refresh_token")}>{t("react.extensions.oauth_import_refresh_token", "Refresh token")}</label>
            <span aria-hidden="true" className="fs-required">*</span>
          </span>
          <SensitiveInput
            aria-describedby={errors.refresh_token ? errorId("refresh_token") : `${controlId("refresh_token")}-help`}
            aria-invalid={errors.refresh_token ? "true" : undefined}
            aria-required="true"
            ariaLabel={t("react.extensions.oauth_import_refresh_token", "Refresh token")}
            autoComplete="off"
            className="fs-input"
            id={controlId("refresh_token")}
            maxLength={REFRESH_TOKEN_MAX_LENGTH}
            onChange={(event) => {
              setRefreshToken(event.target.value);
              setErrors((current) => ({ ...current, refresh_token: "" }));
              setError("");
            }}
            spellCheck={false}
            value={refreshToken}
          />
          <p className="fs-field-helper" id={`${controlId("refresh_token")}-help`}>
            {t("react.extensions.oauth_import_refresh_token_help", "Paste the refresh token issued in the provider's console. It is stored encrypted and never shown again.")}
          </p>
          {errors.refresh_token ? <p className="fs-field-helper error" id={errorId("refresh_token")} role="alert">{errors.refresh_token}</p> : null}
        </div>
      ) : null}
      {settingsFields.map((field) => (
        <div key={field.key}>
          <ConnectionSchemaField
            draft={{ ...EMPTY_DRAFT, connection_values: settings }}
            field={field}
            updateDraft={(patch) => {
              if (patch.connection_values) setSettings(patch.connection_values);
              setErrors((current) => ({ ...current, [field.key]: "" }));
              setError("");
            }}
          />
          {errors[field.key] ? <p className="fs-field-helper error" role="alert">{errors[field.key]}</p> : null}
        </div>
      ))}
      </div>
      {notice ? <p aria-live="polite" className="fs-manifest-success" role="status">{notice}</p> : null}
      {!replacementRevisionValid ? (
        <p className="fs-manifest-notice" role="alert">
          {t("react.extensions.oauth_import_revision_missing", "This connection's current version is unknown, so its refresh token cannot be replaced here. Refresh the page and try again.")}
        </p>
      ) : null}
      {!ready ? <p className="fs-manifest-notice">{configuration ? configurationGuidance(configuration.status, t) : t("react.extensions.create_and_save_a_configuration_before_connecting", "Create and save a configuration before connecting.")}</p> : null}
      {error ? <p className="fs-field-helper error" role="alert">{error}</p> : null}
      <div className="fs-manifest-actions">
        <button className="fs-button primary" disabled={!ready || busy || !replacementRevisionValid} type="submit">
          {submitLabel}
        </button>
        {onCancel ? (
          <button className="fs-button" disabled={busy} onClick={onCancel} type="button">
            {t("common.cancel", "Cancel")}
          </button>
        ) : null}
      </div>
    </form>
  );
}
