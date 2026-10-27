import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import { isHttpError } from "@/lib/http/errors";
import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";
import {
  previewExtensionOAuthConfiguration,
  saveExtensionOAuthConfiguration,
  type ExtensionOAuthScope,
  type OAuthConfigurationImpact,
  type OAuthConfigurationMutation,
  type OAuthConfigurationView,
} from "@/pages/extensions/extension-oauth-api";
import type { OAuthProviderDeclaration } from "@/pages/extensions/extension-oauth-types";

type FormErrors = Record<string, string>;
type FocusRequest = {
  fieldId: string;
  sequence: number;
};

const STORED_SECRET_MASK = "••••••••••••";

function oauthFieldValue(
  secret: boolean,
  present: boolean,
  replacing: boolean,
  secretValue: string,
  value: string,
): string {
  if (!secret) {
    return value;
  }
  return present && !replacing ? STORED_SECRET_MASK : secretValue;
}

type OAuthConfigurationFormProps = {
  configuration?: OAuthConfigurationView;
  onEdit?: () => void;
  onSaved: (configuration: OAuthConfigurationView, notice: string) => void | Promise<void>;
  provider: OAuthProviderDeclaration;
  scope: ExtensionOAuthScope;
};

function fieldValue(configuration: OAuthConfigurationView | undefined, fieldId: string): string {
  return configuration?.fields.find((field) => field.field_id === fieldId)?.value || "";
}

function secretPresent(configuration: OAuthConfigurationView | undefined, fieldId: string): boolean {
  return configuration?.fields.find((field) => field.field_id === fieldId)?.secret_present === true;
}

function safeSaveError( t: I18nContextValue["t"] = fallbackContext.t): string {
  return t("react.extensions.configuration_could_not_be_saved_check_the_fields_and_try_again", "Configuration could not be saved. Check the fields and try again.");
}

function isCanonicalHttpsOrigin(value: string): boolean {
  if (!value?.trim() || value.trim() !== value) {
    return false;
  }
  try {
    const parsed = new URL(value);
    const hostname = parsed.hostname;
    const hostIsValid = hostname.startsWith("[") && hostname.endsWith("]")
      ? true
      : hostname.length <= 253 && hostname.split(".").every((label) =>
          /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/u.test(label),
        );
    return parsed.protocol === "https:"
      && !parsed.username
      && !parsed.password
      && (!parsed.port || Number(parsed.port) >= 1)
      && hostIsValid
      && parsed.origin === value;
  } catch {
    return false;
  }
}

function declaredBackendCredentialFieldId(
  error: unknown,
  provider: OAuthProviderDeclaration,
): string | null {
  if (!isHttpError(error) || !error.payload || typeof error.payload !== "object" || Array.isArray(error.payload)) {
    return null;
  }
  const payload = error.payload as Record<string, unknown>;
  const fieldId = typeof payload.field_id === "string" ? payload.field_id.trim() : "";
  if (payload.error !== "invalid_credential_field" || !fieldId) {
    return null;
  }
  return provider.credential_fields.some((field) => field.field_id === fieldId) ? fieldId : null;
}

function credentialValidationError(
  field: OAuthProviderDeclaration["credential_fields"][number],
  value: string,
  storedSecret: boolean,
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  const length = (field.secret ? value : value.trim()).length;
  const minimum = field.validation?.min_length || 0;
  const maximum = field.validation?.max_length;
  if (field.required && !storedSecret && !length) {
    return t("react.extensions.oauth_final_field_required", "{label} is required.", { label: field.label });
  }
  if (length && length < minimum) {
    return t("react.extensions.oauth_final_field_minimum", "{label} must be at least {minimum} characters.", { label: field.label, minimum });
  }
  if (maximum && length > maximum) {
    return t("react.extensions.oauth_final_field_maximum", "{label} must be at most {maximum} characters.", { label: field.label, maximum });
  }
  if (
    length &&
    field.validation?.type === "https_origin" &&
    !isCanonicalHttpsOrigin(value.trim())
  ) {
    return t("react.extensions.oauth_final_field_origin", "{label} must be a canonical HTTPS origin.", { label: field.label });
  }
  return "";
}

