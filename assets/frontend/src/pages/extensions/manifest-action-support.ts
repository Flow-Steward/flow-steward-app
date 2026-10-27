import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  type ContractActionInput,
  contractActionInputAllowsEmptyString,
  stringFrom,
  objectOrEmpty,
  stringListFromUnknown,
  manifestActionOutcome,
  type ManifestAction,
} from "@/pages/extensions/extension-manifest-utils";
import { isHttpError } from "@/lib/http/errors";
import { translatedExtensionActionErrorGuidance } from "@/pages/extensions/extension-action-error-guidance";
import {
  type BrowserAuthorityApproval,
} from "@/components/shared/browser-authority-approval-modal";
import {
  manifestFieldIsSecret,
  manifestFieldWidget,
  manifestFieldPath,
} from "./connection-form-support";
import {
  oauthStartErrorMessage,
  DEFINITIVE_PRE_SPAWN_CODES,
  AMBIGUOUS_EXTERNAL_EFFECT_CODES,
} from "./hub-support";

type MaterializedActionValue =
  | { kind: "skip" }
  | { kind: "value"; value: unknown }
  | { kind: "invalid"; type: string; required?: boolean };

function materializeMissingActionValue(
  declaration: ContractActionInput,
  rawValue: unknown,
  explicitFields: Set<string>,
  field: Record<string, unknown> | undefined,
): MaterializedActionValue {
  const explicitlyBlank =
    rawValue === "" && explicitFields.has(declaration.name);
  if (explicitlyBlank && declaration.required) {
    return { kind: "invalid", type: declaration.type, required: true };
  }
  const acceptsBlankText =
    explicitlyBlank &&
    contractActionInputAllowsEmptyString(declaration) &&
    field !== undefined &&
    !manifestFieldIsSecret(field, new Set()) &&
    ["text", "textarea"].includes(manifestFieldWidget(field));
  return acceptsBlankText ? { kind: "value", value: "" } : { kind: "skip" };
}

function materializeNumericActionValue(
  rawValue: unknown,
  type: "integer" | "number",
): MaterializedActionValue {
  const normalized =
    typeof rawValue === "number" ? String(rawValue) : stringFrom(rawValue);
  const value = normalized ? Number(normalized) : Number.NaN;
  const valid =
    type === "integer" ? Number.isSafeInteger(value) : Number.isFinite(value);
  return valid
    ? { kind: "value", value }
    : { kind: "invalid", type };
}

function materializeBooleanActionValue(
  rawValue: unknown,
): MaterializedActionValue {
  if (typeof rawValue === "boolean") {
    return { kind: "value", value: rawValue };
  }
  const normalized = stringFrom(rawValue).toLowerCase();
  return ["true", "false"].includes(normalized)
    ? { kind: "value", value: normalized === "true" }
    : { kind: "invalid", type: "boolean" };
}

function materializeActionValue(
  declaration: ContractActionInput,
  rawValue: unknown,
  explicitFields: Set<string>,
  field: Record<string, unknown> | undefined,
): MaterializedActionValue {
  if (rawValue === undefined || rawValue === null || rawValue === "") {
    return materializeMissingActionValue(
      declaration,
      rawValue,
      explicitFields,
      field,
    );
  }
  const declaredType = declaration.type.toLowerCase();
  if (declaredType === "integer" || declaredType === "number") {
    return materializeNumericActionValue(rawValue, declaredType);
  }
  if (declaredType === "boolean") {
    return materializeBooleanActionValue(rawValue);
  }
  return { kind: "value", value: rawValue };
}

export function materializeManifestActionInput(
  inputs: ContractActionInput[],
  candidates: Record<string, unknown>,
  connectionId: string,
  explicitFields: Set<string>,
  fields: Array<Record<string, unknown>>,
): {
  input: Record<string, unknown>;
  invalid?: { name: string; type: string; required?: boolean };
} {
  const input: Record<string, unknown> = {};
  const fieldByPath = new Map(fields.map((field) => [manifestFieldPath(field), field]));
  for (const declaration of inputs) {
    const rawValue = declaration.name === "connection_ref"
      ? connectionId
      : candidates[declaration.name];
    const result = materializeActionValue(
      declaration,
      rawValue,
      explicitFields,
      fieldByPath.get(declaration.name),
    );
    if (result.kind === "invalid") {
      return {
        input,
        invalid: {
          name: declaration.name,
          type: result.type,
          required: result.required,
        },
      };
    }
    if (result.kind === "value") {
      input[declaration.name] = result.value;
    }
  }
  return { input };
}

export function isConnectionTestAction(action: ManifestAction, component: Record<string, unknown>): boolean {
  return (
    stringFrom(component.type) === "connection_form"
    && action.actionId === stringFrom(objectOrEmpty(component.data).test_action)
  );
}

export function manifestActionResult(payload: unknown): Record<string, unknown> {
  const entity = objectOrEmpty(payload);
  const response = objectOrEmpty(objectOrEmpty(entity.action).response);
  const nestedResult = objectOrEmpty(response.result);
  if (Object.keys(nestedResult).length) {
    return nestedResult;
  }
  return objectOrEmpty(entity.result);
}

