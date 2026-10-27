import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  ExternalResourcePicker,
  type ExternalResourcePickerBridge,
} from "@/components/extensions/external-resource-picker";
import { createExternalResourcePickerBridge } from "@/components/extensions/external-resource-picker-bridge";
import adversarialCases from "@/components/extensions/external-resource-picker-adversarial.json";

type SharedPickerMutation = {
  operation: "delete" | "set";
  path: string;
  value?: unknown;
};

function applySharedPickerMutation(value: unknown, mutation: SharedPickerMutation): unknown {
  const clone = structuredClone(value) as Record<string, unknown>;
  const path = mutation.path.split(".");
  let cursor: Record<string, unknown> | unknown[] = clone;
  for (const segment of path.slice(0, -1)) {
    const nested = Array.isArray(cursor) ? cursor[Number(segment)] : cursor[segment];
    if (!nested || typeof nested !== "object") {
      throw new Error("invalid_test_mutation_path");
    }
    cursor = nested as Record<string, unknown> | unknown[];
  }
  const final = path.at(-1) || "";
  if (Array.isArray(cursor)) {
    if (mutation.operation === "delete") {
      cursor.splice(Number(final), 1);
    } else {
      cursor[Number(final)] = mutation.value;
    }
  } else if (mutation.operation === "delete") {
    delete cursor[final];
  } else {
    cursor[final] = mutation.value;
  }
  return clone;
}

