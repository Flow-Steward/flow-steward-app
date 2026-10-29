import { ChevronLeft, ChevronRight, Plus } from "lucide-react";
import { useState } from "react";

import { DsAlert, DsBadge, DsCheckbox } from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { InlineConfirm } from "@/components/shared/inline-confirm";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
 

import {
  formatJson,
  triggerUiFieldPath,
  triggerUiFields,
  triggerUiFieldWidget,
  webhookCurlExample,
  type WorkflowWebhookTrigger,
} from "@/pages/workflows/detail/detail-helpers";
import { DrawerFooter, DrawerFooterSpacer } from "@/pages/workflows/detail/detail-drawer-footer";
import { SignedWebhookAuthFields } from "@/pages/workflows/detail/detail-signed-webhook-fields";
import {
  SIGNED_WEBHOOK_AUTH_MODE,
  SIGNED_WEBHOOK_SECRET_IMPORTED,
  defaultSignedWebhookBuilderDraft,
} from "@/pages/workflows/detail/detail-webhook-signature-builder";
import {
  WEBHOOK_PRESET_CONTENT_TYPES,
  WEBHOOK_PRESET_IDEMPOTENCY_MODE,
  localizedWebhookPresets,
  webhookPresetFromSaved,
  webhookPresetImportsSecret,
  webhookPresetLabel,
  webhookPresetOf,
  webhookPresetSecretDirection,
  webhookPresetSignatureDraft,
  type WebhookPresetId,
} from "@/pages/workflows/detail/detail-webhook-presets";

import {
  CONTENT_TYPES,
  countLabel,
  humanizeKey,
  providerReadiness,
  providerSaveLabel,
  renderWhen,
  requiredFieldPaths,
  webhookReadinessLabel,
  webhookSaveLabel,
  type ProviderTriggerCatalogEntry,
  type ProviderTriggerRow,
  type ProviderTriggerUiField,
  type TriggersView,
  type WebhookExpectedInput,
  type WorkflowTriggersSurfaceContext,
} from "@/pages/workflows/detail/detail-triggers-parts";
import { SettingsSection, TriggerChoice } from "@/pages/workflows/detail/detail-triggers-sections";

import "./detail-triggers.css";

