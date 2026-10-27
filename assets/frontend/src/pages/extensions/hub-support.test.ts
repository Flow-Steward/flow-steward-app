import { extensionDocumentationUrl, oauthCallbackNoticeFromSearch, oauthCallbackMessageFor } from "@/pages/extensions/hub-support";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import * as support from "@/pages/extensions/hub-support";
import type { ExtensionCatalogListing } from "@/pages/extensions/catalog-api";

const ollama = {
  extension_id: "acme.model-provider",
  website_url: "https://github.com/Flow-Steward/ext-llm-ollama",
  support_url: "https://github.com/Flow-Steward/ext-llm-ollama/issues",
};

test("OAuth callback guidance uses the active locale without changing callback identifiers", () => {
  const messages: Record<string, string> = {
    "react.extensions.oauth_final_cancelled": "Autorizacija atšaukta. Galite bandyti prijungti paskyrą dar kartą.",
    "react.extensions.oauth_final_external_account": "Išorinė paskyra",
    "react.extensions.value0_connected_successfully": "{value0} sėkmingai prijungta.",
  };
  const t: I18nContextValue["t"] = (key, defaultText, params) =>
    fallbackContext.t(key, messages[key] || defaultText, params);
  const notice = oauthCallbackNoticeFromSearch("?oauth=error&reason=authorization_cancelled&oauth_connection_id=oauth:42&project_id=project_1", "#oauth-help", "example.extension", t);
  expect(notice).toMatchObject({
    kind: "error",
    message: "Autorizacija atšaukta. Galite bandyti prijungti paskyrą dar kartą.",
    connectionId: "oauth:42",
    projectId: "project_1",
    helpAnchor: "oauth-help",
    extensionId: "example.extension",
  });
  const success = oauthCallbackNoticeFromSearch("?oauth=success", "", "example.extension", t);
  expect(oauthCallbackMessageFor(success, {}, t)).toBe("Išorinė paskyra sėkmingai prijungta.");
});

test("extension status display is localized after canonical classification and preserves provider details", () => {
  const messages: Record<string, string> = {
    "react.extensions.oauth_final_health_checking": "Tikrinama būsena…",
    "react.extensions.oauth_final_health_unhealthy_reason": "Sutrikimas — {reason}",
    "react.extensions.display_running_action": "Vykdoma: {label}…",
  };
  const t: I18nContextValue["t"] = (key, defaultText, params) =>
    fallbackContext.t(key, messages[key] || defaultText, params);
  const projection = (support as typeof support & {
    translatedExtensionStatus?: (status: string, translator: I18nContextValue["t"]) => string;
  }).translatedExtensionStatus;
  expect(support.detailHealthStatusFromStatus("Checking health...")).toBe("Checking health...");
  expect(projection?.("Checking health...", t)).toBe("Tikrinama būsena…");
  expect(projection?.("Unhealthy — Vendor code: x_42", t)).toBe("Sutrikimas — Vendor code: x_42");
  expect(projection?.("Running My authored action...", t)).toBe("Vykdoma: My authored action…");
  expect(projection?.("Vendor-specific output: code_99", t)).toBe("Vendor-specific output: code_99");
});

test("the help link prefers the documentation the Marketplace links", () => {
  const catalog = {
    items: [{ extension_id: "acme.model-provider", repository_url: "https://example.test/ollama#readme" }],
  } as unknown as ExtensionCatalogListing;

  expect(extensionDocumentationUrl(ollama, catalog)).toBe("https://example.test/ollama#readme");
});

test("without a catalog entry it uses the website, not the issue tracker", () => {
  // Seen in the demo: "How to fill this in" opened Ollama's GitHub issues.
  expect(extensionDocumentationUrl(ollama, undefined)).toBe(
    "https://github.com/Flow-Steward/ext-llm-ollama",
  );
  expect(extensionDocumentationUrl({ ...ollama, website_url: "" }, undefined)).toBe(
    "https://github.com/Flow-Steward/ext-llm-ollama/issues",
  );
});
