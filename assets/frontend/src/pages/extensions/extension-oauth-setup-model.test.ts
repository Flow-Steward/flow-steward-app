import type { OAuthConfigurationView } from "@/pages/extensions/extension-oauth-api";
import {
  configurationGuidance,
  providerBlockedGuidance,
  providerBlockedReason,
} from "@/pages/extensions/extension-oauth-setup-model";

function configuration(overrides: Partial<OAuthConfigurationView>): OAuthConfigurationView {
  return {
    configuration_id: "cfg-1",
    display_name: "Test app",
    status: "ready_for_authorization",
    fields: [],
    revision: 1,
    authorization_revision: 1,
    ...overrides,
  };
}

describe("provider blocked reason", () => {
  it("is null once a ready configuration exists and the callback side is ready", () => {
    expect(providerBlockedReason([configuration({})], true)).toBeNull();
    expect(providerBlockedReason([configuration({ status: "verified" })], true)).toBeNull();
  });

  it("does not let a ready configuration hide an unregistered callback", () => {
    expect(providerBlockedReason([configuration({})], false)).toEqual({ kind: "callback_registration" });
  });

  it("names the configuration and its known attention code", () => {
    expect(providerBlockedReason(
      [configuration({ status: "needs_attention", attention_code: "invalid_client" })],
      true,
    )).toEqual({ kind: "needs_attention", configurationName: "Test app", attentionCode: "invalid_client" });
  });

  it("prefers needs attention over incomplete, and falls back to no configuration", () => {
    expect(providerBlockedReason(
      [configuration({ status: "incomplete", display_name: "Draft" }), configuration({ status: "needs_attention" })],
      true,
    )).toMatchObject({ kind: "needs_attention", attentionCode: null });
    expect(providerBlockedReason([configuration({ status: "incomplete", display_name: "Draft" })], true))
      .toEqual({ kind: "incomplete", configurationName: "Draft" });
    expect(providerBlockedReason([], true)).toEqual({ kind: "no_configuration" });
    expect(providerBlockedReason(undefined, undefined)).toEqual({ kind: "no_configuration" });
  });
});

describe("guidance text", () => {
  it("tells an invalid_client configuration exactly what to replace", () => {
    const text = configurationGuidance("needs_attention", undefined, "invalid_client");
    expect(text).toContain("invalid_client");
    expect(text).toContain("select Replace and paste the current client secret, then save");
  });

  it("keeps the generic attention text when the reason is unknown", () => {
    expect(configurationGuidance("needs_attention")).toBe(
      "Needs attention — review the fields or replace the stored secret, then save.",
    );
    expect(configurationGuidance("ready_for_authorization", undefined, "invalid_client")).toBe("Ready for authorization.");
  });

  it("explains every blocked reason in words", () => {
    expect(providerBlockedGuidance({ kind: "no_configuration" })).toContain("No OAuth configuration is saved yet");
    expect(providerBlockedGuidance({ kind: "incomplete", configurationName: "Draft" })).toContain('"Draft" is incomplete');
    expect(providerBlockedGuidance({ kind: "callback_registration" })).toContain("redirect URI");
    expect(providerBlockedGuidance({ kind: "needs_attention", configurationName: "Test app", attentionCode: null }))
      .toContain("review the fields or replace the stored secret");
  });
});
