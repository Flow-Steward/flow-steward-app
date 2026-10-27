import * as extensionsApi from "@/pages/extensions/api";
import { extensionInstallWarnings } from "@/pages/extensions/extension-install-warnings";
import { fetchJson } from "@/lib/http/client";
import {
  checkExtensionHealth,
  executeExtensionAction,
  fetchExtensionArchiveLifecycleAvailability,
  fetchExtension,
  fetchExtensions,
  installExtensionFromFile,
  refreshLlmProviderModels,
  saveLlmProviderCredentials,
  setLlmProviderModelEnabled,
  setExtensionEnabled,
} from "@/pages/extensions/api";

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

const mockedFetchJson = vi.mocked(fetchJson);

describe("extensions api", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
  });

  it("loads only the frozen safe effect projection for the selected project and install", async () => {
    mockedFetchJson.mockResolvedValue({ effects: [], truncated: false });
    await expect(extensionsApi.fetchExtensionExternalEffects("project /1", "install/1")).resolves.toEqual({ effects: [], truncated: false });
    expect(mockedFetchJson).toHaveBeenCalledExactlyOnceWith("/api/projects/project%20%2F1/extensions/install%2F1/external-effects");
  });

  it("records only the observed version, action and comment through existing fetch transport", async () => {
    mockedFetchJson.mockResolvedValue({ resolution_ref: { effect_id: "effect/1" } });
    await extensionsApi.resolveExtensionExternalEffect("project/1", "install/1", "effect/1", {
      observed_version: 7, action: "reject", comment: "Verified provider records",
    });
    expect(mockedFetchJson).toHaveBeenCalledExactlyOnceWith(
      "/api/projects/project%2F1/extensions/install%2F1/external-effects/effect%2F1/resolution",
      { method: "POST", body: JSON.stringify({ observed_version: 7, action: "reject", comment: "Verified provider records" }) },
    );
  });

  it("normalizes nested health payloads returned by the backend contract", async () => {
    mockedFetchJson.mockResolvedValue({
      extension_id: "smoke.guardrail.enforcement",
      health: {
        status: "healthy",
        latency_ms: 84,
      },
    });

    await expect(checkExtensionHealth("smoke.guardrail.enforcement")).resolves.toEqual(
      expect.objectContaining({
        extension_id: "smoke.guardrail.enforcement",
        status: "healthy",
        latency_ms: 84,
      }),
    );
    expect(mockedFetchJson).toHaveBeenCalledWith(
      "/api/extensions/smoke.guardrail.enforcement/health",
    );
  });

  it("routes enable and disable commands to their matching backend endpoints", async () => {
    mockedFetchJson.mockResolvedValue({});

    await setExtensionEnabled("vendor.demo extension", true);
    await setExtensionEnabled("vendor.demo extension", false);

    expect(mockedFetchJson).toHaveBeenNthCalledWith(
      1,
      "/api/extensions/vendor.demo%20extension/enable",
      {
        method: "POST",
        body: JSON.stringify({ enabled: true }),
      },
    );
    expect(mockedFetchJson).toHaveBeenNthCalledWith(
      2,
      "/api/extensions/vendor.demo%20extension/disable",
      {
        method: "POST",
        body: JSON.stringify({ enabled: false }),
      },
    );
  });

  it("loads the archive lifecycle capability from its canonical endpoint", async () => {
    mockedFetchJson.mockResolvedValue({
      available: false,
      reason_code: "managed_root_read_only",
      message: "Extension archive lifecycle is unavailable in this deployment.",
      remediation: {
        install_update: ["./flow-steward extensions install --archive <path>"],
        uninstall: ["./flow-steward extensions uninstall <extension-id>"],
      },
    });

    await expect(fetchExtensionArchiveLifecycleAvailability()).resolves.toEqual(
      expect.objectContaining({ available: false, reason_code: "managed_root_read_only" }),
    );
    expect(mockedFetchJson).toHaveBeenCalledWith(
      "/api/extensions/capabilities/archive-lifecycle",
      { cache: "no-store" },
    );
  });

  it("sends browser authority approval only on the explicit retry", async () => {
    mockedFetchJson.mockResolvedValue({ extension: {} });
    const impactHash = "a".repeat(64);
    const file = new File(["zip"], "picker.zip", { type: "application/zip" });

    await setExtensionEnabled("acme.picker", true, impactHash);
    await installExtensionFromFile(file, impactHash);

    expect(mockedFetchJson).toHaveBeenNthCalledWith(
      1,
      "/api/extensions/acme.picker/enable",
      {
        method: "POST",
        body: JSON.stringify({
          enabled: true,
          approved_browser_authority_impact_hash: impactHash,
        }),
      },
    );
    const installRequest = mockedFetchJson.mock.calls[1];
    expect(installRequest[0]).toBe("/api/extensions/install");
    expect((installRequest[1]?.body as FormData).get("file")).toBe(file);
    expect(
      (installRequest[1]?.body as FormData).get("approved_browser_authority_impact_hash"),
    ).toBe(impactHash);
  });

  it("treats the All list filter as an unfiltered extension query", async () => {
    mockedFetchJson.mockResolvedValue({ extensions: [] });

    await fetchExtensions("all");
    await fetchExtensions("active");

    expect(mockedFetchJson).toHaveBeenNthCalledWith(1, "/api/extensions", {
      cache: "no-store",
    });
    expect(mockedFetchJson).toHaveBeenNthCalledWith(2, "/api/extensions?state=active", {
      cache: "no-store",
    });
  });

  it("loads extension detail from the canonical detail endpoint", async () => {
    mockedFetchJson.mockResolvedValue({
      extension_id: "acme.messenger",
      display_name: "Chat Relay",
      state: "active",
    });

    await expect(fetchExtension("acme.messenger")).resolves.toEqual(
      expect.objectContaining({ extension_id: "acme.messenger", active: true }),
    );
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/extensions/acme.messenger", {
      cache: "no-store",
    });
  });

  it("normalizes catalog publisher, category, tags, and official trust metadata", async () => {
    mockedFetchJson.mockResolvedValue({
      extensions: [
        {
          extension_id: "acme.mail",
          display_name: "Outgoing Mail",
          state: "active",
          vendor_name: "Acme",
          category: "messaging-notifications",
          tags: ["mail", "smtp"],
        },
      ],
    });

    const result = await fetchExtensions("all");

    expect(result[0]).toEqual(
      expect.objectContaining({
        display_name: "Outgoing Mail",
        vendor_name: "Acme",
        category: "messaging-notifications",
        tags: ["mail", "smtp"],
      }),
    );
  });

  it("uses the shared LLM-provider endpoints for save, discovery, and verification", async () => {
    mockedFetchJson.mockResolvedValue({});
    await saveLlmProviderCredentials("install 1", {
      secrets: { api_key: "secret" },
      connection_config: { upstream_base_url: "https://acme-llm.example" },
    });
    await refreshLlmProviderModels("install 1", { upstream_base_url: "https://acme-llm.example" });
    await setLlmProviderModelEnabled("model 1", true);

    expect(mockedFetchJson).toHaveBeenNthCalledWith(
      1,
      "/api/llm-provider-extensions/install%201/credentials",
      expect.objectContaining({ method: "POST" }),
    );
    expect(mockedFetchJson).toHaveBeenNthCalledWith(
      2,
      "/api/llm-provider-extensions/install%201/refresh-models",
      expect.objectContaining({ method: "POST" }),
    );
    expect(mockedFetchJson).toHaveBeenNthCalledWith(
      3,
      "/api/llm-provider-extensions/models/model%201/enable",
      { method: "POST" },
    );
  });

  it("assigns a fresh host idempotency identity to each direct action intent", async () => {
    mockedFetchJson.mockResolvedValue({});

    await executeExtensionAction("acme.mail", "send", { input: { subject: "Same" } });
    await executeExtensionAction("acme.mail", "send", { input: { subject: "Same" } });

    const firstHeaders = mockedFetchJson.mock.calls[0]?.[1]?.headers as Record<string, string>;
    const secondHeaders = mockedFetchJson.mock.calls[1]?.[1]?.headers as Record<string, string>;
    expect(firstHeaders["Idempotency-Key"]).toBeTruthy();
    expect(secondHeaders["Idempotency-Key"]).toBeTruthy();
    expect(firstHeaders["Idempotency-Key"]).not.toBe(secondHeaders["Idempotency-Key"]);
  });

  it("uses the logical UI submission identity for an explicit action retry", async () => {
    mockedFetchJson.mockResolvedValue({});

    await executeExtensionAction(
      "acme.mail",
      "send",
      { input: { subject: "Same" } },
      "logical-action-retry-1",
    );

    expect(mockedFetchJson).toHaveBeenCalledWith(
      "/api/extensions/acme.mail/action/send",
      expect.objectContaining({
        headers: { "Idempotency-Key": "logical-action-retry-1" },
      }),
    );
  });
});

describe("extensionInstallWarnings", () => {
  it("collects the install check's warnings once, from the summary and the compiled manifest", () => {
    const socket = { message: "smtp_extension.py:9: import socket opens raw network connections" };
    const warnings = extensionInstallWarnings({
      extension_id: "acme.mailer",
      validation_summary: { validation_warnings: [socket], compile_warnings: [] },
      compiled_manifest: {
        validation_warnings: [socket, { message: "connector/main.py:403: subprocess.Popen() starts a process" }],
      },
    });

    expect(warnings).toEqual([
      "smtp_extension.py:9: import socket opens raw network connections",
      "connector/main.py:403: subprocess.Popen() starts a process",
    ]);
  });

  it("has nothing for an extension without warnings or without a record", () => {
    expect(extensionInstallWarnings({ extension_id: "x" })).toEqual([]);
    expect(extensionInstallWarnings(null)).toEqual([]);
  });
});