export function manifestActionRequestsReload(payload: unknown): boolean {
  const entity = objectOrEmpty(payload);
  const response = objectOrEmpty(objectOrEmpty(entity.action).response);
  return stringListFromUnknown(response.reload || entity.reload).length > 0;
}

export function manifestActionSucceeded(payload: unknown, t: I18nContextValue["t"] = fallbackContext.t): boolean {
  const entity = objectOrEmpty(payload);
  if (typeof entity.ok === "boolean") {
    return entity.ok;
  }
  const action = objectOrEmpty(entity.action);
  if (Object.keys(action).length) {
    return manifestActionOutcome(payload, t).ok;
  }
  return false;
}

/**
 * The extension's own explanation for a failed action, or "" when the response
 * carried none.
 *
 * The host rejects a failed extension action at the HTTP layer, so the
 * explanation travels inside the response body while `Error.message` says only
 * "Extension action failed". Anything reported to a user has to come from here.
 */
export function extensionActionFailureDetail(error: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!isHttpError(error)) {
    return "";
  }
  const payload = objectOrEmpty(error.payload);
  const details = objectOrEmpty(payload.details);
  const context = objectOrEmpty(details.context);
  const action = objectOrEmpty(payload.action || context.action);
  const response = objectOrEmpty(action.response);
  const message = stringFrom(response.error || response.detail || response.message);
  if (!message) {
    return "";
  }
  const code = stringFrom(response.error_code);
  const actionableGuidance = translatedExtensionActionErrorGuidance(t, message);
  if (actionableGuidance) {
    return actionableGuidance;
  }
  return code && code !== message ? `${message} (${code})` : message;
}

/**
 * What to tell the user when saving a connection failed.
 *
 * A refused pre-save check already holds the correction, so it is passed through
 * as written; everything else falls back to the extension's own explanation
 * before the transport message.
 */
export function connectionSaveErrorMessage(
  error: unknown,
  options: { oauthStartAttempted: boolean }, t: I18nContextValue["t"] = fallbackContext.t
): string {
  if (error instanceof PreSaveActionError) {
    return error.message;
  }
  if (options.oauthStartAttempted) {
    return oauthStartErrorMessage(error, t);
  }
  return extensionActionFailureMessage(error, t("react.extensions.display_failed_save_extension_connection", "Failed to save extension connection."), t);
}

/**
 * A pre-save check that refused the form, carrying the explanation a user can act
 * on. It is a distinct type so the surrounding save handler can tell it apart
 * from a transport failure and pass the message through untouched.
 */
export class PreSaveActionError extends Error {
  action: Record<string, unknown>;

  constructor(message: string, action: Record<string, unknown>) {
    super(message);
    this.name = "PreSaveActionError";
    this.action = action;
  }
}

/**
 * A field the user has to correct, named so the form can mark the control itself
 * rather than leaving a message at the top for the user to match up by hand.
 */
export class RequiredFieldError extends Error {
  fieldPath: string;

  constructor(message: string, fieldPath: string) {
    super(message);
    this.name = "RequiredFieldError";
    this.fieldPath = fieldPath;
  }
}

export function errorFieldPath(error: unknown): string {
  return error instanceof RequiredFieldError ? error.fieldPath : "";
}

export function extensionActionFailureMessage(error: unknown, fallback: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!isHttpError(error)) {
    return error instanceof Error ? error.message : fallback;
  }
  const detail = extensionActionFailureDetail(error, t);
  if (detail) {
    return detail;
  }
  const payload = objectOrEmpty(error.payload);
  return stringFrom(payload.detail || payload.message || error.message, fallback);
}

export function browserAuthorityApprovalFromError(error: unknown): BrowserAuthorityApproval | null {
  if (!isHttpError(error) || error.status !== 412) return null;
  const payload = objectOrEmpty(error.payload);
  const nestedError = objectOrEmpty(payload.error);
  const details = objectOrEmpty(payload.details);
  const candidates = [payload, nestedError, details, objectOrEmpty(nestedError.details)];
  const authority = candidates.find(
    (candidate) => stringFrom(candidate.error) === "browser_authority_approval_required",
  );
  if (!authority) return null;
  const impactHash = stringFrom(authority.impact_hash);
  const extensionId = stringFrom(authority.extension_id);
  if (!/^[a-f0-9]{64}$/.test(impactHash) || !extensionId) return null;
  return {
    extensionId,
    impactHash,
    localAssetRefs: stringListFromUnknown(authority.local_asset_refs),
    remoteOrigins: stringListFromUnknown(authority.remote_origins),
    remoteScriptUrls: stringListFromUnknown(authority.remote_script_urls),
  };
}

export type PendingBrowserAuthorityApproval = {
  approval: BrowserAuthorityApproval;
  cancelMessage: string;
  retry: (impactHash: string) => Promise<void>;
  statusKey: string;
  /** Lets a caller holding its own pending state release it on cancellation. */
  onAbandoned?: () => void;
};

