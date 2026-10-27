import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Loader2, Search } from "lucide-react";

import {
  DsBadge,
  DsCard,
  DsCheckboxMark,
  OptionalLabel,
  type DsBadgeTone,
} from "@/components/shared/design-system";
import {
  fetchLlmProviderSources,
  fetchLlmProviderStatus,
  refreshLlmProviderModels,
  saveLlmProviderCredentials,
  setLlmProviderModelEnabled,
  type LlmProviderConnectionStatus,
  type LlmProviderModel,
} from "@/pages/extensions/api";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { invalidateModelRoutingQueries, providerErrorMessage } from "@/lib/utils/model-provider-state";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

import "@/pages/extensions/extension-manifest.css";

type Contract = Record<string, unknown>;

/** Verified is the only state a route can use; stale asks for a re-run. */
const VERIFICATION_TONES: Record<string, DsBadgeTone> = {
  verified: "green",
  stale: "orange",
  failed: "danger",
};
type Props = {
  installId: string;
  installEnabled: boolean;
  contract: Contract;
  initialConnectionConfig?: Record<string, unknown>;
  initialRemoteBaseUrl?: string;
  /** The install's runtime mode; only a remote_https install has a runtime endpoint. */
  runtimeMode?: string;
  /** The extension's own help page (vendor support or website URL), if any. */
  documentationUrl?: string;
  onConnectionSaved?: () => Promise<void> | void;
};

function missingCapabilityText(name: string, t: I18nContextValue["t"]): string {
  switch (name) {
    case "chat": return t("react.extensions.display_no_chat", "no chat");
    case "tools": return t("react.extensions.display_no_tool_calling", "no tool calling");
    case "structured_output": return t("react.extensions.display_no_structured_output", "no structured output");
    default: return t("react.extensions.display_no_capability", "no {name}", { name: name.replaceAll("_", " ") });
  }
}

/** "Not supported: no tool calling" from what the provider reports, or "". */
function unsupportedReason(model: LlmProviderModel, t: I18nContextValue["t"] = fallbackContext.t): string {
  const missing = stringList(model.missing_capabilities);
  if (!missing.length) return "";
  return t("react.extensions.not_supported_value0", "Not supported: {value0}", { value0: missing.map((name) => missingCapabilityText(name, t)).join(", ") });
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
}

/** A `{field: text}` block of the contract, keeping only non-empty strings. */
function stringRecord(value: unknown): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value).flatMap(([key, item]) =>
      typeof item === "string" && item.trim() ? [[key, item.trim()]] : []),
  );
}

/** Only an absolute https link is offered; anything else is not a help page. */
function httpsUrl(value: string): string {
  try {
    return new URL(value).protocol === "https:" ? value : "";
  } catch {
    return "";
  }
}

