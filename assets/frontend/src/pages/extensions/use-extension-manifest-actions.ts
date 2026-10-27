import { useLayoutEffect, useRef, useState } from "react";
import { useI18n } from "@/lib/i18n/useI18n";
import { type ManifestAction } from "@/pages/extensions/extension-manifest-utils";
import { collectManifestFormValues } from "@/pages/extensions/extension-manifest-render-model";
import {
  resolveExtensionExternalEffect,
  type ExtensionExternalEffect,
  type ExtensionEffectResolutionRef,
  executeExtensionQuery,
  executeExtensionAction,
  refreshExtensionCallbackStatus,
  createExtensionCallbackRegistration,
} from "@/pages/extensions/api";
import { normalizeComponentPayload } from "@/pages/extensions/payload";
import {
  manifestActionOutcome,
  stringFrom,
  contractActionInputs,
  listFromUnknown,
  objectOrEmpty,
} from "@/pages/extensions/extension-manifest-utils";
import { isHttpError } from "@/lib/http/errors";
import {
  type ManifestActionAttempt,
  AMBIGUOUS_EXTERNAL_EFFECT_CODES,
  catalogLabel,
} from "./hub-support";
import {
  externalEffectActionErrorCode,
  manifestActionRequestsReload,
  manifestFailureMessage,
  isConnectionTestAction,
  materializeManifestActionInput,
  manifestActionResult,
  manifestActionHostEffect,
} from "./manifest-action-support";
import { connectionActionTarget } from "./connection-form-support";

import type { ExtensionEffectRecovery } from "./extension-action-result-panel";
import type { useExtensionsHubState } from "./use-extensions-hub-state";
import type { useExtensionLifecycleActions } from "./use-extension-lifecycle-actions";

