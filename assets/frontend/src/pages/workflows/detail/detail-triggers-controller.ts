import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useMemo, useState } from "react";

import { useQuery } from "@tanstack/react-query";

import {
  compactWebhookInputMapping,
  errorMessageFromPayload,
  formatJson,
  joinMultilineList,
  objectOrEmpty,
  parseJsonArrayDraft,
  parseJsonObjectDraft,
  saveJson,
  splitMultilineList,
  textOrEmpty,
  webhookInputMappingDraftFromValue,
  type ExpectedWebhookInput,
  type WorkflowProviderTrigger,
  type WorkflowTriggerCatalogEntry,
  type WorkflowWebhookTrigger,
} from "@/pages/workflows/detail/detail-helpers";
import {
  SIGNED_WEBHOOK_AUTH_MODE,
  SIGNED_WEBHOOK_SECRET_GENERATED,
  SIGNED_WEBHOOK_SECRET_IMPORTED,
  defaultSignedWebhookBuilderDraft,
  normalizeSignedWebhookBuilderDraft,
  signedWebhookAuthConfigFromDraft,
  signedWebhookDraftFromAuthConfig,
  type SignedWebhookBuilderDraft,
  type SignedWebhookSecretDirection,
} from "@/pages/workflows/detail/detail-webhook-signature-builder";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export function useWorkflowTriggersController(ctx: Record<string, any>) {
  const { t } = useI18n();
  const {
    drawerMode,
    drawerTab,
    linkedProjectQuery,
    searchParams,
    selectedProjectId,
    workflowContractQuery,
    workflowId,
    workflowInterfaceInputsDraft,
    workflowRecord,
  } = ctx;

  const [triggerSavePending, setTriggerSavePending] = useState(false);
  const [triggerNotice, setTriggerNotice] = useState("");
  const [webhookEditorMode, setWebhookEditorMode] = useState<"create" | "edit">(
    "create",
  );
  const [webhookEnabledDraft, setWebhookEnabledDraft] = useState(true);
  const [webhookDescriptionDraft, setWebhookDescriptionDraft] = useState("");
  const [webhookAuthModeDraft, setWebhookAuthModeDraft] = useState("bearer_token");
  const [webhookAllowedContentTypesDraft, setWebhookAllowedContentTypesDraft] =
    useState<string[]>(["application/json"]);
  const [webhookMaxBodyBytesDraft, setWebhookMaxBodyBytesDraft] = useState("1048576");
  const [webhookMaxFileBytesDraft, setWebhookMaxFileBytesDraft] = useState("10485760");
  const [webhookMaxFilesDraft, setWebhookMaxFilesDraft] = useState("4");
  // A new webhook accepts requests without an Idempotency-Key header, as the
  // server's default does: GitHub, Stripe and Shopify never send one.
  const [webhookIdempotencyModeDraft, setWebhookIdempotencyModeDraft] =
    useState("none");
  const [webhookResponseModeDraft, setWebhookResponseModeDraft] =
    useState("accepted");
  const [webhookSourceExactIpsDraft, setWebhookSourceExactIpsDraft] =
    useState("");
  const [webhookSourceCidrsDraft, setWebhookSourceCidrsDraft] = useState("");
  const [webhookAuthConfigDraft, setWebhookAuthConfigDraft] = useState<Record<string, unknown>>(
    {},
  );
  const [webhookSignatureBuilderDraft, setWebhookSignatureBuilderDraft] =
    useState<SignedWebhookBuilderDraft>(defaultSignedWebhookBuilderDraft());
  const [webhookSignatureBuilderSupported, setWebhookSignatureBuilderSupported] =
    useState(true);
  const [webhookSecretDirectionDraft, setWebhookSecretDirectionDraft] =
    useState<SignedWebhookSecretDirection>(SIGNED_WEBHOOK_SECRET_GENERATED);
  const [webhookImportedSigningSecretDraft, setWebhookImportedSigningSecretDraft] =
    useState("");
  const updateWebhookSignatureBuilderDraft = useCallback(
    (
      update:
        | SignedWebhookBuilderDraft
        | ((current: SignedWebhookBuilderDraft) => SignedWebhookBuilderDraft),
    ) => {
      setWebhookSignatureBuilderDraft((current) =>
        normalizeSignedWebhookBuilderDraft(
          typeof update === "function" ? update(current) : update,
        ),
      );
      setWebhookSignatureBuilderSupported(true);
    },
    [],
  );
  const [webhookInputMappingDraft, setWebhookInputMappingDraft] = useState<
    Record<string, string>
  >({});
  const [webhookDryRunBodyDraft, setWebhookDryRunBodyDraft] = useState(
    '{\n  "title": "Sync inventory now"\n}',
  );
  const [webhookDryRunHeadersDraft, setWebhookDryRunHeadersDraft] = useState(
    '{\n  "x-event-type": "inventory.sync"\n}',
  );
  const [webhookDryRunQueryDraft, setWebhookDryRunQueryDraft] = useState("{}");
  const [webhookDryRunFormDraft, setWebhookDryRunFormDraft] = useState("{}");
  const [webhookDryRunFilesDraft, setWebhookDryRunFilesDraft] = useState("[]");
  const [selectedWebhookKey, setSelectedWebhookKey] = useState("");
  const [copiedWebhookKey, setCopiedWebhookKey] = useState("");
  const [lastWebhookCredentialReveal, setLastWebhookCredentialReveal] =
    useState<Record<string, unknown> | null>(null);
  const [lastWebhookCredentialRevealEntryKey, setLastWebhookCredentialRevealEntryKey] =
    useState("");
  const [copiedCredentialReveal, setCopiedCredentialReveal] = useState(false);
  const [providerTriggerEditorMode, setProviderTriggerEditorMode] = useState<
    "create" | "edit"
  >("create");
  const [selectedProviderTriggerId, setSelectedProviderTriggerId] = useState("");
  const [providerTriggerCatalogIdDraft, setProviderTriggerCatalogIdDraft] =
    useState("");
  const [providerTriggerDisplayNameDraft, setProviderTriggerDisplayNameDraft] =
    useState("");
  const [providerTriggerEnabledDraft, setProviderTriggerEnabledDraft] =
    useState(true);
  const [providerTriggerFormDraft, setProviderTriggerFormDraft] = useState<
    Record<string, unknown>
  >({});
  const providerProjectContextSeed =
    textOrEmpty(searchParams.get("project_id")) ||
    textOrEmpty(workflowRecord.project_id) ||
    textOrEmpty(objectOrEmpty(workflowRecord.project).id);
  const providerProjectResolutionPending =
    !providerProjectContextSeed &&
    !selectedProjectId &&
    linkedProjectQuery.isLoading;
  const providerProjectReady = Boolean(selectedProjectId) && !providerProjectResolutionPending;
  const providerProjectUnavailable =
    !providerProjectReady && !providerProjectResolutionPending;
  const webhookProjectResolutionPending = providerProjectResolutionPending;
  const resolvedWebhookProjectId = textOrEmpty(selectedProjectId);
  const workflowTriggersQuery = useQuery({
    queryKey: ["workflows", "triggers", workflowId, selectedProjectId],
    queryFn: () =>
      saveJson(
        `/api/workflows/${encodeURIComponent(workflowId)}/triggers${
          selectedProjectId
            ? `?project_id=${encodeURIComponent(selectedProjectId)}`
            : ""
        }`,
        {
          method: "GET",
        },
      ),
    enabled:
      Boolean(workflowId) &&
      drawerMode === "workflow-settings" &&
      drawerTab === "triggers",
    retry: false,
  });
  const selectedWebhook =
    ((workflowTriggersQuery.data?.incoming_webhooks as WorkflowWebhookTrigger[]) ||
      []
    ).find((item) => item.entry_key === selectedWebhookKey) || null;
  const providerTriggers = useMemo<WorkflowProviderTrigger[]>(
    () =>
      (workflowTriggersQuery.data?.provider_triggers as WorkflowProviderTrigger[]) ||
      [],
    [workflowTriggersQuery.data],
  );
  const triggerCatalog =
    (workflowTriggersQuery.data?.catalog as WorkflowTriggerCatalogEntry[]) || [];
  const providerTriggerCatalog = triggerCatalog.filter(
    (item) => item.trigger_family === "provider",
  );
  const selectedProviderTrigger =
    providerTriggers.find((item) => item.trigger_id === selectedProviderTriggerId) ||
    null;
  const selectedProviderCatalogEntry =
    providerTriggerCatalog.find(
      (item) => item.catalog_id === providerTriggerCatalogIdDraft,
    ) || null;
  const webhookLifecyclePath = useCallback(
    (entryKey = "", suffix = "") => {
      const normalizedWorkflowId = encodeURIComponent(workflowId);
      const normalizedEntryKey = entryKey ? `/${encodeURIComponent(entryKey)}` : "";
      const normalizedSuffix = suffix ? `/${suffix}` : "";
      const search = new URLSearchParams();
      if (resolvedWebhookProjectId) {
        search.set("project_id", resolvedWebhookProjectId);
      }
      const query = search.toString();
      const querySuffix = query ? `?${query}` : "";
      return `/api/workflows/${normalizedWorkflowId}/triggers/webhooks${normalizedEntryKey}${normalizedSuffix}${querySuffix}`;
    },
    [resolvedWebhookProjectId, workflowId],
  );
  const expectedWebhookInputs = useMemo<ExpectedWebhookInput[]>(() => {
    const contractInterface = objectOrEmpty(
      objectOrEmpty(workflowContractQuery.data).interface,
    );
    const savedInputs = Array.isArray(contractInterface.expected_inputs)
      ? contractInterface.expected_inputs
      : [];
    const fallbackInputs = workflowInterfaceInputsDraft;
    const rows: unknown[] = savedInputs.length ? savedInputs : fallbackInputs;
    return rows
      .map((item) => {
        const row = objectOrEmpty(item);
        return {
          name: textOrEmpty(row.name),
          required: Boolean(row.required),
          type: textOrEmpty(row.type) || "string",
        };
      })
      .filter((item) => Boolean(item.name));
  }, [workflowContractQuery.data, workflowInterfaceInputsDraft]);
  const webhookFieldAutomapSupported = useMemo(() => {
    const allowed = Array.isArray(webhookAllowedContentTypesDraft)
      ? webhookAllowedContentTypesDraft
      : [];
    return allowed.some((contentType) =>
      [
        "application/json",
        "multipart/form-data",
        "application/x-www-form-urlencoded",
      ].includes(firstStringFromUnknownValue([contentType, ""]).trim().toLowerCase()),
    );
  }, [webhookAllowedContentTypesDraft]);
  const webhookMappingHint = webhookFieldAutomapSupported
    ? t("react.workflows.blank_value_falls_back_to_body_input_name", "Blank value falls back to `body.<input_name>`")
    : t("react.workflows.text_only_webhooks_need_an_explicit_selector_like_body_body_or_another_source", "Text-only webhooks need an explicit selector like `body.body` or another source.")
  const webhookMappingPlaceholder = (inputName: string) =>
    webhookFieldAutomapSupported ? `body.${inputName}` : "body.body";
  useEffect(() => {
    if (drawerMode !== "workflow-settings" || drawerTab !== "triggers") {
      return;
    }
    const webhookRows =
      (workflowTriggersQuery.data?.incoming_webhooks as WorkflowWebhookTrigger[]) ||
      [];
    if (!webhookRows.length) {
      if (triggerSavePending || workflowTriggersQuery.isFetching) {
        return;
      }
      if (selectedWebhookKey) {
        setSelectedWebhookKey("");
      }
      return;
    }
    if (
      !selectedWebhookKey ||
      !webhookRows.some((item) => item.entry_key === selectedWebhookKey)
    ) {
      setSelectedWebhookKey(firstStringFromUnknownValue([webhookRows[0]?.entry_key, ""]));
      // A list that arrives after New webhook keeps the new form: only a saved selection becomes an edit.
      setWebhookEditorMode((mode) => mode === "create" ? mode : "edit");
    }
  }, [
    drawerMode,
    drawerTab,
    selectedWebhookKey,
    triggerSavePending,
    workflowTriggersQuery.data,
    workflowTriggersQuery.isFetching,
  ]);
  useEffect(() => {
    if (drawerMode !== "workflow-settings" || drawerTab !== "triggers") {
      return;
    }
    if (!providerTriggers.length) {
      if (selectedProviderTriggerId) {
        setSelectedProviderTriggerId("");
      }
      return;
    }
    if (
      !selectedProviderTriggerId ||
      !providerTriggers.some((item) => item.trigger_id === selectedProviderTriggerId)
    ) {
      setSelectedProviderTriggerId(firstStringFromUnknownValue([providerTriggers[0]?.trigger_id, ""]));
      setProviderTriggerEditorMode("edit");
    }
  }, [
    drawerMode,
    drawerTab,
    providerTriggers,
    selectedProviderTriggerId,
  ]);
  const resetWebhookEditorDrafts = useCallback(() => {
    setWebhookEnabledDraft(true);
    setWebhookDescriptionDraft("");
    setWebhookAuthModeDraft("bearer_token");
    setWebhookAuthConfigDraft({});
    setWebhookSignatureBuilderDraft(defaultSignedWebhookBuilderDraft());
    setWebhookSignatureBuilderSupported(true);
    setWebhookSecretDirectionDraft(SIGNED_WEBHOOK_SECRET_GENERATED);
    setWebhookImportedSigningSecretDraft("");
    setWebhookAllowedContentTypesDraft(["application/json"]);
    setWebhookMaxBodyBytesDraft("1048576");
    setWebhookMaxFileBytesDraft("10485760");
    setWebhookMaxFilesDraft("4");
    setWebhookIdempotencyModeDraft("none");
    setWebhookResponseModeDraft("accepted");
    setWebhookSourceExactIpsDraft("");
    setWebhookSourceCidrsDraft("");
    setWebhookInputMappingDraft({});
    setLastWebhookCredentialReveal(null);
    setLastWebhookCredentialRevealEntryKey("");
  }, []);
  const loadWebhookEditorDrafts = useCallback((webhook: WorkflowWebhookTrigger) => {
    setWebhookEnabledDraft(Boolean(webhook.enabled));
    setWebhookDescriptionDraft(firstStringFromUnknownValue([webhook.description, ""]));
    const authMode = firstStringFromUnknownValue([webhook.auth_mode, "bearer_token"]);
    setWebhookAuthModeDraft(authMode);
    setWebhookAuthConfigDraft(objectOrEmpty(webhook.auth_config));
    if (authMode === SIGNED_WEBHOOK_AUTH_MODE) {
      const { draft, supported } = signedWebhookDraftFromAuthConfig(webhook.auth_config);
      setWebhookSignatureBuilderDraft(draft);
      setWebhookSignatureBuilderSupported(supported);
    } else {
      setWebhookSignatureBuilderDraft(defaultSignedWebhookBuilderDraft());
      setWebhookSignatureBuilderSupported(true);
    }
    setWebhookSecretDirectionDraft(SIGNED_WEBHOOK_SECRET_GENERATED);
    setWebhookImportedSigningSecretDraft("");
    setWebhookAllowedContentTypesDraft(
      Array.isArray(webhook.allowed_content_types) &&
        webhook.allowed_content_types.length
        ? webhook.allowed_content_types
        : ["application/json"],
    );
    setWebhookMaxBodyBytesDraft(firstStringFromUnknownValue([webhook.max_body_bytes, 1048576]));
    setWebhookMaxFileBytesDraft(firstStringFromUnknownValue([webhook.max_file_bytes, 10485760]));
    setWebhookMaxFilesDraft(firstStringFromUnknownValue([webhook.max_files, 4]));
    setWebhookIdempotencyModeDraft(
      firstStringFromUnknownValue([webhook.idempotency_mode, "none"]) || "none",
    );
    setWebhookResponseModeDraft(
      firstStringFromUnknownValue([webhook.response_mode, "accepted"]) || "accepted",
    );
    const restrictions = objectOrEmpty(webhook.source_restrictions);
    setWebhookSourceExactIpsDraft(joinMultilineList(restrictions.exact_ips));
    setWebhookSourceCidrsDraft(joinMultilineList(restrictions.cidrs));
    setWebhookInputMappingDraft(
      webhookInputMappingDraftFromValue(webhook.input_mapping),
    );
  }, []);
  const resetProviderTriggerDrafts = useCallback(() => {
    setProviderTriggerCatalogIdDraft("");
    setProviderTriggerDisplayNameDraft("");
    setProviderTriggerEnabledDraft(true);
    setProviderTriggerFormDraft({});
  }, []);
  const loadProviderTriggerDrafts = useCallback(
    (trigger: WorkflowProviderTrigger) => {
      setProviderTriggerCatalogIdDraft(
        `${firstStringFromUnknownValue([trigger.provider_key, ""]).trim().toLowerCase()}:${firstStringFromUnknownValue([trigger.trigger_type_id, ""]).trim()}`,
      );
      setProviderTriggerDisplayNameDraft(firstStringFromUnknownValue([trigger.display_name, ""]));
      setProviderTriggerEnabledDraft(Boolean(trigger.enabled));
      setProviderTriggerFormDraft(objectOrEmpty(trigger.trigger_config));
    },
    [],
  );
  useEffect(() => {
    if (drawerMode !== "workflow-settings" || drawerTab !== "triggers") {
      return;
    }
    if (webhookEditorMode === "create") {
      return;
    }
    if (selectedWebhook) {
      loadWebhookEditorDrafts(selectedWebhook);
    }
  }, [
    drawerMode,
    drawerTab,
    loadWebhookEditorDrafts,
    selectedWebhook,
    webhookEditorMode,
  ]);
  useEffect(() => {
    if (
      !lastWebhookCredentialReveal ||
      !lastWebhookCredentialRevealEntryKey ||
      !selectedWebhookKey ||
      selectedWebhookKey === lastWebhookCredentialRevealEntryKey
    ) {
      return;
    }
    setLastWebhookCredentialReveal(null);
    setLastWebhookCredentialRevealEntryKey("");
    setCopiedCredentialReveal(false);
  }, [
    lastWebhookCredentialReveal,
    lastWebhookCredentialRevealEntryKey,
    selectedWebhookKey,
  ]);
  useEffect(() => {
    if (drawerMode !== "workflow-settings" || drawerTab !== "triggers") {
      return;
    }
    if (providerTriggerEditorMode === "create") {
      return;
    }
    if (selectedProviderTrigger) {
      loadProviderTriggerDrafts(selectedProviderTrigger);
    }
  }, [
    drawerMode,
    drawerTab,
    loadProviderTriggerDrafts,
    providerTriggerEditorMode,
    selectedProviderTrigger,
  ]);
  function resolvedWebhookAuthConfig(): Record<string, unknown> {
    if (webhookAuthModeDraft !== SIGNED_WEBHOOK_AUTH_MODE) {
      return webhookAuthConfigDraft;
    }
    // An unrepresentable saved config stays byte-identical until the operator
    // explicitly resets it back to a builder-supported shape.
    return webhookSignatureBuilderSupported
      ? signedWebhookAuthConfigFromDraft(webhookSignatureBuilderDraft)
      : webhookAuthConfigDraft;
  }

  function importedSigningSecretPayload(): Record<string, string> {
    // The secret input is only reachable from the signed-webhook editor, but the
    // draft survives an auth-mode switch, so gate on the mode being submitted —
    // otherwise a stale provider secret rides along on a bearer/basic webhook.
    if (
      webhookAuthModeDraft !== SIGNED_WEBHOOK_AUTH_MODE ||
      webhookSecretDirectionDraft !== SIGNED_WEBHOOK_SECRET_IMPORTED
    ) {
      return {};
    }
    const secret = webhookImportedSigningSecretDraft.trim();
    return secret ? { signing_secret: secret } : {};
  }

  async function handleCreateWebhookTrigger() {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!normalizedWorkflowId) {
      return;
    }
    if (webhookProjectResolutionPending) {
      setTriggerNotice(t("react.workflows.wait_for_project_context_to_resolve_before_creating_a_webhook", "Wait for project context to resolve before creating a webhook."));
      return;
    }
    setTriggerNotice("");
    setTriggerSavePending(true);
    try {
      const payloadBody = {
        project_id: resolvedWebhookProjectId || null,
        description: webhookDescriptionDraft.trim(),
        enabled: webhookEnabledDraft,
        auth_mode: webhookAuthModeDraft,
        auth_config: resolvedWebhookAuthConfig(),
        ...importedSigningSecretPayload(),
        source_restrictions: {
          exact_ips: splitMultilineList(webhookSourceExactIpsDraft),
          cidrs: splitMultilineList(webhookSourceCidrsDraft),
        },
        allowed_content_types: webhookAllowedContentTypesDraft,
        max_body_bytes: Number.parseInt(webhookMaxBodyBytesDraft, 10) || 1048576,
        max_file_bytes: Number.parseInt(webhookMaxFileBytesDraft, 10) || 10485760,
        max_files: Number.parseInt(webhookMaxFilesDraft, 10) || 4,
        idempotency_mode: webhookIdempotencyModeDraft || "none",
        response_mode: webhookResponseModeDraft || "accepted",
        input_mapping: compactWebhookInputMapping(webhookInputMappingDraft),
      };
      const payload = await saveJson(webhookLifecyclePath(), {
        method: "POST",
        body: JSON.stringify(payloadBody),
      });
      await workflowTriggersQuery.refetch();
      const nextEntryKey = firstStringFromUnknownValue([payload.entry_key, ""]).trim();
      setSelectedWebhookKey(nextEntryKey);
      setWebhookEditorMode("edit");
      loadWebhookEditorDrafts(payload as WorkflowWebhookTrigger);
      const createdReveal = objectOrEmpty(payload.credential_reveal);
      // An imported provider secret comes back as `credential_configured` with
      // no reveal, so there is nothing to show once.
      setLastWebhookCredentialReveal(
        Object.keys(createdReveal).length ? createdReveal : null,
      );
      setLastWebhookCredentialRevealEntryKey(
        Object.keys(createdReveal).length ? nextEntryKey : "",
      );
      setCopiedCredentialReveal(false);
      let createdNotice = t("react.workflows.display_webhook_created", "Webhook created.");
      if (Object.keys(objectOrEmpty(payload.credential_reveal)).length) {
        createdNotice = t("react.workflows.display_webhook_created_credential", "Webhook created. Credential shown once below.");
      } else if (payload.credential_configured) {
        createdNotice = t("react.workflows.display_webhook_created_signing_secret", "Webhook created. Your signing secret is stored.");
      }
      setTriggerNotice(createdNotice);
    } catch (error) {
      setTriggerNotice(
        error instanceof Error ? error.message : t("react.workflows.failed_to_create_webhook", "Failed to create webhook."),
      );
    } finally {
      setTriggerSavePending(false);
    }
  }

  async function handleSaveWebhookTrigger() {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!normalizedWorkflowId || !selectedWebhook) {
      return;
    }
    if (webhookProjectResolutionPending) {
      setTriggerNotice(t("react.workflows.wait_for_project_context_to_resolve_before_saving_webhook_settings", "Wait for project context to resolve before saving webhook settings."));
      return;
    }
    setTriggerNotice("");
    setTriggerSavePending(true);
    try {
      const payload = await saveJson(
        webhookLifecyclePath(selectedWebhook.entry_key),
        {
          method: "PATCH",
          body: JSON.stringify({
            project_id: resolvedWebhookProjectId || null,
            description: webhookDescriptionDraft.trim(),
            enabled: webhookEnabledDraft,
            auth_mode: webhookAuthModeDraft,
            auth_config: resolvedWebhookAuthConfig(),
            ...importedSigningSecretPayload(),
            source_restrictions: {
              exact_ips: splitMultilineList(webhookSourceExactIpsDraft),
              cidrs: splitMultilineList(webhookSourceCidrsDraft),
            },
            allowed_content_types: webhookAllowedContentTypesDraft,
            max_body_bytes:
              Number.parseInt(webhookMaxBodyBytesDraft, 10) || 1048576,
            max_file_bytes:
              Number.parseInt(webhookMaxFileBytesDraft, 10) || 10485760,
            max_files: Number.parseInt(webhookMaxFilesDraft, 10) || 4,
            idempotency_mode: webhookIdempotencyModeDraft || "none",
            response_mode: webhookResponseModeDraft || "accepted",
            input_mapping: compactWebhookInputMapping(webhookInputMappingDraft),
          }),
        },
      );
      await workflowTriggersQuery.refetch();
      setSelectedWebhookKey(firstStringFromUnknownValue([payload.entry_key, selectedWebhook.entry_key]));
      loadWebhookEditorDrafts(payload as WorkflowWebhookTrigger);
      setLastWebhookCredentialReveal(
        Object.keys(objectOrEmpty(payload.credential_reveal)).length
          ? objectOrEmpty(payload.credential_reveal)
          : null,
      );
      setLastWebhookCredentialRevealEntryKey(
        Object.keys(objectOrEmpty(payload.credential_reveal)).length
          ? firstStringFromUnknownValue([payload.entry_key, selectedWebhook.entry_key, ""]).trim()
          : "",
      );
      setCopiedCredentialReveal(false);
      let savedNotice = t("react.workflows.display_webhook_settings_saved", "Webhook settings saved.");
      if (Object.keys(objectOrEmpty(payload.credential_reveal)).length) {
        savedNotice = t("react.workflows.display_webhook_saved_credential", "Webhook settings saved. New credential shown once below.");
      } else if (payload.credential_configured) {
        savedNotice = t("react.workflows.display_webhook_saved_signing_secret", "Webhook settings saved. Your signing secret was replaced.");
      }
      setTriggerNotice(savedNotice);
    } catch (error) {
      setTriggerNotice(
        error instanceof Error
          ? error.message
          : t("react.workflows.failed_to_save_webhook_settings", "Failed to save webhook settings."),
      );
    } finally {
      setTriggerSavePending(false);
    }
  }

  async function handleCopyWebhookUrl(url: string, entryKey: string) {
    try {
      await navigator.clipboard.writeText(url);
      setCopiedWebhookKey(entryKey);
      setTriggerNotice(t("react.workflows.public_webhook_url_copied", "Public webhook URL copied."));
    } catch {
      setTriggerNotice(t("react.workflows.failed_to_copy_webhook_url", "Failed to copy webhook URL."));
    }
  }

  async function handleCopyCredentialReveal() {
    if (!lastWebhookCredentialReveal) {
      return;
    }
    try {
      await navigator.clipboard.writeText(formatJson(lastWebhookCredentialReveal));
      setCopiedCredentialReveal(true);
      setTriggerNotice(t("react.workflows.credential_copied", "Credential copied."));
    } catch {
      setTriggerNotice(t("react.workflows.failed_to_copy_credential_reveal", "Failed to copy credential reveal."));
    }
  }

  async function handleDryRunWebhook(entryKey: string) {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!normalizedWorkflowId || !entryKey) {
      return;
    }
    if (webhookProjectResolutionPending) {
      setTriggerNotice(t("react.workflows.wait_for_project_context_to_resolve_before_dry_running_this_webhook", "Wait for project context to resolve before dry-running this webhook."));
      return;
    }
    try {
      const parsedBody = parseJsonObjectDraft(webhookDryRunBodyDraft, {
        label: t("react.workflows.dry_run_body", "Dry-run body"),
      }, t);
      const parsedHeaders = parseJsonObjectDraft(webhookDryRunHeadersDraft, {
        label: t("react.workflows.dry_run_headers", "Dry-run headers"),
      }, t);
      const parsedQuery = parseJsonObjectDraft(webhookDryRunQueryDraft, {
        label: t("react.workflows.dry_run_query", "Dry-run query"),
      }, t);
      const parsedForm = parseJsonObjectDraft(webhookDryRunFormDraft, {
        label: t("react.workflows.dry_run_form_fields", "Dry-run form fields"),
      }, t);
      const parsedFiles = parseJsonArrayDraft(webhookDryRunFilesDraft, {
        label: t("react.workflows.dry_run_files", "Dry-run files"),
      }, t);
      setTriggerNotice("");
      setTriggerSavePending(true);
      try {
        const payload = await saveJson(
          webhookLifecyclePath(entryKey, "dry-run"),
          {
            method: "POST",
            body: JSON.stringify({
              project_id: resolvedWebhookProjectId || null,
              body: parsedBody,
              headers: parsedHeaders,
              query: parsedQuery,
              form: parsedForm,
              files: parsedFiles,
            }),
          },
        );
        const validation = objectOrEmpty(payload.validation);
        setTriggerNotice(
          validation.ok
            ? t("react.workflows.dry_run_validation_passed", "Dry-run validation passed.")
            : errorMessageFromPayload(validation, "Dry-run validation failed."),
        );
      } catch (error) {
        setTriggerNotice(
          error instanceof Error ? error.message : t("react.workflows.failed_to_dry_run_webhook", "Failed to dry-run webhook."),
        );
      } finally {
        setTriggerSavePending(false);
      }
    } catch (error) {
      setTriggerNotice(
        error instanceof Error ? error.message : t("react.workflows.dry_run_payload_is_invalid", "Dry-run payload is invalid."),
      );
      return;
    }
  }

  async function handleDeleteWebhook(entryKey: string) {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!normalizedWorkflowId || !entryKey) {
      return;
    }
    if (webhookProjectResolutionPending) {
      setTriggerNotice(t("react.workflows.wait_for_project_context_to_resolve_before_deleting_this_webhook", "Wait for project context to resolve before deleting this webhook."));
      return;
    }
    setTriggerNotice("");
    setTriggerSavePending(true);
    try {
      await saveJson(webhookLifecyclePath(entryKey), { method: "DELETE" });
      await workflowTriggersQuery.refetch();
      resetWebhookEditorDrafts();
      setWebhookEditorMode("create");
      setSelectedWebhookKey("");
      setTriggerNotice(t("react.workflows.webhook_deleted", "Webhook deleted."));
    } catch (error) {
      setTriggerNotice(
        error instanceof Error ? error.message : t("react.workflows.failed_to_delete_webhook", "Failed to delete webhook."),
      );
    } finally {
      setTriggerSavePending(false);
    }
  }

  async function handleCreateProviderTrigger() {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (providerProjectResolutionPending) {
      setTriggerNotice(t("react.workflows.wait_for_project_context_to_resolve_before_creating_a_provider_trigger", "Wait for project context to resolve before creating a provider trigger."));
      return;
    }
    if (!selectedProjectId) {
      setTriggerNotice(t("react.workflows.link_this_workflow_to_a_project_before_creating_provider_triggers", "Link this workflow to a project before creating provider triggers."));
      return;
    }
    if (!normalizedWorkflowId || !selectedProviderCatalogEntry) {
      setTriggerNotice(t("react.workflows.pick_a_provider_trigger_from_the_catalog", "Pick a provider trigger from the catalog."));
      return;
    }
    setTriggerNotice("");
    setTriggerSavePending(true);
    try {
      const payload = await saveJson(
        `/api/workflows/${encodeURIComponent(normalizedWorkflowId)}/triggers/providers`,
        {
          method: "POST",
          body: JSON.stringify({
            project_id: selectedProjectId || null,
            provider_key: selectedProviderCatalogEntry.provider_key,
            trigger_type_id: selectedProviderCatalogEntry.trigger_type_id,
            display_name:
              providerTriggerDisplayNameDraft.trim() ||
              selectedProviderCatalogEntry.display_name,
            enabled: providerTriggerEnabledDraft,
            trigger_config: providerTriggerFormDraft,
          }),
        },
      );
      await workflowTriggersQuery.refetch();
      setSelectedProviderTriggerId(firstStringFromUnknownValue([payload.trigger_id, ""]));
      setProviderTriggerEditorMode("edit");
      setTriggerNotice(t("react.workflows.provider_trigger_saved", "Provider trigger saved."));
    } catch (error) {
      setTriggerNotice(
        error instanceof Error ? error.message : t("react.workflows.failed_to_save_provider_trigger", "Failed to save provider trigger."),
      );
    } finally {
      setTriggerSavePending(false);
    }
  }

  async function handleSaveProviderTrigger() {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (providerProjectResolutionPending) {
      setTriggerNotice(t("react.workflows.wait_for_project_context_to_resolve_before_updating_provider_triggers", "Wait for project context to resolve before updating provider triggers."));
      return;
    }
    if (!selectedProjectId && !selectedProviderTrigger?.project_id) {
      setTriggerNotice(t("react.workflows.link_this_workflow_to_a_project_before_updating_provider_triggers", "Link this workflow to a project before updating provider triggers."));
      return;
    }
    if (!normalizedWorkflowId || !selectedProviderTrigger || !selectedProviderCatalogEntry) {
      setTriggerNotice(t("react.workflows.pick_a_configured_provider_trigger", "Pick a configured provider trigger."));
      return;
    }
    setTriggerNotice("");
    setTriggerSavePending(true);
    try {
      await saveJson(
        `/api/workflows/${encodeURIComponent(normalizedWorkflowId)}/triggers/providers/${encodeURIComponent(selectedProviderTrigger.trigger_id)}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            project_id:
              selectedProviderTrigger?.project_id || selectedProjectId || null,
            provider_key: selectedProviderCatalogEntry.provider_key,
            trigger_type_id: selectedProviderCatalogEntry.trigger_type_id,
            display_name:
              providerTriggerDisplayNameDraft.trim() ||
              selectedProviderCatalogEntry.display_name,
            enabled: providerTriggerEnabledDraft,
            trigger_config: providerTriggerFormDraft,
          }),
        },
      );
      await workflowTriggersQuery.refetch();
      setTriggerNotice(t("react.workflows.provider_trigger_updated", "Provider trigger updated."));
    } catch (error) {
      setTriggerNotice(
        error instanceof Error
          ? error.message
          : t("react.workflows.failed_to_update_provider_trigger", "Failed to update provider trigger."),
      );
    } finally {
      setTriggerSavePending(false);
    }
  }

  async function handleDeleteProviderTrigger(triggerId: string) {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (providerProjectResolutionPending) {
      setTriggerNotice(t("react.workflows.wait_for_project_context_to_resolve_before_deleting_provider_triggers", "Wait for project context to resolve before deleting provider triggers."));
      return;
    }
    if (!normalizedWorkflowId || !triggerId) {
      return;
    }
    setTriggerNotice("");
    setTriggerSavePending(true);
    try {
      await saveJson(
        `/api/workflows/${encodeURIComponent(normalizedWorkflowId)}/triggers/providers/${encodeURIComponent(triggerId)}`,
        { method: "DELETE" },
      );
      await workflowTriggersQuery.refetch();
      resetProviderTriggerDrafts();
      setProviderTriggerEditorMode("create");
      setSelectedProviderTriggerId("");
      setTriggerNotice(t("react.workflows.provider_trigger_deleted", "Provider trigger deleted."));
    } catch (error) {
      setTriggerNotice(
        error instanceof Error
          ? error.message
          : t("react.workflows.failed_to_delete_provider_trigger", "Failed to delete provider trigger."),
      );
    } finally {
      setTriggerSavePending(false);
    }
  }

  async function handleRotateWebhookCredential(entryKey: string) {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!normalizedWorkflowId || !entryKey) {
      return;
    }
    if (webhookProjectResolutionPending) {
      setTriggerNotice(t("react.workflows.wait_for_project_context_to_resolve_before_rotating_this_credential", "Wait for project context to resolve before rotating this credential."));
      return;
    }
    setTriggerSavePending(true);
    setTriggerNotice("");
    try {
      const payload = await saveJson(
        webhookLifecyclePath(entryKey, "rotate-secret"),
        { method: "POST" },
      );
      setLastWebhookCredentialReveal(objectOrEmpty(payload.credential_reveal));
      setLastWebhookCredentialRevealEntryKey(firstStringFromUnknownValue([entryKey, ""]).trim());
      setCopiedCredentialReveal(false);
      await workflowTriggersQuery.refetch();
      setTriggerNotice(t("react.workflows.credential_rotated_reveal_shown_once_below", "Credential rotated. Reveal shown once below."));
    } catch (error) {
      setTriggerNotice(
        error instanceof Error ? error.message : t("react.workflows.failed_to_rotate_credential", "Failed to rotate credential."),
      );
    } finally {
      setTriggerSavePending(false);
    }
  }

  async function handleRegenerateWebhookUrl(entryKey: string) {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!normalizedWorkflowId || !entryKey) {
      return;
    }
    if (webhookProjectResolutionPending) {
      setTriggerNotice(t("react.workflows.wait_for_project_context_to_resolve_before_regenerating_this_url", "Wait for project context to resolve before regenerating this URL."));
      return;
    }
    setTriggerSavePending(true);
    setTriggerNotice("");
    try {
      const payload = await saveJson(
        webhookLifecyclePath(entryKey, "regenerate-entry-key"),
        { method: "POST" },
      );
      setSelectedWebhookKey(firstStringFromUnknownValue([payload.entry_key, ""]));
      setLastWebhookCredentialReveal(null);
      setLastWebhookCredentialRevealEntryKey("");
      await workflowTriggersQuery.refetch();
      setTriggerNotice(t("react.workflows.public_webhook_url_regenerated", "Public webhook URL regenerated."));
    } catch (error) {
      setTriggerNotice(
        error instanceof Error ? error.message : t("react.workflows.failed_to_regenerate_webhook_url", "Failed to regenerate webhook URL."),
      );
    } finally {
      setTriggerSavePending(false);
    }
  }


  return {
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
    loadProviderTriggerDrafts,
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
    selectedProviderTriggerId,
    selectedWebhook,
    selectedWebhookKey,
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
    setWebhookSignatureBuilderDraft: updateWebhookSignatureBuilderDraft,
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
    webhookMappingHint,
    webhookMappingPlaceholder,
    webhookMaxBodyBytesDraft,
    webhookMaxFileBytesDraft,
    webhookMaxFilesDraft,
    webhookResponseModeDraft,
    webhookSourceCidrsDraft,
    webhookSourceExactIpsDraft,
    workflowTriggersQuery,
  };
}