describe("ExternalResourcePicker", () => {
  it("loads only scripts from declared HTTPS origins and passes bounded bootstrap data to a standard bridge", async () => {
    const vendorOpen = vi.fn().mockResolvedValue([{ opaque_id: "resource-a" }]);
    const loadScript = vi.fn().mockResolvedValue(undefined);
    const executeBootstrap = vi.fn().mockResolvedValue({ session: "short-lived" });
    const bridge = createExternalResourcePickerBridge({
      allowed_script_origins: ["https://sdk.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://sdk.example.test/picker.js" }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    }, {
      executeBootstrap,
      globalObject: { FixturePicker: { open: vendorOpen } },
      loadScript,
    });

    await expect(bridge.open()).resolves.toEqual([{ opaque_id: "resource-a" }]);
    expect(loadScript).toHaveBeenCalledWith("https://sdk.example.test/picker.js");
    expect(executeBootstrap).toHaveBeenCalledWith("picker_bootstrap");
    expect(vendorOpen).toHaveBeenCalledWith({ bootstrap: { session: "short-lived" }, options: {} });
  });

  it("resolves a bundle-local bridge asset without treating it as a provider origin", async () => {
    const loadScript = vi.fn().mockResolvedValue(undefined);
    const bridge = createExternalResourcePickerBridge({
      allowed_script_origins: ["https://sdk.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [
        { src: "https://sdk.example.test/picker.js" },
        { asset_ref: "assets/picker-bridge.js" },
      ],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    }, {
      executeBootstrap: vi.fn().mockResolvedValue({ session: "short-lived" }),
      globalObject: {
        FixturePicker: { open: vi.fn().mockResolvedValue([{ opaque_id: "resource-a" }]) },
      },
      loadScript,
      resolveAssetScript: (assetRef) => `/api/extensions/fixture/assets/${assetRef}`,
    });

    await bridge.open();

    expect(loadScript).toHaveBeenNthCalledWith(1, "https://sdk.example.test/picker.js");
    expect(loadScript).toHaveBeenNthCalledWith(
      2,
      "/api/extensions/fixture/assets/assets/picker-bridge.js",
    );
  });

  it("rejects a script outside the declared origin before bootstrap or browser execution", async () => {
    const executeBootstrap = vi.fn();
    expect(() => createExternalResourcePickerBridge({
      allowed_script_origins: ["https://sdk.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://other.example.test/picker.js" }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    }, {
      executeBootstrap,
      globalObject: {},
      loadScript: vi.fn(),
    })).toThrow("invalid_external_picker_script");
    expect(executeBootstrap).not.toHaveBeenCalled();
  });

  it("rejects unbounded selection declarations before bootstrap or browser execution", () => {
    const executeBootstrap = vi.fn();
    expect(() => createExternalResourcePickerBridge({
      allowed_script_origins: ["https://sdk.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://sdk.example.test/picker.js" }],
      selection: {
        maximum: 10_000,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    }, {
      executeBootstrap,
      globalObject: {},
      loadScript: vi.fn(),
    })).toThrow("invalid_external_picker_declaration");
    expect(executeBootstrap).not.toHaveBeenCalled();
  });

  it("keeps the browser defense aligned with the closed install-time declaration", () => {
    const valid = {
      allowed_script_origins: ["https://sdk-closed.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://sdk-closed.example.test/picker.js" }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    };
    const invalid = [
      { ...valid, unknown_key: true },
      {
        ...valid,
        selection: {
          ...valid.selection,
          fields: [{ ...valid.selection.fields[0], required: "yes" }],
        },
      },
      {
        ...valid,
        scripts: [{ src: "https://sdk-closed.example.test/picker.js", async: true }],
      },
    ];

    for (const declaration of invalid) {
      expect(() => createExternalResourcePickerBridge(declaration as never, {
        executeBootstrap: vi.fn(),
        globalObject: {},
        loadScript: vi.fn(),
      })).toThrow();
    }
  });

  it("uses the shared canonical URL and UTF-16 copy matrix", () => {
    const declaration = {
      allowed_script_origins: [adversarialCases.valid_origin],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: adversarialCases.valid_script }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    };
    const dependencies = {
      executeBootstrap: vi.fn(),
      globalObject: {},
      loadScript: vi.fn(),
    };

    expect(() => createExternalResourcePickerBridge(declaration, dependencies)).not.toThrow();
    for (const origin of adversarialCases.invalid_origins) {
      expect(() => createExternalResourcePickerBridge({
        ...declaration,
        allowed_script_origins: [origin],
      }, dependencies)).toThrow();
    }
    for (const source of adversarialCases.invalid_scripts) {
      expect(() => createExternalResourcePickerBridge({
        ...declaration,
        scripts: [{ src: source }],
      }, dependencies)).toThrow();
    }
    for (const buttonLabel of adversarialCases.invalid_button_labels) {
      expect(() => createExternalResourcePickerBridge({
        ...declaration,
        button_label: buttonLabel,
      }, dependencies)).toThrow();
    }
    for (const buttonLabel of adversarialCases.valid_button_labels) {
      expect(() => createExternalResourcePickerBridge({
        ...declaration,
        button_label: buttonLabel,
      }, dependencies)).not.toThrow();
    }
    for (const codeUnit of adversarialCases.invalid_unicode_code_units) {
      const malformed = String.fromCharCode(codeUnit);
      expect(() => createExternalResourcePickerBridge({
        ...declaration,
        button_label: malformed,
      }, dependencies)).toThrow("invalid_external_picker_declaration");
      expect(() => createExternalResourcePickerBridge({
        ...declaration,
        options: { payload: malformed },
      }, dependencies)).toThrow("invalid_external_picker_declaration");
    }
    for (const pair of adversarialCases.valid_surrogate_pairs) {
      const paired = String.fromCharCode(pair.high, pair.low);
      expect(() => createExternalResourcePickerBridge({
        ...declaration,
        button_label: paired,
      }, dependencies)).not.toThrow();
      expect(() => createExternalResourcePickerBridge({
        ...declaration,
        options: { nested: { payload: paired } },
      }, dependencies)).not.toThrow();
    }
    for (const mutation of adversarialCases.invalid_scalar_cases as SharedPickerMutation[]) {
      expect(() => createExternalResourcePickerBridge(
        applySharedPickerMutation(declaration, mutation) as never,
        dependencies,
      )).toThrow("invalid_external_picker_declaration");
    }
    for (const mutation of adversarialCases.valid_scalar_cases as SharedPickerMutation[]) {
      expect(() => createExternalResourcePickerBridge(
        applySharedPickerMutation(declaration, mutation) as never,
        dependencies,
      )).not.toThrow();
    }
  });

  it("uses the shared UTF-16 JSON bound for picker options", () => {
    const counts = adversarialCases.options_utf16_cases;
    const declaration = {
      allowed_script_origins: [adversarialCases.valid_origin],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: adversarialCases.valid_script }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    };
    const dependencies = {
      executeBootstrap: vi.fn(),
      globalObject: {},
      loadScript: vi.fn(),
    };

    expect(() => createExternalResourcePickerBridge({
      ...declaration,
      options: { payload: "😀".repeat(counts.valid_emoji_count) },
    }, dependencies)).not.toThrow();
    expect(() => createExternalResourcePickerBridge({
      ...declaration,
      options: { payload: "😀".repeat(counts.invalid_emoji_count) },
    }, dependencies)).toThrow("invalid_external_picker_declaration");
    for (const boundary of adversarialCases.invalid_options_numeric_boundaries) {
      expect(() => createExternalResourcePickerBridge({
        ...declaration,
        options: { n: boundary.number, payload: "x".repeat(boundary.payload_length) },
      }, dependencies)).toThrow("invalid_external_picker_declaration");
    }
  });

  it("validates optional connect and frame origins with the canonical bounded HTTPS policy", () => {
    const declaration = {
      allowed_script_origins: ["https://sdk.example.test"],
      allowed_connect_origins: ["https://api.example.test"],
      allowed_frame_origins: ["https://frame.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://sdk.example.test/picker.js" }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    };
    const dependencies = {
      executeBootstrap: vi.fn(),
      globalObject: {},
      loadScript: vi.fn(),
    };

    expect(() => createExternalResourcePickerBridge(declaration, dependencies)).not.toThrow();
    for (const key of ["allowed_connect_origins", "allowed_frame_origins"] as const) {
      for (const origins of [
        [],
        ["http://api.example.test"],
        ["https://API.example.test"],
        ["https://api.example.test/"],
        ["https://api.example.test", "https://api.example.test"],
        Array.from({ length: 5 }, (_, index) => `https://api-${index}.example.test`),
      ]) {
        expect(() => createExternalResourcePickerBridge({
          ...declaration,
          [key]: origins,
        }, dependencies)).toThrow("invalid_external_picker_declaration");
      }
    }
  });

  it("rejects secret-bearing bootstrap results before the browser SDK", async () => {
    const vendorOpen = vi.fn();
    const bridge = createExternalResourcePickerBridge({
      allowed_script_origins: ["https://sdk-sensitive.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://sdk-sensitive.example.test/picker.js" }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    }, {
      executeBootstrap: vi.fn().mockResolvedValue({
        browser_token: "safe",
        nested: { refresh_token: "must-not-enter-browser-sdk" },
      }),
      globalObject: { FixturePicker: { open: vendorOpen } },
      loadScript: vi.fn().mockResolvedValue(undefined),
    });

    await expect(bridge.open()).rejects.toThrow("invalid_external_picker_bootstrap");
    expect(vendorOpen).not.toHaveBeenCalled();
  });

  it.each([
    "access_token",
    "accessToken",
    "ACCESS-TOKEN",
    "access.token",
    "oauth_access_token",
    "oauthAccessToken",
    "OAUTH-ACCESS-TOKEN",
    "oauth.access.token",
    "token",
    "TOKEN",
    "to-ken",
    "to.ken",
    "bearer_token",
    "bearerToken",
    "BEARER-TOKEN",
    "bearer.token",
    "session_token",
    "sessionToken",
    "SESSION-TOKEN",
    "session.token",
    "authorization",
    "Authorization",
    "AUTH-ORIZATION",
    "auth.orization",
  ])("rejects top-level and nested generic credential key %s before the browser SDK", async (key) => {
    for (const bootstrap of [
      { [key]: "must-not-enter-browser-sdk" },
      { safe: { nested: [{ [key]: "must-not-enter-browser-sdk" }] } },
    ]) {
      const vendorOpen = vi.fn();
      const bridge = createExternalResourcePickerBridge({
        allowed_script_origins: ["https://sdk-sensitive.example.test"],
        bootstrap_action_id: "picker_bootstrap",
        bridge_global: "FixturePicker",
        button_label: "Choose resource",
        target_component_id: "resource_form",
        scripts: [{ src: "https://sdk-sensitive.example.test/picker.js" }],
        selection: {
          maximum: 1,
          fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
        },
      }, {
        executeBootstrap: vi.fn().mockResolvedValue(bootstrap),
        globalObject: { FixturePicker: { open: vendorOpen } },
        loadScript: vi.fn().mockResolvedValue(undefined),
      });

      await expect(bridge.open()).rejects.toThrow("invalid_external_picker_bootstrap");
      expect(vendorOpen).not.toHaveBeenCalled();
    }
  });

  it("passes bounded non-secret picker bootstrap fields to the browser SDK", async () => {
    const vendorOpen = vi.fn().mockResolvedValue([{ opaque_id: "resource-a" }]);
    const bootstrap = {
      app_id: "1234567890",
      developer_key: "picker-key",
      origin: "https://browser.example.test",
    };
    const bridge = createExternalResourcePickerBridge({
      allowed_script_origins: ["https://sdk-safe.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://sdk-safe.example.test/picker.js" }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    }, {
      executeBootstrap: vi.fn().mockResolvedValue(bootstrap),
      globalObject: { FixturePicker: { open: vendorOpen } },
      loadScript: vi.fn().mockResolvedValue(undefined),
    });

    await expect(bridge.open()).resolves.toEqual([{ opaque_id: "resource-a" }]);
    expect(vendorOpen).toHaveBeenCalledWith({ bootstrap, options: {} });
  });

  it("rejects dotted source paths and malformed rows without truncating or filtering", async () => {
    const base = {
      allowed_script_origins: ["https://sdk-shape.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "FixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://sdk-shape.example.test/picker.js" }],
      selection: {
        maximum: 1,
        fields: [{ source: "resource.id", target: "resource_id", max_length: 128 }],
      },
    };
    expect(() => createExternalResourcePickerBridge(base, {
      executeBootstrap: vi.fn(),
      globalObject: {},
      loadScript: vi.fn(),
    })).toThrow("invalid_external_picker_declaration");

    const bridge = createExternalResourcePickerBridge({
      ...base,
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    }, {
      executeBootstrap: vi.fn().mockResolvedValue({ session: "safe" }),
      globalObject: {
        FixturePicker: { open: vi.fn().mockResolvedValue([{ opaque_id: "ok" }, null]) },
      },
      loadScript: vi.fn().mockResolvedValue(undefined),
    });
    await expect(bridge.open()).rejects.toThrow("invalid_external_picker_selection");
  });

  it("deduplicates concurrent and repeated successful SDK loads", async () => {
    const globalObject = {
      CacheFixturePicker: { open: vi.fn().mockResolvedValue([{ opaque_id: "resource-a" }]) },
    };
    const bridge = createExternalResourcePickerBridge({
      allowed_script_origins: ["https://sdk-cache.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "CacheFixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://sdk-cache.example.test/picker.js" }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    }, {
      executeBootstrap: vi.fn().mockResolvedValue({ session: "safe" }),
      globalObject,
    });

    const first = bridge.open();
    const concurrent = bridge.open();
    // The unit under test owns a document-level SDK cache, so DOM inspection is the observable boundary.
    // eslint-disable-next-line testing-library/no-node-access
    const script = document.head.querySelector<HTMLScriptElement>('script[src="https://sdk-cache.example.test/picker.js"]');
    expect(script).not.toBeNull();
    script?.dispatchEvent(new Event("load"));
    await Promise.all([first, concurrent]);
    await bridge.open();
    // eslint-disable-next-line testing-library/no-node-access
    expect(document.head.querySelectorAll('script[src="https://sdk-cache.example.test/picker.js"]')).toHaveLength(1);
  });

  it("evicts a failed SDK load so the next open can retry", async () => {
    const bridge = createExternalResourcePickerBridge({
      allowed_script_origins: ["https://sdk-retry.example.test"],
      bootstrap_action_id: "picker_bootstrap",
      bridge_global: "RetryFixturePicker",
      button_label: "Choose resource",
      target_component_id: "resource_form",
      scripts: [{ src: "https://sdk-retry.example.test/picker.js" }],
      selection: {
        maximum: 1,
        fields: [{ source: "opaque_id", target: "resource_id", max_length: 128 }],
      },
    }, {
      executeBootstrap: vi.fn().mockResolvedValue({ session: "safe" }),
      globalObject: {
        RetryFixturePicker: { open: vi.fn().mockResolvedValue([{ opaque_id: "resource-a" }]) },
      },
    });
    const failed = bridge.open();
    // eslint-disable-next-line testing-library/no-node-access
    document.head.querySelector<HTMLScriptElement>('script[src="https://sdk-retry.example.test/picker.js"]')?.dispatchEvent(new Event("error"));
    await expect(failed).rejects.toThrow("external_picker_script_unavailable");

    const retried = bridge.open();
    // eslint-disable-next-line testing-library/no-node-access
    document.head.querySelector<HTMLScriptElement>('script[src="https://sdk-retry.example.test/picker.js"]')?.dispatchEvent(new Event("load"));
    await expect(retried).resolves.toEqual([{ opaque_id: "resource-a" }]);
  });

  it("accepts only declaration-shaped selections and binds declared fields", async () => {
    const user = userEvent.setup();
    const onSelection = vi.fn();
    const bridge: ExternalResourcePickerBridge = {
      open: vi.fn().mockResolvedValue([
        { opaque_id: "resource-a", title: "Quarterly plan", ignored_vendor_value: "drop-me" },
      ]),
    };
    render(
      <ExternalResourcePicker
        bridge={bridge}
        declaration={{
          button_label: "Choose resource",
          selection: {
            maximum: 1,
            fields: [
              { source: "opaque_id", target: "resource_id", required: true, max_length: 128 },
              { source: "title", target: "resource_name", required: true, max_length: 200 },
            ],
          },
        }}
        disabled={false}
        onSelection={onSelection}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Choose resource" }));
    expect(onSelection).toHaveBeenCalledWith([
      { resource_id: "resource-a", resource_name: "Quarterly plan" },
    ]);
  });

  it("fails closed with safe declared copy and restores trigger focus", async () => {
    const user = userEvent.setup();
    const bridge: ExternalResourcePickerBridge = {
      open: vi.fn().mockResolvedValue([{ opaque_id: "x".repeat(200) }]),
    };
    render(
      <ExternalResourcePicker
        bridge={bridge}
        declaration={{
          button_label: "Choose resource",
          error_message: "Selection could not be applied. Try again.",
          selection: {
            maximum: 1,
            fields: [
              { source: "opaque_id", target: "resource_id", required: true, max_length: 16 },
            ],
          },
        }}
        disabled={false}
        onSelection={vi.fn()}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Choose resource" });
    await user.click(trigger);
    expect(await screen.findByRole("alert")).toHaveTextContent("Selection could not be applied. Try again.");
    expect(trigger).toHaveFocus();
  });
});