export function useExtensionManifestActions(
  hubState: ReturnType<typeof useExtensionsHubState>,
  lifecycleActions: ReturnType<typeof useExtensionLifecycleActions>,
) {
  const { t } = useI18n();
  const {
    setComponentError,
    setComponentQueryData,
    setActionResultByExtension,
    pendingManifestActionIntentRef,
    setComponentQueryRevision,
    setComponentErrors,
    componentErrors,
    actionResultByExtension,
    statusById,
    queryClient,
    activePageProjectId,
    setStatusById,
    detailExtension,
    formValuesByComponent,
    activeManifestPage,
    manifestQuery,
    callbacksQuery,
    externalEffectsQuery,
    selectedProjectId,
    extensionInstallId,
    canManageProject,
    setCallbackStatuses,
  } = hubState;
  const { withBusy } = lifecycleActions;

  const scopeKey = JSON.stringify([detailExtension?.extension_id, extensionInstallId, selectedProjectId]);
  const scopeRef = useRef(scopeKey);
  useLayoutEffect(() => {
    scopeRef.current = scopeKey;
    return () => { scopeRef.current = ""; };
  }, [scopeKey]);
  const [recoveryState, setRecoveryState] = useState<Record<string, { busy?: boolean; error?: string; dismissed?: boolean }>>({});
  const recoveryInFlight = useRef(new Set<string>());

  function retainedFor(effect: ExtensionExternalEffect) {
    const retained = pendingManifestActionIntentRef.current?.retained;
    return retained?.scopeKey === scopeKey && retained.effectId === effect.effect_id
      && retained.actionId === effect.operation_id ? retained : undefined;
  }

  async function retryOriginal(effect: ExtensionExternalEffect, resolution: ExtensionEffectResolutionRef) {
    const intent = pendingManifestActionIntentRef.current;
    const retained = retainedFor(effect);
    if (!intent || !retained || resolution.effect_id !== effect.effect_id || resolution.approval_id !== ""
      || scopeRef.current !== scopeKey || !canManageProject) return;
    const extensionKey = detailExtension!.extension_id;
    const originalError = componentErrors[retained.componentId];
    const originalResult = actionResultByExtension[extensionKey];
    const originalStatus = statusById[extensionKey];
    const ownsAmbiguityFeedback = originalResult?.scopeKey === scopeKey
      && manifestActionHostEffect(originalResult.payload)?.effectId === effect.effect_id
      && originalError?.fieldPath === ""
      && originalError.message === t("react.extensions.display_external_effect_ambiguity", "The provider may have accepted this action. Flow Steward did not submit it again. Check the provider status before retrying or starting a new action.");
    const payload = { ...structuredClone(retained.payload), resolution_ref: resolution };
    try {
      const result = await executeExtensionAction(extensionKey, retained.actionId, payload, intent.idempotencyKey);
      if (scopeRef.current !== scopeKey) return;
      const outcome = manifestActionOutcome(result, t);
      setActionResultByExtension((current) => ({ ...current, [extensionKey]: { label: retained.label, ok: outcome.ok, payload: result, scopeKey } }));
      if (outcome.ok && ownsAmbiguityFeedback && pendingManifestActionIntentRef.current === intent) {
        setComponentErrors((current) => {
          if (current[retained.componentId] !== originalError) return current;
          const next = { ...current };
          delete next[retained.componentId];
          return next;
        });
        if (originalStatus === originalError?.message) {
          setStatusById((current) => current[extensionKey] === originalStatus
            ? { ...current, [extensionKey]: `${retained.label} completed.` } : current);
        }
      }
      if (outcome.ok || !AMBIGUOUS_EXTERNAL_EFFECT_CODES.has(externalEffectActionErrorCode(result))) {
        if (pendingManifestActionIntentRef.current === intent) pendingManifestActionIntentRef.current = null;
      }
    } catch (error) {
      if (scopeRef.current !== scopeKey) return;
      const code = externalEffectActionErrorCode(error);
      const failure = manifestFailureMessage(error, undefined, code, `${retained.label} failed.`, t);
      if (!failure.ambiguous && pendingManifestActionIntentRef.current === intent) pendingManifestActionIntentRef.current = null;
      setActionResultByExtension((current) => ({ ...current, [extensionKey]: {
        label: retained.label, ok: false, scopeKey,
        payload: isHttpError(error) ? error.payload : { error: { message: error instanceof Error ? error.message : "Extension action failed." } },
      } }));
    }
  }

  async function recoverEffect(effect: ExtensionExternalEffect, decision?: { action: "approve" | "reject"; comment: string }) {
    const key = `${scopeKey}:${effect.effect_id}`;
    if (!canManageProject || scopeRef.current !== scopeKey || recoveryInFlight.current.has(key)) return;
    if (decision ? !effect.decision_allowed || !decision.comment.trim() : !effect.retry_supported || effect.retry_unavailable_reason === "retry_in_progress" || !effect.resolution_ref || !retainedFor(effect)) return;
    recoveryInFlight.current.add(key);
    setRecoveryState((current) => ({ ...current, [key]: { busy: true } }));
    try {
      const receipt = decision ? await resolveExtensionExternalEffect(selectedProjectId, extensionInstallId, effect.effect_id, {
        observed_version: effect.observed_version, action: decision.action, comment: decision.comment.trim(),
      }) : null;
      if (scopeRef.current !== scopeKey) return;
      const resolution = receipt?.resolution_ref || effect.resolution_ref;
      if (decision?.action === "approve") {
        if (retainedFor(effect)) pendingManifestActionIntentRef.current = null;
        setRecoveryState((current) => ({ ...current, [key]: { dismissed: true } }));
        setActionResultByExtension((current) => {
          const next = { ...current };
          if (manifestActionHostEffect(next[detailExtension!.extension_id]?.payload)?.effectId === effect.effect_id) delete next[detailExtension!.extension_id];
          return next;
        });
        setStatusById((current) => ({ ...current, [detailExtension!.extension_id]: t("react.extensions.effect_recovery_recorded", "Decision recorded.") }));
      } else if (resolution && (!receipt || receipt.action === "reject") && resolution.effect_id === effect.effect_id) {
        await retryOriginal(effect, resolution);
      }
      if (scopeRef.current === scopeKey) await externalEffectsQuery.refetch();
    } catch (error) {
      if (scopeRef.current !== scopeKey) return;
      const message = isHttpError(error) && error.status === 403
        ? t("react.extensions.effect_recovery_forbidden", "You do not have permission to resolve this effect.")
        : t("react.extensions.effect_recovery_failed", "The decision could not be recorded. Refresh and try again.");
      setRecoveryState((current) => ({ ...current, [key]: { error: message } }));
      if (isHttpError(error) && error.status === 409) await externalEffectsQuery.refetch();
    } finally {
      recoveryInFlight.current.delete(key);
      setRecoveryState((current) => ({ ...current, [key]: { ...current[key], busy: false } }));
    }
  }

  const effectRecoveries: ExtensionEffectRecovery[] = (externalEffectsQuery.data?.effects || [])
    .filter((effect) => !recoveryState[`${scopeKey}:${effect.effect_id}`]?.dismissed)
    .map((effect) => {
      const key = `${scopeKey}:${effect.effect_id}`;
      return { effect, canManage: canManageProject, originalRequestAvailable: Boolean(retainedFor(effect)),
        busy: Boolean(recoveryState[key]?.busy), error: recoveryState[key]?.error || "",
        onDecision: (action, comment) => recoverEffect(effect, { action, comment }),
        onRetry: () => recoverEffect(effect),
        onLater: () => setRecoveryState((current) => ({ ...current, [key]: { dismissed: true } })),
      };
    });

  async function executeManifestQueryRequest({
    action,
    componentId,
    extensionKey,
    payload,
    resolvedValues,
    targetId,
  }: {
    action: ManifestAction;
    componentId: string;
    extensionKey: string;
    payload: Record<string, unknown>;
    resolvedValues: Record<string, unknown>;
    targetId: string;
  }) {
    const result = await executeExtensionQuery(extensionKey, targetId, {
      component_id: payload.component_id,
      context: payload.context,
      page_id: payload.page_id,
      params: resolvedValues,
    });
    if (scopeRef.current !== scopeKey) return;
    if (componentId) {
      const normalized =
        result && typeof result === "object" && !Array.isArray(result)
          ? normalizeComponentPayload(result)
          : {};
      setComponentQueryData((current) => ({ ...current, [componentId]: normalized }));
    }
    setActionResultByExtension((current) => ({
      ...current,
      [extensionKey]: { label: action.label, ok: true, payload: result, scopeKey },
    }));
  }

  async function executeManifestMutationRequest({
    action,
    attempt,
    componentId,
    extensionKey,
    payload,
    targetId,
  }: {
    action: ManifestAction;
    attempt: ManifestActionAttempt;
    componentId: string;
    extensionKey: string;
    payload: Record<string, unknown>;
    targetId: string;
  }) {
    attempt.identity = [extensionKey, targetId, componentId].join(":");
    if (
      pendingManifestActionIntentRef.current
      && pendingManifestActionIntentRef.current.identity !== attempt.identity
    ) {
      pendingManifestActionIntentRef.current = null;
    }
    attempt.idempotencyKey =
      (pendingManifestActionIntentRef.current?.retained?.scopeKey === scopeKey
        ? pendingManifestActionIntentRef.current.idempotencyKey : undefined)
      || globalThis.crypto.randomUUID();
    const existing = pendingManifestActionIntentRef.current;
    pendingManifestActionIntentRef.current = existing?.retained?.scopeKey === scopeKey
      && existing.identity === attempt.identity ? existing : {
        identity: attempt.identity, idempotencyKey: attempt.idempotencyKey,
        retained: { scopeKey, actionId: targetId, label: action.label, componentId,
          payload: structuredClone(payload), effectId: "" },
      };
    const result = await executeExtensionAction(
      extensionKey,
      targetId,
      payload,
      attempt.idempotencyKey,
    );
    if (scopeRef.current !== scopeKey) return;
    const outcome = manifestActionOutcome(result, t);
    attempt.failedPayload = outcome.ok ? undefined : result;
    setActionResultByExtension((current) => ({
      ...current,
      [extensionKey]: { label: action.label, ok: outcome.ok, payload: result, scopeKey },
    }));
    if (!outcome.ok) {
      const code = externalEffectActionErrorCode(result);
      if (!AMBIGUOUS_EXTERNAL_EFFECT_CODES.has(code)) pendingManifestActionIntentRef.current = null;
      throw new Error(outcome.message);
    }
    pendingManifestActionIntentRef.current = null;
    if (manifestActionRequestsReload(result)) {
      setComponentQueryRevision((current) => current + 1);
    }
  }

  function clearManifestComponentError(componentId: string) {
    if (!componentId) {
      return;
    }
    setComponentErrors((current) => {
      if (!current[componentId]) {
        return current;
      }
      const next = { ...current };
      delete next[componentId];
      return next;
    });
  }

  async function completeManifestAction(
    action: ManifestAction,
    componentId: string,
    extensionKey: string,
    isTestAction: boolean,
  ) {
    if (scopeRef.current !== scopeKey) return;
    clearManifestComponentError(componentId);
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["extensions", "preview", extensionKey, activePageProjectId] }),
      queryClient.invalidateQueries({ queryKey: ["extensions", "compiled-manifest", extensionKey] }),
    ]);
    if (scopeRef.current !== scopeKey) return;
    setStatusById((current) => ({
      ...current,
      [extensionKey]: isTestAction ? "Connection test successful." : `${action.label} completed.`,
    }));
  }

  function preserveManifestActionIntent(
    attempt: ManifestActionAttempt,
    extensionKey: string,
    targetId: string,
    componentId: string,
    ambiguous: boolean,
  ) {
    attempt.identity ||= [extensionKey, targetId, componentId].join(":");
    if (!ambiguous) {
      pendingManifestActionIntentRef.current = null;
      return;
    }
    const current = pendingManifestActionIntentRef.current;
    if (current?.identity === attempt.identity) return;
    pendingManifestActionIntentRef.current = {
      identity: attempt.identity,
      idempotencyKey:
        attempt.idempotencyKey
        || current?.idempotencyKey
        || globalThis.crypto.randomUUID(),
    };
  }

  function handleManifestActionFailure({
    action,
    attempt,
    componentId,
    error,
    extensionKey,
    targetId,
  }: {
    action: ManifestAction;
    attempt: ManifestActionAttempt;
    componentId: string;
    error: unknown;
    extensionKey: string;
    targetId: string;
  }) {
    if (scopeRef.current !== scopeKey) return;
    const source = attempt.failedPayload ?? error;
    const hostEffect = manifestActionHostEffect(source);
    const retained = pendingManifestActionIntentRef.current?.retained;
    if (retained?.scopeKey === scopeKey && retained.actionId === targetId && hostEffect?.operationId === targetId) {
      retained.effectId = hostEffect.effectId;
    }
    const code = externalEffectActionErrorCode(source);
    const failure = manifestFailureMessage(
      error,
      attempt.failedPayload,
      code,
      `${action.label} failed.`, t
    );
    if (action.kind !== "query") {
      preserveManifestActionIntent(
        attempt,
        extensionKey,
        targetId,
        componentId,
        failure.ambiguous,
      );
    }
    const fallbackPayload = isHttpError(error)
      ? error.payload
      : { error: { message: failure.message } };
    setActionResultByExtension((current) => ({
      ...current,
      [extensionKey]: {
        label: action.label,
        scopeKey,
        ok: false,
        payload: attempt.failedPayload ?? fallbackPayload,
      },
    }));
    if (componentId) {
      setComponentErrors((current) => ({
        ...current,
        [componentId]: { message: failure.message, fieldPath: "" },
      }));
    }
    setStatusById((current) => ({
      ...current,
      [extensionKey]: failure.message,
    }));
    if (failure.ambiguous) void externalEffectsQuery.refetch();
  }

  async function handleManifestAction(
    action: ManifestAction,
    form: HTMLFormElement | null,
    component: Record<string, unknown>,
    overrides?: {
      target?: Record<string, unknown>;
      values?: Record<string, unknown>;
      data?: Record<string, unknown>;
    },
  ) {
    if (!detailExtension?.extension_id) {
      return;
    }
    const extensionKey = detailExtension.extension_id;
    const targetId = action.kind === "query" ? action.queryId || action.actionId : action.actionId;
    if (!targetId) {
      setStatusById((current) => ({ ...current, [extensionKey]: "Extension action is missing an id." }));
      return;
    }
    await withBusy(extensionKey, async () => {
      const isTestAction = isConnectionTestAction(action, component);
      setStatusById((current) => ({
        ...current,
        [extensionKey]: `${action.kind === "query" || isTestAction ? "Running" : "Saving"} ${action.label}...`,
      }));
      const componentId = stringFrom(component.component_id || component.id || component.key);
      const domValues = collectManifestFormValues(form);
      const formValues = {
        ...formValuesByComponent[componentId],
        ...domValues,
      };
      const resolvedValues = overrides?.values || formValues;
      if (
        isTestAction
        && !stringFrom(resolvedValues.connection_id)
      ) {
        setComponentError(component, t("react.extensions.save_the_connection_before_testing_it", "Save the connection before testing it."));
        setStatusById((current) => ({
          ...current,
          [extensionKey]: "Save the connection before testing it.",
        }));
        return;
      }
      const actionTarget =
        overrides?.target
        || connectionActionTarget(component, resolvedValues);
      const basePayload = {
        component_id: componentId,
        context: activePageProjectId ? { project_id: activePageProjectId } : {},
        page_id: stringFrom(activeManifestPage?.page_id || activeManifestPage?.id || activeManifestPage?.key),
        target: actionTarget,
      };
      const declaredInputs = contractActionInputs(manifestQuery.data, targetId);
      const inputCandidates = { ...resolvedValues, ...overrides?.data };
      const materializedInput = declaredInputs === null
        ? null
        : materializeManifestActionInput(
          declaredInputs,
          inputCandidates,
          stringFrom(actionTarget.connection_id),
          new Set(Object.keys(domValues)),
          [
            ...listFromUnknown(component.fields),
            ...listFromUnknown(objectOrEmpty(component.ui_schema).fields),
            ...listFromUnknown(objectOrEmpty(component.ui_schema).sections)
              .flatMap((section) => listFromUnknown(section.fields)),
          ],
        );
      if (materializedInput?.invalid) {
        const message = materializedInput.invalid.required
          ? t("react.extensions.display_field_required", "{field} is required.", { field: catalogLabel(materializedInput.invalid.name) })
          : t("react.extensions.display_field_valid_type", "{field} must be a valid {type}.", { field: catalogLabel(materializedInput.invalid.name), type: materializedInput.invalid.type });
        setComponentError(component, message, materializedInput.invalid.name);
        const canonicalMessage = materializedInput.invalid.required
          ? `${catalogLabel(materializedInput.invalid.name)} is required.`
          : `${catalogLabel(materializedInput.invalid.name)} must be a valid ${materializedInput.invalid.type}.`;
        setStatusById((current) => ({ ...current, [extensionKey]: canonicalMessage }));
        return;
      }
      const declaredInput = materializedInput?.input ?? null;
      const payload = declaredInput === null
        ? {
          ...basePayload,
          values: resolvedValues,
          data: overrides?.data || {},
        }
        : { ...basePayload, input: declaredInput };
      const attempt: ManifestActionAttempt = { identity: "", idempotencyKey: "" };
      try {
        if (action.kind === "query") {
          await executeManifestQueryRequest({
            action,
            componentId,
            extensionKey,
            payload,
            resolvedValues,
            targetId,
          });
        } else {
          await executeManifestMutationRequest({
            action,
            attempt,
            componentId,
            extensionKey,
            payload,
            targetId,
          });
        }
        await completeManifestAction(action, componentId, extensionKey, isTestAction);
      } catch (error) {
        handleManifestActionFailure({
          action,
          attempt,
          componentId,
          error,
          extensionKey,
          targetId,
        });
      }
    });
  }

  async function handleExternalResourcePickerBootstrap(
    actionId: string,
    component: Record<string, unknown>,
  ): Promise<unknown> {
    if (!detailExtension?.extension_id || !actionId) {
      throw new Error(t("react.extensions.external_picker_is_unavailable", "External picker is unavailable."));
    }
    const result = await executeExtensionAction(detailExtension.extension_id, actionId, {
      component_id: stringFrom(component.component_id || component.id || component.key),
      context: activePageProjectId ? { project_id: activePageProjectId } : {},
      page_id: stringFrom(
        activeManifestPage?.page_id
        || activeManifestPage?.id
        || activeManifestPage?.key,
      ),
      input: {},
    });
    const outcome = manifestActionOutcome(result, t);
    if (!outcome.ok) {
      throw new Error(t("react.extensions.external_picker_is_unavailable", "External picker is unavailable."));
    }
    return manifestActionResult(result);
  }

  async function handleCallbackSetup(
    component: Record<string, unknown>,
    setupAction: "configure" | "status",
  ) {
    if (!detailExtension?.extension_id) {
      return;
    }
    const extensionKey = detailExtension.extension_id;
    const callbackId = stringFrom(component.callback_id || component.callbackId);
    const configureActionId = stringFrom(component.configure_action_id || "configure_webhook");
    const statusActionId = stringFrom(component.status_action_id || "get_webhook_status");
    if (!activePageProjectId) {
      setStatusById((current) => ({ ...current, [extensionKey]: "Select a project before configuring callbacks." }));
      return;
    }
    await withBusy(extensionKey, async () => {
      try {
        if (setupAction === "status") {
          const registration = callbacksQuery.data?.find((item) => item.callback_id === callbackId);
          if (!registration?.id) {
            setStatusById((current) => ({ ...current, [extensionKey]: "Create the callback registration before refreshing webhook status." }));
            return;
          }
          setStatusById((current) => ({ ...current, [extensionKey]: "Refreshing webhook status..." }));
          const statusPayload = await refreshExtensionCallbackStatus(extensionKey, registration.id, {
            action_id: statusActionId,
            runtime: { fake_mode: false },
          });
          setCallbackStatuses((current) => ({
            ...current,
            [registration.id]: objectOrEmpty(statusPayload.webhook_status),
            [callbackId]: objectOrEmpty(statusPayload.webhook_status),
          }));
          await callbacksQuery.refetch();
          setStatusById((current) => ({ ...current, [extensionKey]: "Webhook status refreshed." }));
          return;
        }
        setStatusById((current) => ({ ...current, [extensionKey]: "Creating callback registration..." }));
        const registration = await createExtensionCallbackRegistration(extensionKey, callbackId, {
          project_id: activePageProjectId,
          enabled: true,
          configure: true,
          configure_action_id: configureActionId,
        });
        setCallbackStatuses((current) => ({
          ...current,
          [registration.id]: objectOrEmpty(registration.webhook_status),
          [callbackId]: objectOrEmpty(registration.webhook_status),
        }));
        await callbacksQuery.refetch();
        setStatusById((current) => ({ ...current, [extensionKey]: "Callback registered and webhook configured." }));
      } catch (error) {
        if (isHttpError(error)) {
          const payload = objectOrEmpty(error.payload);
          const registrationId = stringFrom(payload.id);
          const webhookStatus = objectOrEmpty(payload.webhook_status);
          if (registrationId || Object.keys(webhookStatus).length) {
            setCallbackStatuses((current) => ({
              ...current,
              ...(registrationId ? { [registrationId]: webhookStatus } : {}),
              [callbackId]: webhookStatus,
            }));
          }
          await queryClient.invalidateQueries({ queryKey: ["extensions", "callbacks", extensionKey, activePageProjectId] });
          const detail = stringFrom(payload.detail || payload.message || error.message);
          setStatusById((current) => ({
            ...current,
            [extensionKey]: detail || "Webhook configuration failed. Polling fallback remains active.",
          }));
          return;
        }
        setStatusById((current) => ({
          ...current,
          [extensionKey]: error instanceof Error ? error.message : "Webhook configuration failed.",
        }));
      }
    });
  }
  return { handleCallbackSetup, handleExternalResourcePickerBootstrap, handleManifestAction, effectRecoveries, scopeKey };
}
