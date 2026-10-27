import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

import type { WorkflowWebhookTrigger } from "@/pages/workflows/detail/detail-core";
import {
  SIGNED_WEBHOOK_AUTH_MODE,
  SIGNED_WEBHOOK_SECRET_GENERATED,
  SIGNED_WEBHOOK_SECRET_IMPORTED,
  localizedSIGNED_WEBHOOK_EXAMPLES,
  normalizeSignedWebhookBuilderDraft,
  signedWebhookDraftFromAuthConfig,
  type SignedWebhookBuilderDraft,
  type SignedWebhookSecretDirection,
} from "@/pages/workflows/detail/detail-webhook-signature-builder";

/**
 * "Who sends the requests?" — a preset is a starting point for the webhook
 * form, never a provider identity sent to the server. Any setting a preset does
 * not produce turns the webhook into Custom. Presets accept requests without an
 * Idempotency-Key header: GitHub, Stripe and Shopify do not send one, and the
 * server's own default for a new webhook is the same.
 */
export type WebhookPresetId = "myapp" | "github" | "stripe" | "shopify" | "custom";
type SignedPresetId = "github" | "stripe" | "shopify";

export const WEBHOOK_PRESET_IDEMPOTENCY_MODE = "none";
export const WEBHOOK_PRESET_CONTENT_TYPES = ["application/json"];

const SIGNED_PRESET_EXAMPLE: Record<SignedPresetId, string> = {
  github: "github_style",
  stripe: "stripe_style",
  shopify: "shopify_style",
};

/** Stripe and Shopify show their own signing secret; GitHub takes the one we generate. */
const SIGNED_PRESET_IMPORTS_SECRET: Record<SignedPresetId, boolean> = {
  github: false,
  stripe: true,
  shopify: true,
};

const COMPARED_SIGNATURE_FIELDS: Array<keyof SignedWebhookBuilderDraft> = [
  "signatureHeader",
  "valueMode",
  "prefix",
  "signatureKey",
  "allowMultiple",
  "digestFormat",
  "timestampMode",
  "timestampHeader",
  "timestampKey",
  "payloadRecipe",
];

export type WebhookPresetOption = { id: WebhookPresetId; label: string; help: string };

export function localizedWebhookPresets(t: I18nContextValue["t"] = fallbackContext.t): WebhookPresetOption[] {
  return [
    { id: "myapp", label: t("react.workflows.webhook_preset_myapp", "My app or script"), help: t("react.workflows.webhook_preset_myapp_help", "Your code calls the URL with a token we generate.") },
    { id: "github", label: "GitHub", help: t("react.workflows.webhook_preset_github_help", "Signed by GitHub with a secret we generate.") },
    { id: "stripe", label: "Stripe", help: t("react.workflows.webhook_preset_stripe_help", "Signed by Stripe with the secret Stripe gives you.") },
    { id: "shopify", label: "Shopify", help: t("react.workflows.webhook_preset_shopify_help", "Signed by Shopify with the secret Shopify gives you.") },
    { id: "custom", label: t("react.workflows.webhook_preset_custom", "Custom"), help: t("react.workflows.webhook_preset_custom_help", "Another service or your own rules, in Advanced settings.") },
  ];
}

export function webhookPresetLabel(id: WebhookPresetId, t: I18nContextValue["t"] = fallbackContext.t): string {
  return localizedWebhookPresets(t).find((preset) => preset.id === id)?.label || id;
}

export function webhookPresetSignatureDraft(id: WebhookPresetId): SignedWebhookBuilderDraft | null {
  if (id !== "github" && id !== "stripe" && id !== "shopify") return null;
  const example = localizedSIGNED_WEBHOOK_EXAMPLES().find((item) => item.id === SIGNED_PRESET_EXAMPLE[id]);
  return example ? normalizeSignedWebhookBuilderDraft({ ...example.draft }) : null;
}

export function webhookPresetSecretDirection(id: WebhookPresetId): SignedWebhookSecretDirection {
  return (id === "stripe" || id === "shopify") && SIGNED_PRESET_IMPORTS_SECRET[id]
    ? SIGNED_WEBHOOK_SECRET_IMPORTED
    : SIGNED_WEBHOOK_SECRET_GENERATED;
}

/** Whether the preset asks for the signing secret the source service shows. */
export function webhookPresetImportsSecret(id: WebhookPresetId): boolean {
  return id === "stripe" || id === "shopify";
}

function sameSignature(left: SignedWebhookBuilderDraft, right: SignedWebhookBuilderDraft): boolean {
  const a = normalizeSignedWebhookBuilderDraft(left);
  const b = normalizeSignedWebhookBuilderDraft(right);
  return COMPARED_SIGNATURE_FIELDS.every((field) => {
    if (field === "allowMultiple") return Boolean(a[field]) === Boolean(b[field]);
    const leftValue = String(a[field] ?? "").trim();
    const rightValue = String(b[field] ?? "").trim();
    return field === "signatureHeader" || field === "timestampHeader"
      ? leftValue.toLowerCase() === rightValue.toLowerCase()
      : leftValue === rightValue;
  });
}

export type WebhookPresetDraft = {
  authMode: string;
  contentTypes: string[];
  idempotencyMode: string;
  signature: SignedWebhookBuilderDraft | null;
  /** The author chose Custom: the form keeps it even when the settings match a preset. */
  forceCustom?: boolean;
};

export function webhookPresetOf(draft: WebhookPresetDraft): WebhookPresetId {
  const types = draft.contentTypes.map((item) => item.trim().toLowerCase());
  if (
    draft.forceCustom ||
    types.length !== 1 ||
    types[0] !== WEBHOOK_PRESET_CONTENT_TYPES[0] ||
    (draft.idempotencyMode || WEBHOOK_PRESET_IDEMPOTENCY_MODE) !== WEBHOOK_PRESET_IDEMPOTENCY_MODE
  ) {
    return "custom";
  }
  if (draft.authMode === "bearer_token") return "myapp";
  if (draft.authMode !== SIGNED_WEBHOOK_AUTH_MODE || !draft.signature) return "custom";
  const signature = draft.signature;
  return (["github", "stripe", "shopify"] as const).find((id) => {
    const expected = webhookPresetSignatureDraft(id);
    return expected ? sameSignature(signature, expected) : false;
  }) || "custom";
}

/** The preset a saved webhook matches, for its row in the triggers list. */
export function webhookPresetFromSaved(webhook: WorkflowWebhookTrigger): WebhookPresetId {
  const authMode = firstStringFromUnknownValue([webhook.auth_mode, "bearer_token"]);
  const signature = authMode === SIGNED_WEBHOOK_AUTH_MODE
    ? signedWebhookDraftFromAuthConfig(webhook.auth_config)
    : null;
  return webhookPresetOf({
    authMode,
    contentTypes: Array.isArray(webhook.allowed_content_types) && webhook.allowed_content_types.length
      ? webhook.allowed_content_types
      : WEBHOOK_PRESET_CONTENT_TYPES,
    idempotencyMode: firstStringFromUnknownValue([webhook.idempotency_mode, WEBHOOK_PRESET_IDEMPOTENCY_MODE]),
    signature: signature?.supported ? signature.draft : null,
  });
}