export function WorkflowTriggersSurface({
  ctx,
}: Readonly<{
  ctx: WorkflowTriggersSurfaceContext;
}>) {
  const { t } = useI18n();
  const [view, setView] = useState<TriggersView>("list");
  const [forceCustomPreset, setForceCustomPreset] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [webhookConfirm, setWebhookConfirm] = useState<"" | "rotate" | "regenerate">("");
  const saveLoader = useLoaderDelay(Boolean(ctx.triggerSavePending));
  if (ctx.drawerMode !== "workflow-settings" || ctx.drawerTab !== "triggers") return null;
  const {
    copiedCredentialReveal,
    copiedWebhookKey,
    expectedWebhookInputs,
    handleCopyCredentialReveal,
    handleCopyWebhookUrl,
    handleCreateProviderTrigger,
    handleCreateWebhookTrigger,
    handleDeleteProviderTrigger,
    handleDeleteWebhook,
    handleDryRunWebhook,
    handleRegenerateWebhookUrl,
    handleRotateWebhookCredential,
    handleSaveProviderTrigger,
    handleSaveWebhookTrigger,
    lastWebhookCredentialReveal,
    loadWebhookEditorDrafts,
    providerProjectReady,
    providerProjectResolutionPending,
    providerProjectUnavailable,
    providerTriggerCatalog,
    providerTriggerCatalogIdDraft,
    providerTriggerDisplayNameDraft,
    providerTriggerEditorMode,
    providerTriggerEnabledDraft,
    providerTriggerFormDraft,
    providerTriggers,
    resetProviderTriggerDrafts,
    resetWebhookEditorDrafts,
    selectedProviderCatalogEntry,
    selectedProviderTrigger,
    setProviderTriggerCatalogIdDraft,
    setProviderTriggerDisplayNameDraft,
    setProviderTriggerEditorMode,
    setProviderTriggerEnabledDraft,
    setProviderTriggerFormDraft,
    setSelectedProviderTriggerId,
    setSelectedWebhookKey,
    setWebhookAllowedContentTypesDraft,
    setWebhookAuthConfigDraft,
    setWebhookAuthModeDraft,
    setWebhookDescriptionDraft,
    setWebhookDryRunBodyDraft,
    setWebhookDryRunFilesDraft,
    setWebhookDryRunFormDraft,
    setWebhookDryRunHeadersDraft,
    setWebhookDryRunQueryDraft,
    setWebhookEditorMode,
    setWebhookEnabledDraft,
    setWebhookIdempotencyModeDraft,
    setWebhookImportedSigningSecretDraft,
    setWebhookInputMappingDraft,
    setWebhookMaxBodyBytesDraft,
    setWebhookMaxFileBytesDraft,
    setWebhookMaxFilesDraft,
    setWebhookResponseModeDraft,
    setWebhookSecretDirectionDraft,
    setWebhookSignatureBuilderDraft,
    setWebhookSourceCidrsDraft,
    setWebhookSourceExactIpsDraft,
    triggerNotice,
    triggerSavePending,
    webhookAllowedContentTypesDraft,
    webhookAuthConfigDraft,
    webhookAuthModeDraft,
    webhookImportedSigningSecretDraft,
    webhookSecretDirectionDraft,
    webhookSignatureBuilderDraft,
    webhookSignatureBuilderSupported,
    webhookDescriptionDraft,
    webhookDryRunBodyDraft,
    webhookDryRunFilesDraft,
    webhookDryRunFormDraft,
    webhookDryRunHeadersDraft,
    webhookDryRunQueryDraft,
    webhookEditorMode,
    webhookEnabledDraft,
    webhookFieldAutomapSupported,
    webhookIdempotencyModeDraft,
    webhookInputMappingDraft,
    webhookMappingPlaceholder,
    webhookMaxBodyBytesDraft,
    webhookMaxFileBytesDraft,
    webhookMaxFilesDraft,
    webhookResponseModeDraft,
    webhookSourceCidrsDraft,
    webhookSourceExactIpsDraft,
    workflowTriggersQuery,
  } = ctx;

  const selectedWebhook = (ctx.selectedWebhook || null) as WorkflowWebhookTrigger | null;
  const selectedProviderCatalogEntryView = (selectedProviderCatalogEntry || {}) as ProviderTriggerCatalogEntry;
  const selectedProviderTriggerView = (selectedProviderTrigger || {}) as ProviderTriggerRow;
  const webhooks = (workflowTriggersQuery.data?.incoming_webhooks as WorkflowWebhookTrigger[]) || [];
  const providerRows = (providerTriggers || []) as ProviderTriggerRow[];
  const catalog = (providerTriggerCatalog || []) as ProviderTriggerCatalogEntry[];
  // An event in a connected app starts the workflow: offered only when an
  // installed extension declares such events or one is already bound here.
  const showProviderTriggers = catalog.length > 0 || providerRows.length > 0;
  const editingWebhook = Boolean(webhookEditorMode === "edit" && selectedWebhook);
  const editingProvider = Boolean(providerTriggerEditorMode === "edit" && selectedProviderTrigger);
  const pending = Boolean(triggerSavePending);

  const preset: WebhookPresetId = webhookPresetOf({
    authMode: webhookAuthModeDraft,
    contentTypes: webhookAllowedContentTypesDraft,
    idempotencyMode: webhookIdempotencyModeDraft,
    signature: webhookAuthModeDraft === SIGNED_WEBHOOK_AUTH_MODE && webhookSignatureBuilderSupported ? webhookSignatureBuilderDraft : null,
    forceCustom: forceCustomPreset,
  });
  const presetName = webhookPresetLabel(preset, t);

  function openList() {
    setWebhookConfirm("");
    setView("list");
  }

  function openWebhook(webhook: WorkflowWebhookTrigger | null) {
    setForceCustomPreset(false);
    setWebhookConfirm("");
    if (webhook) {
      setSelectedWebhookKey(webhook.entry_key);
      setWebhookEditorMode("edit");
      loadWebhookEditorDrafts(webhook);
      setAdvancedOpen(webhookPresetFromSaved(webhook) === "custom");
    } else {
      setWebhookEditorMode("create");
      resetWebhookEditorDrafts();
      setAdvancedOpen(false);
    }
    setView("webhook");
  }

  function openProvider(trigger: ProviderTriggerRow | null) {
    if (trigger) {
      setSelectedProviderTriggerId(trigger.trigger_id);
      setProviderTriggerEditorMode("edit");
    } else {
      setProviderTriggerEditorMode("create");
      resetProviderTriggerDrafts();
    }
    setView("provider");
  }

  /** A preset fills the settings it implies; any other setting makes the webhook Custom. */
  function applyPreset(id: WebhookPresetId) {
    if (id === "custom") {
      setForceCustomPreset(true);
      setAdvancedOpen(true);
      return;
    }
    setForceCustomPreset(false);
    setWebhookAllowedContentTypesDraft([...WEBHOOK_PRESET_CONTENT_TYPES]);
    setWebhookIdempotencyModeDraft(WEBHOOK_PRESET_IDEMPOTENCY_MODE);
    if (id === "myapp") {
      setWebhookAuthModeDraft("bearer_token");
      return;
    }
    setWebhookAuthModeDraft(SIGNED_WEBHOOK_AUTH_MODE);
    const signature = webhookPresetSignatureDraft(id);
    if (signature) setWebhookSignatureBuilderDraft(signature);
    setWebhookSecretDirectionDraft(webhookPresetSecretDirection(id));
  }

  const backButton = (
    <Button className="workflow-triggers-back" onClick={openList} size="sm" type="button" variant="ghost">
      <ChevronLeft aria-hidden="true" />
      {t("react.workflows.triggers_variant_e62f2148", "Triggers")}
    </Button>
  );

  const notice = triggerNotice ? <DsAlert title={triggerNotice} /> : null;

  if (view === "list") {
    return (
      <div className="workflow-triggers" id="workflow-triggers-surface">
        {notice}
        <div className="workflow-triggers-group">
          <span className="workflow-triggers-micro">{t("react.workflows.triggers_variant_e62f2148", "Triggers")}</span>
          <p className="workflow-triggers-help">
            {showProviderTriggers
              ? t("react.workflows.triggers_intro", "Start this workflow from outside: a URL that services call, or an event in a connected app.")
              : t("react.workflows.triggers_intro_webhooks", "Start this workflow from outside: a URL that another service or your own code calls.")}</p>
          <section aria-label={t("react.workflows.incoming_webhooks", "Incoming webhooks")} className="workflow-triggers-group">
            <div className="workflow-triggers-block-head">
              <span className="workflow-triggers-block-title">{t("react.workflows.incoming_webhooks", "Incoming webhooks")}</span>
              <span className="workflow-triggers-caption">
                {countLabel(webhooks.length, t("react.workflows.webhook_count_one", "webhook"), t("react.workflows.webhook_count_many", "webhooks"))}
              </span>
            </div>
            <p className="workflow-triggers-help">
              {t("react.workflows.incoming_webhooks_help", "A URL that another service or your own code calls to start this workflow.")}</p>
            {renderWhen(Boolean(workflowTriggersQuery.isLoading), (
              <p className="workflow-triggers-empty">{t("react.workflows.loading_triggers_progress", "Loading triggers…")}</p>
            ))}
            {renderWhen(!workflowTriggersQuery.isLoading && !webhooks.length, (
              <p className="workflow-triggers-empty">{t("react.workflows.no_incoming_webhooks_yet", "No incoming webhooks yet.")}</p>
            ))}
            <div className="workflow-triggers-rows">
              {webhooks.map((webhook) => (
                <button className="workflow-triggers-row" key={webhook.entry_key} onClick={() => openWebhook(webhook)} type="button">
                  <span className="workflow-triggers-row-main">
                    <span className="workflow-triggers-row-title">{webhook.description || webhook.entry_key}</span>
                    <span className="workflow-triggers-row-sub">
                      {webhookPresetLabel(webhookPresetFromSaved(webhook), t)}{" · "}<code>{webhook.public_path}</code>
                    </span>
                  </span>
                  {webhook.enabled ? null : <span className="workflow-triggers-caption">{t("react.workflows.trigger_readiness_off", "Off")}</span>}
                  <ChevronRight aria-hidden="true" />
                </button>
              ))}
            </div>
            <div>
              <Button onClick={() => openWebhook(null)} size="sm" type="button" variant="secondary">
                <Plus aria-hidden="true" />
                {t("react.workflows.new_webhook", "New webhook")}
              </Button>
            </div>
          </section>
          {showProviderTriggers ? (
          <section aria-label={t("react.workflows.provider_triggers_title", "Provider triggers")} className="workflow-triggers-group" id="workflow-provider-trigger-list">
            <div className="workflow-triggers-block-head">
              <span className="workflow-triggers-block-title">{t("react.workflows.provider_triggers_title", "Provider triggers")}</span>
              <span className="workflow-triggers-caption">
                {countLabel(providerRows.length, t("react.workflows.trigger_count_one", "trigger"), t("react.workflows.trigger_count_many", "triggers"))}
              </span>
            </div>
            <p className="workflow-triggers-help">
              {t("react.workflows.provider_triggers_help", "Start this workflow when something happens in a connected app.")}</p>
            {renderWhen(Boolean(providerProjectResolutionPending), (
              <p className="workflow-triggers-help" data-tone="attention">
                {t("react.workflows.resolving_provider_trigger_project", "Resolving project context for provider triggers…")}</p>
            ))}
            {renderWhen(Boolean(!providerProjectResolutionPending && providerProjectUnavailable), (
              <p className="workflow-triggers-help" data-tone="attention">
                {t("react.workflows.link_this_workflow_to_a_project_before_configuring_provider_triggers", "Link this workflow to a project before configuring provider triggers.")}</p>
            ))}
            {renderWhen(Boolean(!providerProjectResolutionPending && !providerProjectUnavailable && !providerRows.length), (
              <p className="workflow-triggers-empty">{t("react.workflows.no_provider_triggers_configured_yet", "No provider triggers configured yet.")}</p>
            ))}
            <div className="workflow-triggers-rows">
              {providerRows.map((trigger) => {
                const catalogId = `${firstStringFromUnknownValue([trigger.provider_key, ""]).trim().toLowerCase()}:${firstStringFromUnknownValue([trigger.trigger_type_id, ""]).trim()}`;
                const entry = catalog.find((item) => item.catalog_id === catalogId);
                const readiness = providerReadiness(trigger, t);
                const app = humanizeKey(firstStringFromUnknownValue([trigger.provider_key, ""]));
                const event = entry?.display_name || trigger.event_name || t("react.workflows.event_not_set", "event not set");
                return (
                  <button className="workflow-triggers-row" disabled={!providerProjectReady} key={trigger.trigger_id} onClick={() => openProvider(trigger)} type="button">
                    <span className="workflow-triggers-row-main">
                      <span className="workflow-triggers-row-title">{trigger.display_name || entry?.display_name || trigger.provider_key}</span>
                      <span className="workflow-triggers-row-sub">{app ? `${app} · ${event}` : event}</span>
                      {readiness.hint ? <span className="workflow-triggers-help" data-tone={readiness.tone === "orange" ? "attention" : undefined}>{readiness.hint}</span> : null}
                    </span>
                    <DsBadge tone={readiness.tone}>{readiness.label}</DsBadge>
                    <ChevronRight aria-hidden="true" />
                  </button>
                );
              })}
            </div>
            <div>
              <Button disabled={!providerProjectReady} onClick={() => openProvider(null)} size="sm" type="button" variant="secondary">
                <Plus aria-hidden="true" />
                {t("react.workflows.new_provider_trigger", "New provider trigger")}
              </Button>
            </div>
          </section>
          ) : null}
        </div>
      </div>
    );
  }

  if (showProviderTriggers && view === "provider") {
    const groups = new Map<string, ProviderTriggerCatalogEntry[]>();
    catalog.forEach((entry) => {
      const app = humanizeKey(firstStringFromUnknownValue([entry.provider_key, entry.catalog_id.split(":")[0], ""]));
      groups.set(app, [...(groups.get(app) || []), entry]);
    });
    const fields = selectedProviderCatalogEntry
      ? triggerUiFields(selectedProviderCatalogEntryView.ui_schema) as ProviderTriggerUiField[]
      : [];
    const required = selectedProviderCatalogEntry ? requiredFieldPaths(selectedProviderCatalogEntryView, fields) : null;
    const mainFields = required ? fields.filter((field) => required.has(triggerUiFieldPath(field))) : fields;
    const optionalFields = required ? fields.filter((field) => !required.has(triggerUiFieldPath(field))) : [];
    const optionalSet = optionalFields.filter((field) => {
      const value = providerTriggerFormDraft[triggerUiFieldPath(field)];
      return value !== undefined && value !== "" && value !== false;
    }).length;
    const providerDisabled = !providerProjectReady || pending;
    const readiness = editingProvider ? providerReadiness(selectedProviderTriggerView, t) : null;
    const renderField = (field: ProviderTriggerUiField) => {
      const fieldPath = triggerUiFieldPath(field);
      const widget = triggerUiFieldWidget(field);
      const currentValue = providerTriggerFormDraft[fieldPath];
      const label = firstStringFromUnknownValue([field.label, fieldPath]);
      const isRequired = Boolean(required?.has(fieldPath));
      const update = (value: unknown) => setProviderTriggerFormDraft((current: Record<string, unknown>) => ({ ...current, [fieldPath]: value }));
      if (widget === "boolean" || widget === "checkbox") {
        return (
          <DsCheckbox checked={Boolean(currentValue)} disabled={providerDisabled} key={fieldPath} onChange={update}>
            {label}
          </DsCheckbox>
        );
      }
      if (widget === "select") {
        const options = Array.isArray(field.options) ? field.options : [];
        return (
          <label className="block text-xs fs-ink-secondary" key={fieldPath}>
            <span>{label}{isRequired ? <span aria-hidden="true" className="fs-required">*</span> : null}</span>
            <select className="fs-input mt-1 w-full" disabled={providerDisabled} onChange={(event) => update(event.target.value)} value={firstStringFromUnknownValue([currentValue, ""])}>
              <option value="">{t("react.workflows.select_value", "Select value")}</option>
              {options.map((option) => (
                <option key={`${fieldPath}:${firstStringFromUnknownValue([option.value, ""])}`} value={firstStringFromUnknownValue([option.value, ""])}>
                  {firstStringFromUnknownValue([option.label, option.value, ""])}
                </option>
              ))}
            </select>
          </label>
        );
      }
      return (
        <label className="block text-xs fs-ink-secondary" key={fieldPath}>
          <span>{label}{isRequired ? <span aria-hidden="true" className="fs-required">*</span> : null}</span>
          <input className="fs-input mt-1 w-full" disabled={providerDisabled} onChange={(event) => update(event.target.value)} placeholder={field.placeholder} value={firstStringFromUnknownValue([currentValue, ""])} />
        </label>
      );
    };
    return (
      <div className="workflow-triggers" id="workflow-triggers-surface">
        {backButton}
        {notice}
        <div className="workflow-triggers-group" id="workflow-provider-trigger-editor">
          <span className="workflow-triggers-micro">
            {editingProvider
              ? t("react.workflows.provider_trigger", "Provider trigger")
              : t("react.workflows.new_provider_trigger", "New provider trigger")}
          </span>
          <p className="workflow-triggers-help">
            {t("react.workflows.provider_triggers_help", "Start this workflow when something happens in a connected app.")}</p>
          {renderWhen(Boolean(providerProjectResolutionPending), (
            <p className="workflow-triggers-help" data-tone="attention">
              {t("react.workflows.resolving_provider_trigger_project", "Resolving project context for provider triggers…")}</p>
          ))}
          {renderWhen(Boolean(!providerProjectResolutionPending && providerProjectUnavailable), (
            <p className="workflow-triggers-help" data-tone="attention">
              {t("react.workflows.link_this_workflow_to_a_project_before_configuring_provider_triggers", "Link this workflow to a project before configuring provider triggers.")}</p>
          ))}
          {catalog.length ? (
            <div aria-label={t("react.workflows.what_starts_this_workflow", "What starts this workflow?")} className="space-y-2" role="radiogroup">
              <span className="block text-xs fs-ink-secondary">{t("react.workflows.what_starts_this_workflow", "What starts this workflow?")}</span>
              {[...groups.entries()].map(([app, entries]) => (
                <div className="space-y-1" key={app}>
                  <span className="workflow-triggers-caption">{app}</span>
                  <div className="workflow-triggers-choices">
                    {entries.map((entry) => (
                      <TriggerChoice
                        checked={providerTriggerCatalogIdDraft === entry.catalog_id}
                        disabled={providerDisabled}
                        help={entry.description}
                        key={entry.catalog_id}
                        name="provider_trigger_event"
                        onChoose={() => {
                          if (entry.catalog_id === providerTriggerCatalogIdDraft) return;
                          // Picking an event resets its settings and, unless a name was typed, names the trigger after it.
                          const previous = catalog.find((item) => item.catalog_id === providerTriggerCatalogIdDraft);
                          const typedName = Boolean(providerTriggerDisplayNameDraft.trim()) && providerTriggerDisplayNameDraft !== previous?.display_name;
                          setProviderTriggerCatalogIdDraft(entry.catalog_id);
                          if (!typedName) setProviderTriggerDisplayNameDraft(entry.display_name || "");
                          setProviderTriggerEnabledDraft(true);
                          setProviderTriggerFormDraft({});
                        }}
                        value={entry.catalog_id}
                      >
                        {entry.display_name || entry.catalog_id}
                      </TriggerChoice>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="workflow-triggers-empty">
              {t("react.workflows.no_connected_app_offers_triggers", "No connected app offers triggers yet. Apps appear here once their extension is enabled for this project.")}</p>
          )}
          {renderWhen(Boolean(selectedProviderCatalogEntry), (
            <>
              {mainFields.map(renderField)}
              <div>
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("react.workflows.trigger_name", "Name")}</span>
                  <input
                    aria-describedby="workflow-provider-trigger-name-help"
                    className="fs-input mt-1 w-full"
                    disabled={providerDisabled}
                    onChange={(event) => setProviderTriggerDisplayNameDraft(event.target.value)}
                    placeholder={selectedProviderCatalogEntryView.display_name || t("react.workflows.sample_tasks_task_created", "Sample Tasks task created")}
                    value={providerTriggerDisplayNameDraft}
                  />
                </label>
                <p className="mt-1 text-[11px] fs-ink-tertiary" id="workflow-provider-trigger-name-help">{t("react.workflows.trigger_name_help", "Shown in the triggers list.")}</p>
              </div>
            </>
          ))}
          <DsCheckbox checked={Boolean(providerTriggerEnabledDraft)} disabled={providerDisabled} onChange={setProviderTriggerEnabledDraft}>
            {t("settings.mcp.status.enabled", "Enabled")}
          </DsCheckbox>
          {renderWhen(optionalFields.length > 0, (
            <SettingsSection
              note={optionalSet ? t("react.workflows.optional_settings_set", "{value0} set", { value0: optionalSet }) : ""}
              title={t("react.workflows.optional_settings", "Optional settings")}
            >
              {optionalFields.map(renderField)}
            </SettingsSection>
          ))}
          {readiness ? (
            <div className="workflow-triggers-status">
              <DsBadge tone={readiness.tone}>{readiness.label}</DsBadge>
              {readiness.hint ? <span className="workflow-triggers-help" data-tone={readiness.tone === "orange" ? "attention" : undefined}>{readiness.hint}</span> : null}
            </div>
          ) : null}
          {renderWhen(Boolean(selectedProviderTriggerView.provider_ingress_url && editingProvider), (
            <p className="workflow-triggers-help">
              {t("react.workflows.provider_ingress", "Provider ingress:")}{" "}<code>{selectedProviderTriggerView.provider_ingress_url}</code>
            </p>
          ))}
        </div>
        <DrawerFooter>
          {editingProvider ? (
            <Button
              disabled={providerDisabled}
              onClick={() => ignoreAsyncResult(Promise.resolve(handleDeleteProviderTrigger(selectedProviderTriggerView.trigger_id)).then(() => setView("list")))}
              type="button"
              variant="danger"
            >
              {t("settings.model_providers.delete", "Delete")}
            </Button>
          ) : null}
          <DrawerFooterSpacer />
          <Button onClick={openList} type="button" variant="secondary">{t("common.cancel", "Cancel")}</Button>
          <Button
            aria-busy={pending || undefined}
            disabled={providerDisabled}
            onClick={() => ignoreAsyncResult((editingProvider ? handleSaveProviderTrigger : handleCreateProviderTrigger)())}
            type="button"
            variant="primary"
          >
            {pending && saveLoader ? <DsLoader variant="orbit-inline" /> : null}
            {providerSaveLabel(pending, editingProvider, t)}
          </Button>
        </DrawerFooter>
      </div>
    );
  }

  const mappedCount = Object.values((webhookInputMappingDraft || {}) as Record<string, unknown>).filter((value) => stringFromUnknownValue(value).trim()).length;
  const mappingNote = mappedCount
    ? countLabel(mappedCount, t("react.workflows.input_mapped_one", "input mapped"), t("react.workflows.input_mapped_many", "inputs mapped"))
    : t("react.workflows.mapped_by_input_name", "By input name");
  const presetImportsSecret = webhookPresetImportsSecret(preset);
  let presetHowTo = "";
  if (preset === "github") {
    presetHowTo = t("react.workflows.webhook_preset_github_howto", "After you create the webhook, copy the secret shown once into GitHub › Settings › Webhooks › Secret, and set Content type to application/json.");
  } else if (preset === "myapp") {
    presetHowTo = t("react.workflows.webhook_preset_myapp_howto", "After you create the webhook, copy the token shown once. Your app sends it as Authorization: Bearer <token>.");
  }
  const presetSecretHelp = preset === "stripe"
    ? t("react.workflows.webhook_preset_stripe_secret_help", "Optional now. Create the webhook, add its URL in Stripe › Developers › Webhooks, then paste the signing secret Stripe shows (it starts with whsec_).")
    : t("react.workflows.webhook_preset_shopify_secret_help", "Optional now. Create the webhook, add its URL in Shopify admin › Settings › Notifications › Webhooks, then paste the key shown under “Your webhooks will be signed with”.");

  return (
    <div className="workflow-triggers" id="workflow-triggers-surface">
      {backButton}
      {notice}
      {editingWebhook && selectedWebhook ? (
        <div className="workflow-triggers-group">
          <span className="workflow-triggers-micro">{t("react.workflows.selected_trigger_heading", "Selected trigger")}</span>
          <div className="workflow-triggers-url">
            <code>{selectedWebhook.public_url}</code>
            <Button
              onClick={() => ignoreAsyncResult(handleCopyWebhookUrl(selectedWebhook.public_url, selectedWebhook.entry_key))}
              size="sm"
              type="button"
              variant="secondary"
            >
              {copiedWebhookKey === selectedWebhook.entry_key
                ? t("react.workflows.display_copied", "Copied")
                : t("react.workflows.display_copy_public_url", "Copy public URL")}
            </Button>
          </div>
          <dl className="fs-kv is-bare">
            <div className="fs-kv-row">
              <dt>{t("react.workflows.trigger_auth", "Auth")}</dt>
              <dd>{selectedWebhook.auth_mode || t("react.workflows.unspecified", "unspecified")}</dd>
            </div>
            <div className="fs-kv-row">
              <dt>{t("react.workflows.trigger_readiness", "Readiness")}</dt>
              <dd className="is-plain">{webhookReadinessLabel(selectedWebhook, t)}</dd>
            </div>
            <div className="fs-kv-row">
              <dt>{t("react.workflows.trigger_content_types", "Content types")}</dt>
              <dd>{(selectedWebhook.allowed_content_types || []).join(", ") || "n/a"}</dd>
            </div>
            <div className="fs-kv-row">
              <dt>{t("react.workflows.response_mode", "Response mode")}</dt>
              <dd>{selectedWebhook.response_mode || t("react.workflows.accepted", "accepted")}</dd>
            </div>
          </dl>
          {webhookConfirm ? (
            <InlineConfirm
              cancelLabel={t("common.cancel", "Cancel")}
              confirmLabel={webhookConfirm === "regenerate"
                ? t("react.workflows.regenerate_url", "Regenerate URL")
                : t("react.workflows.rotate_credential", "Rotate credential")}
              onCancel={() => setWebhookConfirm("")}
              onConfirm={() => {
                const action = webhookConfirm;
                setWebhookConfirm("");
                ignoreAsyncResult((action === "regenerate" ? handleRegenerateWebhookUrl : handleRotateWebhookCredential)(selectedWebhook.entry_key));
              }}
              pending={pending}
              pendingLabel={t("react.workflows.saving_trigger_progress", "Saving trigger…")}
              question={webhookConfirm === "regenerate"
                ? t("react.workflows.regenerate_url_question", "Regenerate the public URL?")
                : t("react.workflows.rotate_credential_question", "Rotate the credential?")}
              tone="danger"
            >
              {webhookConfirm === "regenerate"
                ? t("react.workflows.regenerate_url_consequence", "The current URL stops working at once. Every source that calls it needs the new URL.")
                : t("react.workflows.rotate_credential_consequence", "The current credential stops working at once. Copy the new one into every source that calls this webhook.")}
            </InlineConfirm>
          ) : (
            <div className="workflow-triggers-actions">
              <Button disabled={pending} onClick={() => setWebhookConfirm("rotate")} size="sm" type="button" variant="secondary">
                {t("react.workflows.rotate_credential", "Rotate credential")}</Button>
              <Button disabled={pending} onClick={() => setWebhookConfirm("regenerate")} size="sm" type="button" variant="secondary">
                {t("react.workflows.regenerate_url", "Regenerate URL")}</Button>
              <span aria-hidden="true" className="workflow-triggers-actions-space" />
              <Button
                disabled={pending}
                onClick={() => ignoreAsyncResult(Promise.resolve(handleDeleteWebhook(selectedWebhook.entry_key)).then(() => setView("list")))}
                size="sm"
                type="button"
                variant="danger"
              >
                {t("react.workflows.delete_webhook", "Delete webhook")}</Button>
            </div>
          )}
          {renderWhen(selectedWebhook.public_url_source === "request_origin_fallback", (
            <DsAlert title={t("react.workflows.public_base_url_is_not_configured_this_webhook_url_is_derived_from_the_current_app_origin_and_may_not_be_exter", "Public base URL is not configured. This webhook URL is derived from the current app origin and may not be externally reachable.")} tone="warning" />
          ))}
          {renderWhen(Boolean(lastWebhookCredentialReveal), (
            <div className="space-y-2">
              <div className="workflow-triggers-actions">
                <span className="workflow-triggers-block-title">{t("react.workflows.one_time_credential_reveal", "One-time credential reveal")}</span>
                <span aria-hidden="true" className="workflow-triggers-actions-space" />
                <Button onClick={() => ignoreAsyncResult(handleCopyCredentialReveal())} size="sm" type="button" variant="secondary">
                  {copiedCredentialReveal ? t("react.workflows.display_copied", "Copied") : t("react.workflows.display_copy_credential", "Copy credential")}
                </Button>
              </div>
              <p className="workflow-triggers-help">
                {t("react.workflows.credential_reveal_help", "Copy it into the source service now. It is not shown again.")}</p>
              <pre className="workflow-triggers-pre">{formatJson(lastWebhookCredentialReveal)}</pre>
            </div>
          ))}
          <SettingsSection title={t("react.workflows.generated_curl", "Generated cURL")}>
            <pre className="workflow-triggers-pre">{webhookCurlExample(selectedWebhook, webhookDryRunBodyDraft, t)}</pre>
          </SettingsSection>
          <SettingsSection title={t("react.workflows.dry_run_request", "Dry-run request")}>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.dry_run_payload", "Dry-run payload")}</span>
              <textarea className="fs-textarea mt-1 min-h-20 w-full font-mono" onChange={(event) => setWebhookDryRunBodyDraft(event.target.value)} value={webhookDryRunBodyDraft} />
            </label>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="block text-xs fs-ink-secondary">
                <span>{t("react.workflows.dry_run_headers", "Dry-run headers")}</span>
                <textarea className="fs-textarea mt-1 min-h-20 w-full font-mono" onChange={(event) => setWebhookDryRunHeadersDraft(event.target.value)} value={webhookDryRunHeadersDraft} />
              </label>
              <label className="block text-xs fs-ink-secondary">
                <span>{t("react.workflows.dry_run_query", "Dry-run query")}</span>
                <textarea className="fs-textarea mt-1 min-h-20 w-full font-mono" onChange={(event) => setWebhookDryRunQueryDraft(event.target.value)} value={webhookDryRunQueryDraft} />
              </label>
              <label className="block text-xs fs-ink-secondary">
                <span>{t("react.workflows.dry_run_form_fields", "Dry-run form fields")}</span>
                <textarea className="fs-textarea mt-1 min-h-20 w-full font-mono" onChange={(event) => setWebhookDryRunFormDraft(event.target.value)} value={webhookDryRunFormDraft} />
              </label>
              <label className="block text-xs fs-ink-secondary">
                <span>{t("react.workflows.dry_run_files", "Dry-run files")}</span>
                <textarea
                  className="fs-textarea mt-1 min-h-20 w-full font-mono"
                  onChange={(event) => setWebhookDryRunFilesDraft(event.target.value)}
                  placeholder={'[\n  {"field_name":"files","filename":"sample.json"}\n]'}
                  value={webhookDryRunFilesDraft}
                />
              </label>
            </div>
            <div className="workflow-triggers-actions">
              <Button disabled={pending} onClick={() => ignoreAsyncResult(handleDryRunWebhook(selectedWebhook.entry_key))} size="sm" type="button" variant="secondary">
                {t("react.workflows.dry_run_payload", "Dry-run payload")}</Button>
              <span className="workflow-triggers-help">{t("react.workflows.dry_run_uses_saved_mapping", "Uses the saved mapping. Save changes first.")}</span>
            </div>
          </SettingsSection>
        </div>
      ) : null}
      <div className="workflow-triggers-group" id="workflow-webhook-trigger-editor">
        <span className="workflow-triggers-micro">
          {editingWebhook
            ? t("react.workflows.webhook_settings", "Webhook settings")
            : t("react.workflows.new_incoming_webhook", "New incoming webhook")}
        </span>
        <p className="workflow-triggers-help">
          {editingWebhook
            ? t("react.workflows.display_editing_trigger", "Editing saved trigger settings.")
            : t("react.workflows.display_create_incoming_trigger", "Create a new incoming webhook trigger.")}
        </p>
        <label className="block text-xs fs-ink-secondary">
          <span>{t("react.workflows.trigger_name", "Name")}</span>
          <input
            autoComplete="off"
            className="fs-input mt-1 w-full"
            onChange={(event) => setWebhookDescriptionDraft(event.target.value)}
            placeholder={t("react.workflows.inventory_sync_trigger", "Inventory sync trigger")}
            value={webhookDescriptionDraft}
          />
        </label>
        <div aria-label={t("react.workflows.who_sends_the_requests", "Who sends the requests?")} className="space-y-1" role="radiogroup">
          <span className="block text-xs fs-ink-secondary">{t("react.workflows.who_sends_the_requests", "Who sends the requests?")}</span>
          <div className="workflow-triggers-choices">
            {localizedWebhookPresets(t).map((option) => (
              <TriggerChoice
                checked={preset === option.id}
                help={option.help}
                key={option.id}
                name="webhook_preset"
                onChoose={() => applyPreset(option.id)}
                value={option.id}
              >
                {option.label}
              </TriggerChoice>
            ))}
          </div>
        </div>
        {presetImportsSecret ? (
          <div>
          <label className="block text-xs fs-ink-secondary">
            <span>{t("react.workflows.signing_secret_from_value0", "Signing secret from {value0}", { value0: presetName })}</span>
            <input
              aria-describedby="workflow-webhook-preset-secret-help"
              autoComplete="off"
              className="fs-input mt-1 w-full"
              onChange={(event) => {
                setWebhookSecretDirectionDraft(SIGNED_WEBHOOK_SECRET_IMPORTED);
                setWebhookImportedSigningSecretDraft(event.target.value);
              }}
              placeholder={preset === "stripe" ? "whsec_…" : t("react.workflows.paste_the_signing_secret", "Paste the signing secret")}
              type="password"
              value={webhookImportedSigningSecretDraft}
            />
          </label>
          <p className="mt-1 text-[11px] fs-ink-tertiary" id="workflow-webhook-preset-secret-help">{presetSecretHelp}</p>
          </div>
        ) : null}
        {presetHowTo ? <p className="workflow-triggers-help">{presetHowTo}</p> : null}
        <DsCheckbox checked={Boolean(webhookEnabledDraft)} onChange={setWebhookEnabledDraft}>
          {t("settings.mcp.status.enabled", "Enabled")}
        </DsCheckbox>
        <SettingsSection note={mappingNote} title={t("react.workflows.input_mapping", "Input mapping")}>
          {webhookFieldAutomapSupported ? (
            <p className="workflow-triggers-help">
              {t("react.workflows.webhook_mapping_by_name", "Each input is read from the request body by its name. Blank value falls back to `body.<input_name>`.")}</p>
          ) : (
            <p className="workflow-triggers-help" data-tone="attention">
              {t("react.workflows.text_only_webhooks_need_an_explicit_selector_like_body_body_or_another_source", "Text-only webhooks need an explicit selector like `body.body` or another source.")}</p>
          )}
          <p className="workflow-triggers-help">
            {t("react.workflows.supported_selector_roots_body_headers_query_form_and_files_header_selectors_are_case_insensitive_at_runtime", "Supported selector roots: `body.`, `headers.`, `query.`, `form.`, and `files`. Header selectors are case-insensitive at runtime.")}{webhookFieldAutomapSupported
              ? ""
              : t("react.workflows.display_plain_webhook_selector_help", " With text/plain-only webhooks, required inputs must use an explicit selector.")}
          </p>
          {expectedWebhookInputs.length ? expectedWebhookInputs.map((input: WebhookExpectedInput) => (
            <label className="block text-xs fs-ink-secondary" key={input.name}>
              <code>{input.name}</code>
              {input.required ? " *" : ""}
              <span className="ml-2 text-[11px] fs-ink-tertiary">{input.type || t("react.workflows.string", "string")}</span>
              <input
                autoComplete="off"
                className="fs-input mt-1 w-full font-mono"
                onChange={(event) => setWebhookInputMappingDraft((current: Record<string, string>) => ({ ...current, [input.name]: event.target.value }))}
                placeholder={webhookMappingPlaceholder(input.name)}
                value={firstStringFromUnknownValue([webhookInputMappingDraft[input.name], ""])}
              />
            </label>
          )) : (
            <p className="workflow-triggers-empty">
              {t("react.workflows.no_expected_inputs_found_yet_add_them_in_workflow_settings_to_create_explicit_trigger_mappings", "No Expected Inputs found yet. Add them in workflow settings to create explicit trigger mappings.")}</p>
          )}
        </SettingsSection>
        <SettingsSection
          note={preset === "custom" ? t("react.workflows.webhook_custom_settings", "Custom settings") : t("react.workflows.webhook_safe_defaults", "Safe defaults")}
          onToggle={setAdvancedOpen}
          open={advancedOpen}
          title={t("react.workflows.advanced_settings", "Advanced settings")}
        >
          <label className="block text-xs fs-ink-secondary">
            <span>{t("monitoring.flags.auth_mode", "Auth mode")}</span>
            <select className="fs-input mt-1 w-full" onChange={(event) => setWebhookAuthModeDraft(event.target.value)} value={webhookAuthModeDraft}>
              <option value="bearer_token">{t("settings.mcp.option.bearer", "Bearer token")}</option>
              <option value="webhook_signature">{t("react.workflows.signed_webhook_hmac", "Signed webhook (HMAC)")}</option>
              <option value="hmac_sha256">HMAC SHA256</option>
              <option value="shared_secret_header">{t("react.workflows.shared_secret_header", "Shared secret header")}</option>
              <option value="basic_auth">{t("react.workflows.basic_auth", "Basic auth")}</option>
            </select>
          </label>
          {renderWhen(webhookAuthModeDraft === SIGNED_WEBHOOK_AUTH_MODE, (
            <SignedWebhookAuthFields
              authConfig={webhookAuthConfigDraft}
              draft={webhookSignatureBuilderDraft}
              importedSecret={webhookImportedSigningSecretDraft}
              onDraftChange={setWebhookSignatureBuilderDraft}
              onImportedSecretChange={setWebhookImportedSigningSecretDraft}
              onResetToBuilderConfig={() => setWebhookSignatureBuilderDraft(defaultSignedWebhookBuilderDraft())}
              onSecretDirectionChange={setWebhookSecretDirectionDraft}
              secretDirection={webhookSecretDirectionDraft}
              showSecretDirection={!presetImportsSecret}
              supported={webhookSignatureBuilderSupported}
            />
          ))}
          {renderWhen(webhookAuthModeDraft === "shared_secret_header" || webhookAuthModeDraft === "hmac_sha256", (
            <div>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.header_name", "Header name")}</span>
              <input
                aria-describedby="workflow-webhook-header-help"
                autoComplete="off"
                className="fs-input mt-1 w-full font-mono"
                onChange={(event) => setWebhookAuthConfigDraft((current: Record<string, unknown>) => ({ ...current, header_name: event.target.value }))}
                placeholder={webhookAuthModeDraft === "hmac_sha256" ? "X-Workflow-Signature" : "X-Workflow-Secret"}
                value={firstStringFromUnknownValue([webhookAuthConfigDraft.header_name, ""])}
              />
            </label>
            <p className="mt-1 text-[11px] fs-ink-tertiary" id="workflow-webhook-header-help">
              {webhookAuthModeDraft === "hmac_sha256"
                ? t("react.workflows.hmac_header_blank_help", "Blank uses X-Workflow-Signature, with the sha256= prefix.")
                : t("react.workflows.secret_header_blank_help", "Blank uses X-Workflow-Secret.")}
            </p>
            </div>
          ))}
          {renderWhen(webhookAuthModeDraft === "basic_auth", (
            <div>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.username", "Username")}</span>
              <input
                aria-describedby="workflow-webhook-username-help"
                autoComplete="off"
                className="fs-input mt-1 w-full"
                onChange={(event) => setWebhookAuthConfigDraft((current: Record<string, unknown>) => ({ ...current, username: event.target.value }))}
                placeholder={t("react.workflows.workflow_variant_da7f739f", "workflow")}
                value={firstStringFromUnknownValue([webhookAuthConfigDraft.username, ""])}
              />
            </label>
            <p className="mt-1 text-[11px] fs-ink-tertiary" id="workflow-webhook-username-help">
              {t("react.workflows.basic_auth_password_help", "Flow Steward generates the password and shows it once after saving.")}</p>
            </div>
          ))}
          <div aria-label={t("react.workflows.allowed_content_types", "Allowed content types")} className="space-y-1" role="group">
            <span className="block text-xs fs-ink-secondary">{t("react.workflows.allowed_content_types", "Allowed content types")}</span>
            <div className="flex flex-col">
              {CONTENT_TYPES.map((contentType) => (
                <DsCheckbox
                  checked={webhookAllowedContentTypesDraft.includes(contentType)}
                  key={contentType}
                  onChange={(checked) => setWebhookAllowedContentTypesDraft((current: string[]) =>
                    checked ? Array.from(new Set([...current, contentType])) : current.filter((item: string) => item !== contentType))}
                >
                  {contentType}
                </DsCheckbox>
              ))}
            </div>
          </div>
          <div className="grid gap-2 sm:grid-cols-3">
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.max_body_bytes", "Max body bytes")}</span>
              <input className="fs-input mt-1 w-full" onChange={(event) => setWebhookMaxBodyBytesDraft(event.target.value)} type="number" value={webhookMaxBodyBytesDraft} />
            </label>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.max_file_bytes", "Max file bytes")}</span>
              <input className="fs-input mt-1 w-full" onChange={(event) => setWebhookMaxFileBytesDraft(event.target.value)} type="number" value={webhookMaxFileBytesDraft} />
            </label>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.max_files", "Max files")}</span>
              <input className="fs-input mt-1 w-full" onChange={(event) => setWebhookMaxFilesDraft(event.target.value)} type="number" value={webhookMaxFilesDraft} />
            </label>
          </div>
          <p className="workflow-triggers-help">
            {t("react.workflows.file_limits_multipart_only", "File limits apply only when multipart/form-data is allowed.")}</p>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.idempotency_mode", "Idempotency mode")}</span>
              <select className="fs-input mt-1 w-full" onChange={(event) => setWebhookIdempotencyModeDraft(event.target.value)} value={webhookIdempotencyModeDraft}>
                <option value="none">{t("react.workflows.no_idempotency_key", "No idempotency key")}</option>
                <option value="header">{t("react.workflows.require_idempotency_key", "Require `Idempotency-Key`")}</option>
              </select>
            </label>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.response_mode", "Response mode")}</span>
              <select className="fs-input mt-1 w-full" onChange={(event) => setWebhookResponseModeDraft(event.target.value)} value={webhookResponseModeDraft}>
                <option value="accepted">{t("react.workflows.accepted_202", "Accepted (202)")}</option>
                <option value="ok">{t("react.workflows.immediate_ok_200", "Immediate OK (200)")}</option>
                <option value="no_content">{t("react.workflows.no_content_204", "No content (204)")}</option>
              </select>
            </label>
          </div>
          {renderWhen(webhookIdempotencyModeDraft === "header", (
            <p className="workflow-triggers-help" data-tone="attention">
              {t("react.workflows.idempotency_header_warning", "Requests without an Idempotency-Key header are rejected. GitHub, Stripe and Shopify do not send one.")}</p>
          ))}
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.exact_ip_allowlist", "Exact IP allowlist")}</span>
              <textarea
                className="fs-textarea mt-1 min-h-20 w-full font-mono"
                onChange={(event) => setWebhookSourceExactIpsDraft(event.target.value)}
                placeholder={"203.0.113.10\n198.51.100.24"}
                value={webhookSourceExactIpsDraft}
              />
            </label>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.cidr_allowlist", "CIDR allowlist")}</span>
              <textarea
                className="fs-textarea mt-1 min-h-20 w-full font-mono"
                onChange={(event) => setWebhookSourceCidrsDraft(event.target.value)}
                placeholder={"203.0.113.0/24\n198.51.100.0/24"}
                value={webhookSourceCidrsDraft}
              />
            </label>
          </div>
          <p className="workflow-triggers-help">
            {t("react.workflows.allowlist_help", "One entry per line. Empty accepts requests from anywhere.")}</p>
        </SettingsSection>
        <DsAlert
          title={t("react.workflows.public_reachability_warning_this_url_is_callable_from_outside_your_project_boundary_keep_credentials_private_a", "Public reachability warning: this URL is callable from outside your project boundary. Keep credentials private and restrict allowed content types and payload size.")}
          tone="warning"
        />
      </div>
      <DrawerFooter>
        <Button onClick={openList} type="button" variant="secondary">{t("common.cancel", "Cancel")}</Button>
        <DrawerFooterSpacer />
        <Button
          aria-busy={pending || undefined}
          disabled={pending}
          onClick={() => ignoreAsyncResult((editingWebhook ? handleSaveWebhookTrigger : handleCreateWebhookTrigger)())}
          type="button"
          variant="primary"
        >
          {pending && saveLoader ? <DsLoader variant="orbit-inline" /> : null}
          {webhookSaveLabel(pending, webhookEditorMode === "edit", Boolean(selectedWebhook), t)}
        </Button>
      </DrawerFooter>
    </div>
  );
}