export type PendingManifestActionIntent = {
  identity: string;
  idempotencyKey: string;
  retained?: {
    scopeKey: string;
    actionId: string;
    label: string;
    componentId: string;
    payload: Readonly<Record<string, unknown>>;
    effectId: string;
  };
};

/** Only the reserved host path can associate a retained request with a ledger row. */
export function manifestActionHostEffect(value: unknown) {
  const entity = objectOrEmpty(isHttpError(value) ? value.payload : value);
  const envelope = objectOrEmpty(objectOrEmpty(entity.details).context);
  const action = objectOrEmpty(entity.action || envelope.action);
  const effect = objectOrEmpty(objectOrEmpty(action.response).external_effect);
  if (typeof effect.effect_id !== "string" || !effect.effect_id.trim()
    || typeof effect.operation_id !== "string" || !effect.operation_id
    || typeof effect.claim_version !== "number" || !Number.isInteger(effect.claim_version)
    || effect.claim_version <= 0 || typeof effect.status !== "string") return null;
  return { effectId: effect.effect_id, operationId: effect.operation_id };
}

export function externalEffectActionErrorCode(value: unknown): string {
  const payload = isHttpError(value) ? value.payload : value;
  const entity = objectOrEmpty(payload);
  const error = objectOrEmpty(entity.error);
  const details = objectOrEmpty(entity.details);
  const context = objectOrEmpty(details.context);
  const action = objectOrEmpty(entity.action || context.action);
  const response = objectOrEmpty(action.response);
  const result = objectOrEmpty(response.result);
  const externalEffect = objectOrEmpty(entity.external_effect || response.external_effect);
  const candidates = [
    entity.code,
    typeof entity.error === "string" ? entity.error : "",
    entity.detail,
    error.code,
    error.error,
    error.detail,
    action.detail,
    response.error_code,
    response.external_effect_status,
    result.error_code,
    result.external_effect_status,
    externalEffect.status,
    externalEffect.error_code,
  ];
  for (const candidate of candidates) {
    const code = stringFrom(candidate).toLowerCase();
    if (
      code === "external_effect_timeout_unknown"
      || code === "timeout_unknown"
      || code === "external_effect_replay_persist_failed"
      || code === "external_effect_confirmation_lease_lost"
      || code === "external_effect_idempotency_conflict"
      || code === "external_effect_pre_spawn_setup_failed"
      || code === "invalid_connection"
    ) {
      return code;
    }
  }
  return "";
}

function isDefinitiveProjectedActionFailure(value: unknown): boolean {
  const payload = isHttpError(value) ? value.payload : value;
  const entity = objectOrEmpty(payload);
  const details = objectOrEmpty(entity.details);
  const context = objectOrEmpty(details.context);
  const action = objectOrEmpty(entity.action || context.action);
  if (stringFrom(action.status).toLowerCase() !== "failed") {
    return false;
  }
  const response = objectOrEmpty(action.response);
  const errorCode = stringFrom(response.error_code).toLowerCase();
  const result = objectOrEmpty(response.result);
  const definitelyNoExternalEffect = response.definitely_no_external_effect === true
    || result.definitely_no_external_effect === true;
  if (!errorCode || !definitelyNoExternalEffect) {
    return false;
  }
  const externalEffectStatus = stringFrom(response.external_effect_status).toLowerCase();
  return ![
    "external_effect_timeout_unknown",
    "timeout_unknown",
    "external_effect_replay_persist_failed",
    "external_effect_confirmation_lease_lost",
  ].includes(errorCode) && externalEffectStatus !== "timeout_unknown";
}

export function manifestFailureMessage(
  error: unknown,
  failedPayload: unknown,
  code: string,
  fallbackMessage: string, t: I18nContextValue["t"] = fallbackContext.t
) {
  const transportUncertain = failedPayload === undefined && !isHttpError(error);
  const definitivePreSpawnFailure = DEFINITIVE_PRE_SPAWN_CODES.has(code);
  const definitiveProjectedFailure = isDefinitiveProjectedActionFailure(failedPayload ?? error);
  const gatewayOutcomeUnknown = !definitivePreSpawnFailure
    && !definitiveProjectedFailure
    && isHttpError(error)
    && [408, 502, 503, 504].includes(error.status);
  const ambiguous = transportUncertain
    || gatewayOutcomeUnknown
    || AMBIGUOUS_EXTERNAL_EFFECT_CODES.has(code);
  if (ambiguous) {
    return { ambiguous, message: t("react.extensions.display_external_effect_ambiguity", "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.") };
  }
  if (code === "external_effect_idempotency_conflict") {
    return { ambiguous, message: t("react.extensions.display_external_effect_changed_intent", "This action changed after an uncertain result. It was not sent again. Check the provider status, then submit the changed request as a new action.") };
  }
  return { ambiguous, message: extensionActionFailureMessage(error, fallbackMessage, t) };
}
