import { describe, expect, it } from "vitest";

import type { WorkflowWebhookTrigger } from "@/pages/workflows/detail/detail-core";
import {
  WEBHOOK_PRESET_IDEMPOTENCY_MODE,
  webhookPresetFromSaved,
  webhookPresetImportsSecret,
  webhookPresetOf,
  webhookPresetSecretDirection,
  webhookPresetSignatureDraft,
} from "@/pages/workflows/detail/detail-webhook-presets";
import {
  SIGNED_WEBHOOK_SECRET_GENERATED,
  SIGNED_WEBHOOK_SECRET_IMPORTED,
  defaultSignedWebhookBuilderDraft,
  signedWebhookAuthConfigFromDraft,
} from "@/pages/workflows/detail/detail-webhook-signature-builder";

const json = ["application/json"];

function savedWebhook(overrides: Partial<WorkflowWebhookTrigger> = {}): WorkflowWebhookTrigger {
  return {
    workflow_id: "orders",
    entry_key: "k1",
    enabled: true,
    allowed_methods: ["POST"],
    auth_mode: "bearer_token",
    auth_config: {},
    allowed_content_types: json,
    idempotency_mode: "none",
    response_mode: "accepted",
    input_mapping: {},
    requires_secret: true,
    public_path: "/webhook/workflows/k1",
    public_url: "https://example.test/webhook/workflows/k1",
    ...overrides,
  };
}

describe("webhook presets", () => {
  it("accept requests without an idempotency key", () => {
    expect(WEBHOOK_PRESET_IDEMPOTENCY_MODE).toBe("none");
    expect(webhookPresetOf({ authMode: "bearer_token", contentTypes: json, idempotencyMode: "none", signature: null })).toBe("myapp");
  });

  it("turn Custom when the webhook requires an Idempotency-Key", () => {
    expect(webhookPresetOf({ authMode: "bearer_token", contentTypes: json, idempotencyMode: "header", signature: null })).toBe("custom");
  });

  it("turn Custom for any content type besides JSON, or when Custom was chosen", () => {
    expect(webhookPresetOf({ authMode: "bearer_token", contentTypes: [...json, "text/plain"], idempotencyMode: "none", signature: null })).toBe("custom");
    expect(webhookPresetOf({ authMode: "bearer_token", contentTypes: json, idempotencyMode: "none", signature: null, forceCustom: true })).toBe("custom");
  });

  it("recognise each provider's signature shape", () => {
    for (const id of ["github", "stripe", "shopify"] as const) {
      expect(webhookPresetOf({ authMode: "webhook_signature", contentTypes: json, idempotencyMode: "none", signature: webhookPresetSignatureDraft(id) })).toBe(id);
    }
    expect(webhookPresetOf({ authMode: "webhook_signature", contentTypes: json, idempotencyMode: "none", signature: defaultSignedWebhookBuilderDraft() })).toBe("custom");
  });

  it("generate the GitHub secret and import the Stripe and Shopify ones", () => {
    expect(webhookPresetSecretDirection("github")).toBe(SIGNED_WEBHOOK_SECRET_GENERATED);
    expect(webhookPresetSecretDirection("stripe")).toBe(SIGNED_WEBHOOK_SECRET_IMPORTED);
    expect(webhookPresetImportsSecret("shopify")).toBe(true);
    expect(webhookPresetImportsSecret("myapp")).toBe(false);
  });

  it("name a saved webhook's preset from its stored settings", () => {
    expect(webhookPresetFromSaved(savedWebhook())).toBe("myapp");
    expect(webhookPresetFromSaved(savedWebhook({ idempotency_mode: "header" }))).toBe("custom");
    const github = webhookPresetSignatureDraft("github");
    expect(github).not.toBeNull();
    expect(webhookPresetFromSaved(savedWebhook({
      auth_mode: "webhook_signature",
      auth_config: signedWebhookAuthConfigFromDraft(github ?? defaultSignedWebhookBuilderDraft()),
    }))).toBe("github");
  });
});