/** "10:42" today, otherwise a short date and time. */
function checkedAt(value: string | null | undefined): string {
  if (!value) return "";
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return "";
  const today = at.toDateString() === new Date().toDateString();
  return today
    ? at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : at.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

/** A discovery failure the server recorded is a bare category such as `timeout`. */
function isRecordedCheckCategory(message: string): boolean {
  return /^[a-z][a-z_]*$/.test(message.trim());
}

type CheckView = { tone: DsBadgeTone; badge: string; text: string };

/** The last check, in words: connected, which failure, or not checked yet. */
function checkView(lastCheck: LlmProviderConnectionStatus["last_check"], t: I18nContextValue["t"] = fallbackContext.t): CheckView {
  if (!lastCheck?.status) {
    return { tone: "neutral", badge: t("react.extensions.not_checked", "Not checked"), text: t("react.extensions.not_checked_yet", "Not checked yet.") };
  }
  const when = checkedAt(lastCheck.checked_at);
  const suffix = when ? ` ${t("react.extensions.checked_value0", "(checked {value0})", { value0: when })}` : "";
  if (lastCheck.status === "ok") {
    return { tone: "green", badge: t("react.extensions.connected_variant_22965568", "Connected"), text: t("react.extensions.the_provider_accepted_the_settings_value0", "The provider accepted the settings{value0}.", { value0: suffix }) };
  }
  const message = providerErrorMessage(lastCheck.status, t("react.extensions.the_last_check_failed", "The last check failed."), t);
  return { tone: "danger", badge: t("react.extensions.check_failed", "Check failed"), text: `${message}${suffix ? ` ${suffix.trim()}` : ""}` };
}

/**
 * Why a model's last check failed, in words a user can act on, with when it ran.
 * A plan or credit refusal is the provider's decision, not a broken check, so it
 * names the provider and says what to do instead of a bare "failed".
 */
function modelFailure(model: LlmProviderModel, supplier: string, t: I18nContextValue["t"] = fallbackContext.t): { badge: string; text: string } {
  const code = String(model.verification_error_safe || "");
  const when = checkedAt(stringFromUnknownValue(model.verification_started_at ?? model.verified_at) || undefined);
  const suffix = when ? ` ${t("react.extensions.checked_value0", "(checked {value0})", { value0: when })}` : "";
  if (code === "billing_quota_exceeded") {
    return {
      badge: t("react.extensions.not_available", "not available"),
      text: supplier
        ? t("react.extensions.value0_refused_this_model_your_plan_doesn_t_include_it_or_the_account_is_out_of_credit_choose_another_model_or", "{value0} refused this model: your plan doesn't include it, or the account is out of credit. Choose another model, or upgrade or top up your {value1} account.{value2}", { value0: supplier, value1: supplier, value2: suffix })
        : t("react.extensions.the_provider_refused_this_model_your_plan_doesn_t_include_it_or_the_account_is_out_of_credit_choose_another_mo", "The provider refused this model: your plan doesn't include it, or the account is out of credit. Choose another model, or upgrade or top up the provider account.{value0}", { value0: suffix }),
    };
  }
  return { badge: t("react.extensions.failed", "failed"), text: `${providerErrorMessage(code, t("react.extensions.verification_failed", "Verification failed."), t)}${suffix}` };
}

type RowAction = { label: string; ariaLabel: string; enable: boolean; primary: boolean };

/** True once the model passed verification and is in use. */
function isAdded(model: LlmProviderModel): boolean {
  return Boolean(model.enabled) && ["passed", "verified"].includes(model.verification_status || "");
}

/**
 * The one action a model row offers. A checkbox read as "select several, then
 * a bulk action" and the user waited for a button that never came; the row's
 * own button says what happens: Add (verify, then use), Remove, Try again.
 */
function rowAction(model: LlmProviderModel, t: I18nContextValue["t"] = fallbackContext.t): RowAction {
  const name = model.model_id;
  if (model.verification_status === "stale") {
    return { label: t("react.extensions.reverify", "Reverify"), ariaLabel: t("react.extensions.reverify_value0", "Reverify {value0}", { value0: name }), enable: true, primary: false };
  }
  if (model.verification_status === "failed") {
    // A model can miss one check by chance; running the whole suite again is
    // the user's call, never an automatic retry.
    return { label: t("react.workflows.try_again", "Try again"), ariaLabel: t("react.extensions.try_verifying_value0_again", "Try verifying {value0} again", { value0: name }), enable: true, primary: false };
  }
  if (model.enabled) {
    return { label: t("extensions.repeater.remove", "Remove"), ariaLabel: t("react.extensions.remove_value0", "Remove {value0}", { value0: name }), enable: false, primary: false };
  }
  return { label: t("registry.agent_detail.common.add", "Add"), ariaLabel: t("react.extensions.add_value0", "Add {value0}", { value0: name }), enable: true, primary: true };
}

/** "key" for the API key, otherwise the field's own label in lower case. */
function secretNoun(name: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  return name === "api_key" ? "key" : fieldLabel(name, t).toLowerCase();
}

function fieldLabel(name: string, t: I18nContextValue["t"] = fallbackContext.t) {
  if (name === "runtime_bearer_token") return t("react.extensions.runtime_bearer_token", "Runtime bearer token");
  if (name === "api_key") return t("react.extensions.api_key", "API key");
  if (name === "upstream_base_url") return t("react.extensions.base_url", "Base URL");
  const words = name.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Optional fields use the shared note; each visible name labels its control. */
function renderFieldName(name: string, required: boolean, labelId: string, t: I18nContextValue["t"] = fallbackContext.t) {
  if (!required) {
    return <OptionalLabel id={labelId}>{fieldLabel(name, t)}</OptionalLabel>;
  }
  return (
    <span className="fs-required-label">
      <span className="fs-field-label" id={labelId}>{fieldLabel(name, t)}</span>
      <span aria-hidden="true" className="fs-required">*</span>
    </span>
  );
}

export function LlmProviderPanel({
  installId,
  installEnabled,
  contract,
  initialConnectionConfig = {},
  initialRemoteBaseUrl = "",
  runtimeMode = "",
  documentationUrl = "",
  onConnectionSaved,
}: Readonly<Props>) {
  const { t } = useI18n();
  const fieldLabelPrefix = useId();
  const enableGuidanceId = useId();
  const enableGuidance = t("react.extensions.enable_before_checking_connection", "Enable this extension before checking the connection.");
  // Only committed lifecycle state may authorize a follow-up after awaited saves.
  const latestInstallEnabled = useRef(installEnabled);
  useLayoutEffect(() => {
    latestInstallEnabled.current = installEnabled;
  }, [installEnabled]);
  const usesRemoteRuntime = runtimeMode.trim().toLowerCase() === "remote_https";
  const supplier = stringFromUnknownValue(contract.supplier_label).trim();
  const client = useQueryClient();
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [configValues, setConfigValues] = useState<Record<string, string>>({});
  const [remoteBaseUrl, setRemoteBaseUrl] = useState("");
  const [filter, setFilter] = useState("");
  const [showAllModels, setShowAllModels] = useState(false);
  const [status, setStatus] = useState("");
  const [mutationPending, setMutationPending] = useState(false);
  // Models being checked right now. Each row runs on its own: one model's
  // minute-long check does not hold up the others.
  const [checkingModels, setCheckingModels] = useState<ReadonlySet<string>>(new Set());
  const sources = useQuery({ queryKey: ["llm-provider-sources"], queryFn: fetchLlmProviderSources });
  const connection = useQuery({
    queryKey: ["llm-provider-status", installId],
    queryFn: () => fetchLlmProviderStatus(installId),
  });
  const savedSecretNames = useMemo(
    () => new Set((connection.data?.saved_secrets || []).map((secret) => secret.name)),
    [connection.data],
  );
  const lastCheck = connection.isSuccess ? checkView(connection.data?.last_check ?? null, t) : null;
  const requiredSecretNames = useMemo(
    () => stringList(contract.required_provider_secrets),
    [contract.required_provider_secrets],
  );
  const secretNames = useMemo(
    () => [...new Set([...requiredSecretNames, ...stringList(contract.optional_provider_secrets)])]
      // A runtime bearer only authenticates a separately hosted runtime.
      .filter((name) => usesRemoteRuntime || name !== "runtime_bearer_token"),
    [contract.optional_provider_secrets, requiredSecretNames, usesRemoteRuntime],
  );
  const requiredConfigNames = useMemo(
    () => stringList(contract.required_connection_config),
    [contract.required_connection_config],
  );
  const configNames = useMemo(
    () => [...new Set([...requiredConfigNames, ...stringList(contract.optional_connection_config)])],
    [contract.optional_connection_config, requiredConfigNames],
  );
  const configDefaults = useMemo(
    () => stringRecord(contract.connection_config_defaults),
    [contract.connection_config_defaults],
  );
  const configHelp = useMemo(
    () => stringRecord(contract.connection_config_help),
    [contract.connection_config_help],
  );
  const secretHelp = useMemo(
    () => stringRecord(contract.provider_secret_help),
    [contract.provider_secret_help],
  );
  const helpUrl = httpsUrl(documentationUrl.trim());
  const relevant = useMemo(
    () => (sources.data || []).filter((source) => source.extension_install_id === installId),
    [installId, sources.data],
  );
  const discoveredModels = useMemo(
    () => relevant.flatMap((source) => (source.models || []).map((model) => ({ ...model, source }))),
    [relevant],
  );
  // Models the provider says cannot meet Flow Steward's requirements are hidden
  // until asked for; a model already enabled or verified always stays listed.
  const unsupportedCount = useMemo(
    () => discoveredModels.filter((model) => unsupportedReason(model, t) && !model.enabled).length,
    [discoveredModels, t],
  );
  const models = useMemo(
    () => discoveredModels
      .filter((model) => showAllModels || model.enabled || !unsupportedReason(model, t))
      .filter((model) => `${model.model_id} ${model.display_label || ""}`.toLowerCase().includes(filter.toLowerCase())),
    [discoveredModels, filter, showAllModels, t],
  );
  const persistedRemoteBaseUrl = stringFromUnknownValue(
    relevant[0]?.remote_base_url || initialRemoteBaseUrl,
  ).trim();
  const persistedConfig = relevant[0]?.connection_config || initialConnectionConfig;
  const persistedConfigKey = JSON.stringify(
    persistedConfig && typeof persistedConfig === "object" && !Array.isArray(persistedConfig)
      ? persistedConfig
      : {},
  );
  useEffect(() => {
    if (persistedRemoteBaseUrl) {
      setRemoteBaseUrl((current) => current || persistedRemoteBaseUrl);
    }
    const parsed = JSON.parse(persistedConfigKey) as Record<string, unknown>;
    const restored = Object.fromEntries(
      Object.entries(parsed).flatMap(([key, value]) =>
        typeof value === "string" ? [[key, value]] : []),
    );
    if (Object.keys(restored).length) {
      setConfigValues((current) => Object.keys(current).length ? current : restored);
    }
  }, [installId, persistedConfigKey, persistedRemoteBaseUrl]);
  const config = useMemo(
    () => Object.fromEntries(Object.entries(configValues).filter(([, value]) => value.trim().length > 0)),
    [configValues],
  );
  // Saving sends every declared field: an emptied optional field is saved as ""
  // so the server clears it back to the provider's default.
  const savedConfig = useMemo(
    () => Object.fromEntries(configNames.map((name) => [name, String(configValues[name] || "").trim()])),
    [configNames, configValues],
  );
  const populatedSecrets = useMemo(
    () => Object.fromEntries(Object.entries(secrets).filter(([, value]) => value.trim().length > 0)),
    [secrets],
  );
  const requiredConfigMissing = useMemo(
    () => requiredConfigNames.some((name) => !String(configValues[name] || "").trim()),
    [requiredConfigNames, configValues],
  );

  /**
   * Ask the provider for its models: the check that tells whether the saved key
   * and settings work. A failure the server recorded shows in the connection
   * status line, so it is not repeated here; anything else is returned.
   */
  async function runCheck(): Promise<string> {
    if (!latestInstallEnabled.current) return enableGuidance;
    try {
      await refreshLlmProviderModels(installId, config);
      return t("react.extensions.discovery_complete_add_the_models_you_want_to_use", "Discovery complete. Add the models you want to use.");
    } catch (error) {
      const message = error instanceof Error ? error.message : "";
      return isRecordedCheckCategory(message) && connection.isSuccess
        ? ""
        : providerErrorMessage(message, t("react.extensions.discovery_failed", "Discovery failed."), t);
    } finally {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["llm-provider-sources"] }),
        client.invalidateQueries({ queryKey: ["llm-provider-status", installId] }),
      ]);
    }
  }

  async function refresh() {
    if (!latestInstallEnabled.current) {
      setStatus(enableGuidance);
      return;
    }
    if (mutationPending || requiredConfigMissing) return;
    setMutationPending(true);
    setStatus(t("react.extensions.manual_discovering_models", "Discovering models…"));
    try {
      setStatus(await runCheck());
    } finally {
      setMutationPending(false);
    }
  }

  async function save() {
    if (mutationPending || requiredConfigMissing) return;
    const checkAfterSave = installEnabled;
    setMutationPending(true);
    setStatus(t("react.extensions.manual_saving_connection", "Saving connection…"));
    let saveMessage = "";
    try {
      const saved = await saveLlmProviderCredentials(installId, {
        secrets: populatedSecrets,
        connection_config: savedConfig,
        ...(usesRemoteRuntime ? { remote_base_url: remoteBaseUrl.trim() } : {}),
      });
      setSecrets({});
      const unchanged = saved?.changed === false
        || (saved?.changed === undefined && saved?.verification_invalidated === false);
      saveMessage = unchanged
        ? t("react.extensions.manual_no_changes_the_saved_key_and_settings_are_the_same", "No changes. The saved key and settings are the same.")
        : t("react.extensions.manual_connection_saved_previous_verification_is_stale_until_re_verified", "Connection saved; previous verification is stale until re-verified.");
      await Promise.all([
        client.invalidateQueries({ queryKey: ["llm-provider-status", installId] }),
        invalidateModelRoutingQueries(client),
        Promise.resolve(onConnectionSaved?.()),
      ]);
    } catch (error) {
      setStatus(providerErrorMessage(error instanceof Error ? error.message : "", t("react.extensions.save_failed", "Save failed."), t));
      setMutationPending(false);
      return;
    }
    // Disabled setup can save the endpoint needed to Enable, without a check.
    // A disabled-origin save stays save-only even if Enable finishes meanwhile.
    if (!checkAfterSave || !latestInstallEnabled.current) {
      setStatus([
        saveMessage,
        t("react.extensions.settings_saved_without_checking_connection", "Settings saved without checking the connection."),
        ...(!latestInstallEnabled.current ? [enableGuidance] : []),
      ].join(" "));
      setMutationPending(false);
      return;
    }
    setStatus(t("react.extensions.manual_value0_checking_the_connection", "{value0} Checking the connection…", { value0: saveMessage }));
    try {
      const checked = await runCheck();
      setStatus([saveMessage, checked].filter(Boolean).join(" "));
    } finally {
      setMutationPending(false);
    }
  }

  async function toggle(model: LlmProviderModel, enabled: boolean) {
    if (enabled && !latestInstallEnabled.current) {
      setStatus(enableGuidance);
      return;
    }
    if (mutationPending || checkingModels.has(model.id) || model.verification_status === "pending") return;
    setCheckingModels((current) => new Set(current).add(model.id));
    const modelLabel = model.display_label || model.model_id;
    setStatus(enabled ? t("react.extensions.manual_checking_value0_this_can_take_up_to_a_minute", "Checking {value0}… This can take up to a minute.", { value0: modelLabel }) : t("react.extensions.manual_removing_value0", "Removing {value0}…", { value0: modelLabel }));
    try {
      await setLlmProviderModelEnabled(model.id, enabled);
      await invalidateModelRoutingQueries(client);
      setStatus(enabled ? t("react.extensions.manual_value0_added_it_passed_the_checks", "{value0} added: it passed the checks.", { value0: modelLabel }) : t("react.extensions.manual_value0_removed", "{value0} removed.", { value0: modelLabel }));
    } catch (error) {
      // "Capability verification failed: <check>" names the failed check; say
      // it in words, or keep the server's sentence when the check is unknown.
      const message = error instanceof Error ? error.message : "";
      const check = message.replace(/^Capability verification failed:\s*/, "");
      setStatus(providerErrorMessage(check, "", t) || providerErrorMessage(message, t("react.extensions.verification_failed", "Verification failed."), t));
    } finally {
      try {
        await client.invalidateQueries({ queryKey: ["llm-provider-sources"] });
      } finally {
        setCheckingModels((current) => {
          const next = new Set(current);
          next.delete(model.id);
          return next;
        });
      }
    }
  }

  return (
    <DsCard
      description={usesRemoteRuntime
        ? t("react.extensions.save_the_public_extension_runtime_endpoint_separately_from_provider_credentials_and_upstream_configuration_dis", "Save the public extension runtime endpoint separately from provider credentials and upstream configuration, discover models, then add the ones you want to use.")
        : t("react.extensions.connect_the_provider_discover_its_models_then_add_the_ones_you_want_to_use", "Connect the provider, discover its models, then add the ones you want to use.")}
      title={t("react.extensions.llm_provider", "LLM provider")}
    >
      <div className="fs-manifest-fields">
        {usesRemoteRuntime ? (
          <label className="fs-field">
            <span className="fs-field-label">{t("react.extensions.runtime_endpoint_url", "Runtime endpoint URL")}</span>
            <input className="fs-input" onChange={(event) => setRemoteBaseUrl(event.target.value)} placeholder="https://llm-runtime.your-company.com" value={remoteBaseUrl}/>
          </label>
        ) : null}
        {configNames.map((name, index) => {
          const required = requiredConfigNames.includes(name);
          const labelId = `${fieldLabelPrefix}-config-${index}`;
          // The extension's declared default shows what an empty field means.
          const placeholder = configDefaults[name]
            || (required ? fieldLabel(name, t) : t("react.extensions.manual_leave_empty_for_the_provider_s_default", "Leave empty for the provider's default"));
          const helpId = configHelp[name] ? `llm-config-help-${name}` : undefined;
          return (
            <label className="fs-field" key={name}>
              {renderFieldName(name, required, labelId, t)}
              <input aria-describedby={helpId} aria-labelledby={labelId} className="fs-input" onChange={(event) => setConfigValues((current) => ({ ...current, [name]: event.target.value }))} placeholder={placeholder} required={required} value={configValues[name] || ""}/>
              {helpId ? <span className="fs-field-helper" id={helpId}>{configHelp[name]}</span> : null}
            </label>
          );
        })}
        {secretNames.map((name, index) => {
          const helpId = secretHelp[name] ? `llm-secret-help-${name}` : undefined;
          const labelId = `${fieldLabelPrefix}-secret-${index}`;
          const saved = savedSecretNames.has(name);
          // Only names come back from the server: whether a value is saved,
          // never the value. "No key saved" when the field matters.
          const savedText = saved
            ? t("react.extensions.value0_saved", "{value0} saved", { value0: fieldLabel(name, t) })
            : requiredSecretNames.includes(name) || secretHelp[name]
              ? t("react.extensions.no_value0_saved", "No {value0} saved", { value0: fieldLabel(name, t) })
              : "";
          const savedId = connection.isSuccess && savedText ? `llm-secret-saved-${name}` : undefined;
          const describedBy = [savedId, helpId].filter(Boolean).join(" ") || undefined;
          return (
            <label className="fs-field" key={name}>
              {renderFieldName(name, requiredSecretNames.includes(name), labelId, t)}
              <input aria-describedby={describedBy} aria-labelledby={labelId} autoComplete="off" className="fs-input" onChange={(event) => setSecrets((current) => ({ ...current, [name]: event.target.value }))} placeholder={saved ? t("react.extensions.saved_enter_a_new_value0_to_replace_it", "Saved. Enter a new {value0} to replace it", { value0: secretNoun(name, t) }) : fieldLabel(name, t)} type="password" value={secrets[name] || ""}/>
              {savedId ? <span className={!saved && requiredSecretNames.includes(name) ? "fs-field-helper error" : "fs-field-helper"} id={savedId}>{savedText}</span> : null}
              {helpId ? <span className="fs-field-helper" id={helpId}>{secretHelp[name]}</span> : null}
            </label>
          );
        })}
        {helpUrl ? (
          <a className="fs-manifest-link" href={helpUrl} rel="noreferrer" target="_blank">
            {t("react.extensions.how_to_fill_this_in", "How to fill this in")}<span className="fs-sr-only"> {" "}{t("react.extensions.opens_in_a_new_tab", "(opens in a new tab)")}</span>
          </a>
        ) : null}
      </div>
      {lastCheck ? (
        <p className="fs-manifest-description">
          <DsBadge tone={lastCheck.tone}>{lastCheck.badge}</DsBadge>{" "}
          <span>{lastCheck.text}</span>
        </p>
      ) : null}
      {!installEnabled ? (
        <>
          <p className="fs-manifest-description" id={enableGuidanceId}>{enableGuidance}</p>
          <p className="fs-field-helper">{t("react.extensions.settings_can_be_saved_before_enabling", "You can save settings before enabling this extension.")}</p>
        </>
      ) : null}
      <div className="fs-manifest-actions">
        <button className="fs-button sm primary" disabled={mutationPending || requiredConfigMissing} onClick={() => ignoreAsyncResult(save())} type="button">{t("react.extensions.save_connection", "Save connection")}</button>
        <button aria-describedby={!installEnabled ? enableGuidanceId : undefined} className="fs-button sm" disabled={!installEnabled || mutationPending || requiredConfigMissing} onClick={() => ignoreAsyncResult(refresh())} type="button">{t("react.extensions.discover_models", "Discover models")}</button>
      </div>
      {status ? <p aria-live="polite" className="fs-manifest-description" role="status">{status}</p> : null}
      <label className="fs-search fs-llm-search">
        <Search aria-hidden="true" />
        <input aria-label={t("react.extensions.search_provider_models", "Search provider models")} onChange={(event) => setFilter(event.target.value)} placeholder={t("react.extensions.search_discovered_models", "Search discovered models")} type="search" value={filter}/>
      </label>
      {unsupportedCount ? (
        <label className="fs-checkbox-label">
          <input checked={showAllModels} onChange={(event) => setShowAllModels(event.target.checked)} type="checkbox"/>
          <DsCheckboxMark />
          <span>{t("react.extensions.show_all_models", "Show all models (")}{unsupportedCount} {" "}{t("react.extensions.not_supported", "not supported)")}</span>
        </label>
      ) : null}
      {sources.isLoading ? <p className="fs-empty-copy">{t("react.extensions.loading_provider_models", "Loading provider models…")}</p> : null}
      {sources.isError ? (
        <div className="fs-manifest-list">
          <p className="fs-field-helper error" role="alert">{t("react.extensions.could_not_load_provider_models", "Could not load provider models.")}</p>
          <div className="fs-manifest-actions">
            <button className="fs-button sm" onClick={() => ignoreAsyncResult(sources.refetch())} type="button">
              {t("react.extensions.retry_loading_models", "Retry loading models")}</button>
          </div>
        </div>
      ) : null}
      {sources.isSuccess && discoveredModels.length === 0 ? (
        <div className="fs-manifest-empty"><p className="fs-empty-copy">{t("react.extensions.no_models_discovered_yet", "No models discovered yet.")}</p></div>
      ) : null}
      {sources.isSuccess && discoveredModels.length > 0 && models.length === 0 ? (
        <div className="fs-manifest-empty"><p className="fs-empty-copy">{filter.trim() ? t("react.extensions.no_models_match_this_search", "No models match this search.") : t("react.extensions.no_model_reports_tool_calling_and_structured_output_show_all_models_to_try_one_anyway", "No model reports tool calling and structured output. Show all models to try one anyway.")}</p></div>
      ) : null}
      {models.length ? (
        <div className="fs-manifest-list">
          {models.map((model) => {
            const checking = checkingModels.has(model.id) || model.verification_status === "pending";
            const action = rowAction(model, t);
            const verificationUnavailable = action.enable && !installEnabled;
            const added = isAdded(model);
            const hintId = `llm-model-hint-${model.id}`;
            const failure = model.verification_error_safe ? modelFailure(model, supplier, t) : null;
            return (
              <div className={failure && !checking ? "fs-manifest-row is-error" : "fs-manifest-row"} key={model.id}>
                <span className="fs-manifest-row-copy">
                  <span className="fs-manifest-row-name">{model.display_label || model.model_id}</span>
                  {checking ? (
                    <span className="fs-manifest-row-detail" id={hintId}>
                      {t("react.extensions.checking_chat_tool_calling_and_structured_output_this_can_take_up_to_a_minute", "Checking chat, tool calling and structured output. This can take up to a minute.")}</span>
                  ) : null}
                  {!checking && failure ? (
                    <span className="fs-manifest-row-detail is-error">{failure.text}</span>
                  ) : null}
                  {!checking && !model.verification_error_safe && unsupportedReason(model, t) ? (
                    <span className="fs-manifest-row-detail">{unsupportedReason(model, t)}</span>
                  ) : null}
                </span>
                <div className="fs-manifest-actions">
                  <DsBadge tone={added ? "green" : model.verification_error_safe === "billing_quota_exceeded" && !checking ? "orange" : VERIFICATION_TONES[model.verification_status || "unverified"] ?? "neutral"}>
                    {added ? t("project_edit.diff.added", "Added") : checking ? t("react.extensions.checking_variant_7f98506a", "checking") : model.verification_status === "failed" && failure ? failure.badge : model.verification_status || t("react.extensions.not_added", "not added")}
                  </DsBadge>
                  {/* One button per row, kept while it is busy so focus stays on
                      it: aria-disabled rather than disabled, which would drop focus. */}
                  <button
                    aria-busy={checking || undefined}
                    aria-describedby={[checking ? hintId : "", verificationUnavailable ? enableGuidanceId : ""].filter(Boolean).join(" ") || undefined}
                    aria-disabled={checking || mutationPending || verificationUnavailable || undefined}
                    aria-label={checking ? t("react.extensions.checking_value0", "Checking {value0}", { value0: model.model_id }) : action.ariaLabel}
                    className={action.primary && !checking ? "fs-button sm primary" : "fs-button sm"}
                    onClick={() => {
                      if (!checking && !mutationPending && !verificationUnavailable) ignoreAsyncResult(toggle(model, action.enable));
                    }}
                    type="button"
                  >
                    {checking ? (
                      <>
                        <Loader2 aria-hidden="true" className="fs-spinner-icon" />
                        {t("react.extensions.checking", "Checking…")}</>
                    ) : action.label}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
    </DsCard>
  );
}