export function OAuthConfigurationForm({
  configuration,
  onEdit,
  onSaved,
  provider,
  scope,
}: Readonly<OAuthConfigurationFormProps>) {
  const { t } = useI18n();
  const focusSequenceRef = useRef(0);
  const pendingFocusFrameRef = useRef<number | null>(null);
  const pointerSubmitIntentRef = useRef(false);
  const replacementFocusFieldRef = useRef<string | null>(null);
  const instanceId = useId().replace(/[^A-Za-z0-9_-]/gu, "-");
  const controlIdPrefix = `extension-oauth-configuration-${instanceId}-`;
  const [displayName, setDisplayName] = useState(configuration?.display_name || "");
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      provider.credential_fields
        .filter((field) => !field.secret)
        .map((field) => [field.field_id, fieldValue(configuration, field.field_id)]),
    ),
  );
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [replacingSecrets, setReplacingSecrets] = useState<Set<string>>(() => new Set());
  const [errors, setErrors] = useState<FormErrors>({});
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);
  const [touched, setTouched] = useState<Set<string>>(() => new Set());
  const [busy, setBusy] = useState(false);
  const [impact, setImpact] = useState<OAuthConfigurationImpact | null>(null);
  const normalizedDisplayName = displayName.trim();
  const controlId = (fieldId: string) => (
    `${controlIdPrefix}${fieldId.replace(/[^A-Za-z0-9_-]/gu, "-")}`
  );
  const errorId = (fieldId: string) => `${controlId(fieldId)}-error`;

  useLayoutEffect(() => {
    if (!focusRequest) {
      return;
    }
    if (!errors[focusRequest.fieldId]) {
      setFocusRequest(null);
      return;
    }
    if (pendingFocusFrameRef.current !== null) {
      return;
    }
    const target = globalThis.document?.getElementById(
      `${controlIdPrefix}${focusRequest.fieldId.replace(/[^A-Za-z0-9_-]/gu, "-")}`,
    );
    if (!target?.isConnected || target.getAttribute("aria-invalid") !== "true") {
      setFocusRequest(null);
      return;
    }
    target.focus();
    const frameId = globalThis.requestAnimationFrame(() => {
      if (pendingFocusFrameRef.current !== frameId) {
        return;
      }
      pendingFocusFrameRef.current = null;
      const settledTarget = globalThis.document?.getElementById(
        `${controlIdPrefix}${focusRequest.fieldId.replace(/[^A-Za-z0-9_-]/gu, "-")}`,
      );
      setFocusRequest((current) => (
        current?.sequence === focusRequest.sequence ? null : current
      ));
      if (
        settledTarget?.isConnected
        && settledTarget.getAttribute("aria-invalid") === "true"
      ) {
        settledTarget.focus();
      }
    });
    pendingFocusFrameRef.current = frameId;
  }, [controlIdPrefix, errors, focusRequest]);

  useLayoutEffect(() => {
    const fieldId = replacementFocusFieldRef.current;
    if (!fieldId || !replacingSecrets.has(fieldId)) {
      return;
    }
    replacementFocusFieldRef.current = null;
    globalThis.document?.getElementById(
      `${controlIdPrefix}${fieldId.replace(/[^A-Za-z0-9_-]/gu, "-")}`,
    )?.focus();
  }, [controlIdPrefix, replacingSecrets]);

  useEffect(() => () => {
    if (pendingFocusFrameRef.current !== null) {
      globalThis.cancelAnimationFrame(pendingFocusFrameRef.current);
      pendingFocusFrameRef.current = null;
    }
  }, []);

  const dirty = useMemo(() => {
    if (!configuration) {
      return Boolean(normalizedDisplayName || Object.values(values).some(Boolean) || Object.values(secretValues).some(Boolean));
    }
    if (normalizedDisplayName !== configuration.display_name) {
      return true;
    }
    return provider.credential_fields.some((field) => {
      if (field.secret) {
        return replacingSecrets.has(field.field_id) && Boolean(secretValues[field.field_id]);
      }
      return (values[field.field_id] || "").trim() !== fieldValue(configuration, field.field_id);
    });
  }, [configuration, normalizedDisplayName, provider.credential_fields, replacingSecrets, secretValues, values]);

  function validate(): FormErrors {
    const next: FormErrors = {};
    if (!normalizedDisplayName) {
      next.display_name = t("react.extensions.oauth_final_configuration_required", "Configuration name is required.");
    }
    for (const field of provider.credential_fields) {
      const value = field.secret ? secretValues[field.field_id] || "" : values[field.field_id] || "";
      const storedSecret = field.secret && secretPresent(configuration, field.field_id) && !replacingSecrets.has(field.field_id);
      const validationError = credentialValidationError(field, value, storedSecret, t);
      if (validationError) {
        next[field.field_id] = validationError;
      }
    }
    return next;
  }

  function mutation(confirmationToken?: string): OAuthConfigurationMutation {
    const publicValues = Object.fromEntries(
      provider.credential_fields
        .filter((field) => !field.secret)
        .map((field) => [field.field_id, (values[field.field_id] || "").trim()]),
    );
    const secretReplacements = Object.fromEntries(
      provider.credential_fields
        .filter((field) => field.secret && (replacingSecrets.has(field.field_id) || !configuration))
        .map((field) => [field.field_id, secretValues[field.field_id] || ""])
        .filter(([, value]) => Boolean(value)),
    );
    return {
      ...(configuration ? {
        configuration_id: configuration.configuration_id,
        expected_revision: configuration.revision,
      } : {}),
      display_name: normalizedDisplayName,
      values: publicValues,
      secret_replacements: secretReplacements,
      ...(confirmationToken ? { confirmation_token: confirmationToken } : {}),
    };
  }

  function applyErrors(next: FormErrors) {
    const fieldId = Object.keys(next)[0];
    cancelPendingFocusRequest();
    focusSequenceRef.current += 1;
    setFocusRequest(fieldId ? {
      fieldId,
      sequence: focusSequenceRef.current,
    } : null);
    setErrors(next);
    setTouched((current) => new Set([...current, ...Object.keys(next)]));
  }

  function cancelPendingFocusRequest() {
    pointerSubmitIntentRef.current = false;
    setFocusRequest(null);
    if (pendingFocusFrameRef.current !== null) {
      globalThis.cancelAnimationFrame(pendingFocusFrameRef.current);
      pendingFocusFrameRef.current = null;
    }
  }

  function validateTouchedField(fieldId: string) {
    const nextErrors = validate();
    if (pointerSubmitIntentRef.current && Object.keys(nextErrors).length) {
      pointerSubmitIntentRef.current = false;
      applyErrors(nextErrors);
      return;
    }
    setTouched((current) => new Set(current).add(fieldId));
    const message = nextErrors[fieldId] || "";
    setErrors((current) => ({ ...current, [fieldId]: message, form: "" }));
  }

  function applySaveError(error: unknown) {
    const fieldId = declaredBackendCredentialFieldId(error, provider);
    if (fieldId) {
      const label = provider.credential_fields.find((field) => field.field_id === fieldId)?.label || t("react.extensions.oauth_final_credential", "Credential");
      applyErrors({ [fieldId]: t("react.extensions.oauth_final_field_invalid", "{label} is invalid. Check this field and try again.", { label }) });
      return;
    }
    setErrors({ form: safeSaveError( t) });
  }

  async function persist(command: OAuthConfigurationMutation, successMessage = t("react.extensions.oauth_final_saved", "Configuration saved.")) {
    const saved = await saveExtensionOAuthConfiguration(scope, command);
    setImpact(null);
    await onSaved(saved, successMessage);
  }

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    pointerSubmitIntentRef.current = false;
    const nextErrors = validate();
    if (Object.keys(nextErrors).length) {
      applyErrors(nextErrors);
      return;
    }
    if (!dirty || busy) {
      return;
    }
    setErrors({});
    setBusy(true);
    try {
      const command = mutation();
      const preview = await previewExtensionOAuthConfiguration(scope, command);
      if (preview.requires_confirmation && preview.confirmation_token) {
        setImpact(preview);
      } else {
        await persist(
          command,
          preview.change_policies.includes("runtime_revalidate")
            ? t("react.extensions.oauth_final_saved_revalidate", "Configuration saved. Runtime credentials will be revalidated on the next use.")
            : t("react.extensions.oauth_final_saved", "Configuration saved."),
        );
      }
    } catch (error) {
      applySaveError(error);
    } finally {
      setBusy(false);
    }
  }

  async function confirmImpact() {
    if (!impact?.confirmation_token || busy) {
      return;
    }
    setBusy(true);
    try {
      await persist(mutation(impact.confirmation_token));
    } catch (error) {
      setImpact(null);
      applySaveError(error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      aria-label={t("react.extensions.oauth_configuration", "OAuth configuration")}
      className="fs-manifest-inset"
      noValidate
      onKeyDownCapture={(event) => {
        if (event.key === "Tab") cancelPendingFocusRequest();
      }}
      onPointerCancelCapture={cancelPendingFocusRequest}
      onPointerDownCapture={(event) => {
        cancelPendingFocusRequest();
        const target = event.target as HTMLElement;
        pointerSubmitIntentRef.current = Boolean(target.closest?.('button[type="submit"]'));
      }}
      onPointerUpCapture={() => {
        pointerSubmitIntentRef.current = false;
      }}
      onSubmit={(event) => void submit(event)}
    >
      <div className="fs-field fs-manifest-measure">
      <span className="fs-required-label">
        <label className="fs-field-label" htmlFor={controlId("display_name")}>{t("react.extensions.configuration_name", "Configuration name")}</label>
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
          cancelPendingFocusRequest();
          onEdit?.();
          setDisplayName(event.target.value);
          setErrors((current) => ({ ...current, display_name: "", form: "" }));
        }}
        onBlur={() => validateTouchedField("display_name")}
        required
        value={displayName}
      />
      {errors.display_name ? <p className="fs-field-helper error" id={errorId("display_name")} role="alert">{errors.display_name}</p> : null}
      </div>

      {provider.credential_fields.map((field) => {
        const present = field.secret && secretPresent(configuration, field.field_id);
        const replacing = !present || replacingSecrets.has(field.field_id);
        const describedBy = [
          `${controlId(field.field_id)}-help`,
          errors[field.field_id] ? errorId(field.field_id) : "",
        ].filter(Boolean).join(" ");
        return (
          <div className="fs-field fs-manifest-measure" key={field.field_id}>
            <span className="fs-required-label">
              <label className="fs-field-label" htmlFor={controlId(field.field_id)}>{field.label}</label>
              {field.required ? <span aria-hidden="true" className="fs-required">*</span> : null}
            </span>
            <p className="fs-field-helper" id={`${controlId(field.field_id)}-help`}>{field.help_text}</p>
            <input
              aria-describedby={describedBy}
              aria-invalid={errors[field.field_id] ? "true" : undefined}
              aria-required={field.required ? "true" : undefined}
              autoComplete={field.autocomplete || "off"}
              className="fs-input"
              disabled={field.secret && !replacing}
              id={controlId(field.field_id)}
              maxLength={field.validation?.max_length}
              minLength={field.validation?.min_length}
              onChange={(event) => {
                cancelPendingFocusRequest();
                onEdit?.();
                const update = field.secret ? setSecretValues : setValues;
                update((current) => ({ ...current, [field.field_id]: event.target.value }));
                setErrors((current) => ({ ...current, [field.field_id]: "", form: "" }));
              }}
              onBlur={() => validateTouchedField(field.field_id)}
              placeholder={field.placeholder}
              required={field.required && replacing}
              type={field.secret ? "password" : "text"}
              value={oauthFieldValue(
                field.secret,
                present,
                replacing,
                secretValues[field.field_id] || "",
                values[field.field_id] || "",
              )}
            />
            {present && !replacing ? (
              <div className="fs-manifest-stored">
                <span>{t("react.extensions.stored_value_will_be_kept", "Stored value will be kept.")}</span>
                <button
                  className="fs-button sm"
                  onClick={() => {
                    onEdit?.();
                    replacementFocusFieldRef.current = field.field_id;
                    setReplacingSecrets((current) => new Set(current).add(field.field_id));
                  }}
                  type="button"
                >
                  {t("react.extensions.replace", "Replace")}</button>
              </div>
            ) : null}
            {present && replacing ? (
              <button
                className="fs-manifest-link"
                onClick={() => {
                  onEdit?.();
                  setSecretValues((current) => ({ ...current, [field.field_id]: "" }));
                  setReplacingSecrets((current) => {
                    const next = new Set(current);
                    next.delete(field.field_id);
                    return next;
                  });
                  setErrors((current) => ({ ...current, [field.field_id]: "", form: "" }));
                }}
                type="button"
              >
                {t("react.extensions.cancel_replacement", "Cancel replacement")}</button>
            ) : null}
            {touched.has(field.field_id) && errors[field.field_id] ? <p className="fs-field-helper error" id={errorId(field.field_id)} role="alert">{errors[field.field_id]}</p> : null}
          </div>
        );
      })}

      {errors.form ? <p className="fs-field-helper error" role="alert">{errors.form}</p> : null}
      <div className="fs-manifest-actions">
        <button
          className="fs-button primary"
          disabled={!dirty || busy}
          type="submit"
        >
          {busy ? t("react.extensions.saving", "Saving…") : t("react.extensions.save_configuration", "Save configuration")}
        </button>
      </div>

      {/* Nothing is removed, but the change reaches accounts outside this form:
          a Dialog that names them, with a primary rather than a danger action. */}
      <ConfirmActionDialog
        confirmLabel={t("react.extensions.confirm_and_save", "Confirm and save")}
        confirmTone="primary"
        consequence={impact?.connections.length === 1
          ? t("react.extensions.oauth_final_reauthorize_one", "{name} will need to authorize again.", { name: impact.connections[0].display_name || impact.connections[0].connection_id })
          : t("react.extensions.oauth_final_reauthorize_many", "These connections will need to authorize again.")}
        eyebrow={t("react.extensions.oauth_configuration", "OAuth configuration")}
        irreversible={false}
        objects={(impact?.connections ?? []).map((connection) => connection.display_name || connection.connection_id)}
        onCancel={() => setImpact(null)}
        onConfirm={() => void confirmImpact()}
        open={Boolean(impact)}
        pending={busy}
        pendingLabel={t("react.extensions.saving", "Saving…")}
        title={t("react.extensions.confirm_configuration_impact", "Confirm configuration impact")}
      />
    </form>
  );
}
