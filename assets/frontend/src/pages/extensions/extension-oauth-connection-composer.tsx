import { useI18n } from "@/lib/i18n/useI18n";
import { useId, useState } from "react";


import { DsSelect } from "@/components/shared/design-system";
import { isHttpError } from "@/lib/http/errors";
import { isBasicEmailAddress } from "@/lib/utils/string-normalization";
import "@/pages/extensions/extension-manifest.css";
import {
  startExtensionOAuthAuthorization,
  type ExtensionOAuthScope,
  type OAuthConfigurationView,
} from "@/pages/extensions/extension-oauth-api";
import type {
  OAuthConnectionTypeDeclaration,
  OAuthIntentInputDeclaration,
  OAuthProviderDeclaration,
} from "@/pages/extensions/extension-oauth-types";

import {
  authorizationStartGuidance,
  configurationGuidance,
  controlSegment,
  humanize,
  isConfigurationReady,
  record,
  text,
  withInstalledRelease,
} from "./extension-oauth-setup-model";
type ConnectionComposerProps = {
  allowedIntentIds?: readonly string[];
  configuration?: OAuthConfigurationView;
  connectionTypes: readonly OAuthConnectionTypeDeclaration[];
  onAuthorization?: (authorizationUrl: string) => void;
  provider: OAuthProviderDeclaration;
  scope: ExtensionOAuthScope;
  returnPageId: string;
};

type ConnectionFormErrors = Record<string, string>;

function validEmail(value: string): boolean {
  return isBasicEmailAddress(value);
}

export function ConnectionComposer({
  allowedIntentIds,
  configuration,
  connectionTypes,
  onAuthorization,
  provider,
  returnPageId,
  scope,
}: Readonly<ConnectionComposerProps>) {
  const { t } = useI18n();
  const instanceId = useId().replace(/[^A-Za-z0-9_-]/gu, "-");
  const firstType = connectionTypes[0]?.connection_type_id || "";
  const intents = provider.authorization_intents.filter((row) =>
    !allowedIntentIds?.length || allowedIntentIds.includes(row.intent_id),
  );
  const firstIntent = intents[0]?.intent_id || "";
  const [connectionType, setConnectionType] = useState(firstType);
  const [intentId, setIntentId] = useState(firstIntent);
  const [displayName, setDisplayName] = useState("");
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [errors, setErrors] = useState<ConnectionFormErrors>({});
  const intent = intents.find((row) => row.intent_id === intentId) || intents[0];
  const ready = isConfigurationReady(configuration);
  const controlId = (fieldId: string) => `extension-oauth-connection-${instanceId}-${controlSegment(fieldId)}`;
  const errorId = (fieldId: string) => `${controlId(fieldId)}-error`;

  function validate(): ConnectionFormErrors {
    const next: ConnectionFormErrors = {};
    if (!displayName.trim()) {
      next.display_name = t("react.extensions.oauth_final_connection_required", "Connection name is required.");
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

  async function connect(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
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

  if (!connectionTypes.length || !intents.length) {
    return null;
  }

  return (
    <form aria-label={t("react.extensions.connect_an_account", "Connect an account")} className="fs-manifest-inset" noValidate onSubmit={(event) => void connect(event)}>
      <h3 className="fs-manifest-section-title">{t("react.extensions.connect_an_account", "Connect an account")}</h3>
      <div className="fs-manifest-fields">
      {connectionTypes.length > 1 ? (
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
      {intents.length > 1 ? (
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
      </div>
      {!ready ? <p className="fs-manifest-notice">{configuration ? configurationGuidance(configuration.status, t) : t("react.extensions.create_and_save_a_configuration_before_connecting", "Create and save a configuration before connecting.")}</p> : null}
      {error ? <p className="fs-field-helper error" role="alert">{error}</p> : null}
      <div className="fs-manifest-actions">
        <button className="fs-button primary" disabled={!ready || busy} type="submit">
          {busy ? t("react.extensions.starting", "Starting…") : t("react.extensions.connect", "Connect")}
        </button>
      </div>
    </form>
  );
}
