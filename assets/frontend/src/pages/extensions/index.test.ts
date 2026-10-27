import { describe, expect, it } from "vitest";

import {
  contractActionInputAllowsEmptyString,
  contractActionInputs,
  fieldOptions,
  manifestPageRequiresProject,
  stringListFromUnknown,
} from "@/pages/extensions/extension-manifest-utils";
import { normalizeComponentPayload } from "@/pages/extensions/payload";

describe("normalizeComponentPayload", () => {
  it("prefers query.response payloads over the typed query envelope wrapper", () => {
    expect(
      normalizeComponentPayload({
        extension_id: "acme.messenger",
        query_id: "list_channels",
        query: {
          response: {
            ok: true,
            items: [{ channel_id: "live-group" }],
          },
          status: "succeeded",
        },
      }),
    ).toEqual({
      ok: true,
      items: [{ channel_id: "live-group" }],
    });
  });

  it("unwraps typed query envelopes returned by the extension query endpoint", () => {
    expect(
      normalizeComponentPayload({
        extension_id: "acme.messenger",
        query_id: "get_inbound_status",
        query: {
          ok: true,
          items: [{ active_transport: "polling", inbound_mode: "Auto" }],
        },
      }),
    ).toEqual({
      ok: true,
      items: [{ active_transport: "polling", inbound_mode: "Auto" }],
    });
  });

  it("unwraps typed action envelopes returned by the extension action endpoint", () => {
    expect(
      normalizeComponentPayload({
        extension_id: "acme.messenger",
        action_id: "poll_now",
        action: {
          ok: true,
          values: { inbound_mode: "auto" },
        },
      }),
    ).toEqual({
      ok: true,
      values: { inbound_mode: "auto" },
    });
  });
});

describe("fieldOptions", () => {
  it("reads select options from the field-level manifest contract", () => {
    expect(
      fieldOptions({
        widget: "select",
        options: [
          { value: "starttls", label: "STARTTLS" },
          { value: "ssl", label: "SSL" },
        ],
      }),
    ).toEqual([
      { value: "starttls", label: "STARTTLS" },
      { value: "ssl", label: "SSL" },
    ]);
  });

  it.each(["direct", "legacy"])("preserves only literal boolean disabled flags in %s options", (location) => {
    const options = [
      { value: "none", label: "No TLS (unavailable)", disabled: true },
      { value: "ssl", label: "SSL/TLS", disabled: false },
      { value: "starttls", label: "STARTTLS" },
      { value: "string", label: "String flag", disabled: "true" },
      { value: "false-string", label: "False string flag", disabled: "false" },
      { value: "number", label: "Numeric flag", disabled: 1 },
      { value: "none", label: "Duplicate" },
      { value: "", label: "Invalid", disabled: true },
    ];
    expect(fieldOptions(location === "direct" ? { options } : { props: { options } })).toEqual([
      { value: "none", label: "No TLS (unavailable)", disabled: true },
      { value: "ssl", label: "SSL/TLS", disabled: false },
      { value: "starttls", label: "STARTTLS" },
      { value: "string", label: "String flag" },
      { value: "false-string", label: "False string flag" },
      { value: "number", label: "Numeric flag" },
    ]);
  });

  it("keeps supporting legacy props.options select manifests", () => {
    expect(
      fieldOptions({
        widget: "select",
        props: {
          options: [{ value: "one", label: "One" }],
        },
      }),
    ).toEqual([{ value: "one", label: "One" }]);
  });

  it("drops invalid or duplicate select options before rendering", () => {
    expect(
      fieldOptions({
        widget: "select",
        options: [
          {},
          { value: "starttls", label: "STARTTLS" },
          { value: "starttls", label: "Duplicate" },
          { value: "ssl", label: "" },
        ],
      }),
    ).toEqual([{ value: "starttls", label: "STARTTLS" }]);
  });
});

describe("manifestPageRequiresProject", () => {
  it("keeps static Markdown pages independent of project scope", () => {
    expect(
      manifestPageRequiresProject(
        {
          page_id: "about",
          components: [
            {
              component_id: "about_copy",
              type: "markdown",
              body: "About this extension.",
            },
          ],
        },
        true,
      ),
    ).toBe(false);
  });

  it("requires a project for query-backed Markdown and interactive components", () => {
    expect(
      manifestPageRequiresProject(
        {
          components: [
            { component_id: "status", type: "markdown", query_id: "get_status" },
          ],
        },
        true,
      ),
    ).toBe(true);
    expect(
      manifestPageRequiresProject(
        {
          components: [
            { component_id: "configuration", type: "form", fields: [] },
          ],
        },
        true,
      ),
    ).toBe(true);
  });

  it("honors an explicitly project-scoped Markdown-only page", () => {
    expect(
      manifestPageRequiresProject(
        {
          page_id: "oauth_setup",
          project_scoped: true,
          components: [
            {
              component_id: "setup_copy",
              type: "markdown",
              body: "Configure the provider for this project.",
            },
          ],
        },
        true,
      ),
    ).toBe(true);
  });

  it("does not impose project scope on account-scoped extensions", () => {
    expect(
      manifestPageRequiresProject(
        {
          components: [
            { component_id: "configuration", type: "form", fields: [] },
          ],
        },
        false,
      ),
    ).toBe(false);
  });

  it("keeps an explicitly static catalog page independent of project scope", () => {
    expect(
      manifestPageRequiresProject(
        {
          page_id: "supported_connectors",
          project_scoped: false,
          components: [
            { component_id: "catalog", type: "grid", query_id: "supported_connectors" },
          ],
        },
        true,
      ),
    ).toBe(false);
  });
});

describe("stringListFromUnknown", () => {
  it("keeps scalar manifest lists used by connection_form metadata", () => {
    expect(stringListFromUnknown(["provider", "password", "", null, "password"])).toEqual([
      "provider",
      "password",
    ]);
  });
});

describe("contractActionInputAllowsEmptyString", () => {
  it("preserves nullable Contract V2 string schemas while normalizing text inputs", () => {
    const inputs = contractActionInputs({
      operation_manifest: [{
        operation_id: "save_config",
        inputs: [{
          name: "message_prefix",
          value_type: "text",
          schema: { type: ["string", "null"] },
        }],
      }],
    }, "save_config");

    expect(inputs).toEqual([{
      name: "message_prefix",
      type: "string",
      required: false,
      schema: {
        type: ["string", "null"],
      },
    }]);
    expect(contractActionInputAllowsEmptyString(inputs![0])).toBe(true);
  });

  it.each([
    ["plain string", { type: "string" }],
    ["zero minimum", { type: "string", minLength: 0 }],
    ["matching pattern", { type: "string", pattern: "^$" }],
    ["including enum", { type: "string", enum: ["", "configured"] }],
    ["matching const", { type: "string", const: "" }],
    ["passing allOf", { type: "string", allOf: [{ minLength: 0 }, { pattern: "^$" }] }],
    ["passing anyOf", { type: "string", anyOf: [{ minLength: 1 }, { const: "" }] }],
    ["exactly one oneOf", { type: "string", oneOf: [{ const: "" }, { const: "configured" }] }],
    ["passing not", { type: "string", not: { minLength: 1 } }],
    ["passing then", { type: "string", if: { const: "" }, then: { pattern: "^$" } }],
    ["passing else", { type: "string", if: { minLength: 1 }, else: { const: "" } }],
  ])("accepts empty for a %s schema", (_name, schema) => {
    expect(contractActionInputAllowsEmptyString({
      name: "field",
      type: "string",
      required: false,
      schema,
    })).toBe(true);
  });

  it.each([
    ["non-string type", { type: "number" }],
    ["positive minimum", { type: "string", minLength: 1 }],
    ["negative maximum", { type: "string", maxLength: -1 }],
    ["format", { type: "string", format: "email" }],
    ["excluding pattern", { type: "string", pattern: ".+" }],
    ["invalid pattern", { type: "string", pattern: "[" }],
    ["excluding enum", { type: "string", enum: ["configured"] }],
    ["excluding const", { type: "string", const: "configured" }],
    ["failing allOf", { type: "string", allOf: [{ minLength: 0 }, { minLength: 1 }] }],
    ["failing anyOf", { type: "string", anyOf: [{ minLength: 1 }, { const: "configured" }] }],
    ["ambiguous oneOf", { type: "string", oneOf: [{ minLength: 0 }, { pattern: "^$" }] }],
    ["failing not", { type: "string", not: { minLength: 0 } }],
    ["failing then", { type: "string", if: { const: "" }, then: { minLength: 1 } }],
    ["failing else", { type: "string", if: { minLength: 1 }, else: { minLength: 1 } }],
    ["unresolved ref", { type: "string", $ref: "#/$defs/non_empty" }],
  ])("rejects empty for a %s schema", (_name, schema) => {
    expect(contractActionInputAllowsEmptyString({
      name: "field",
      type: "string",
      required: false,
      schema,
    })).toBe(false);
  });
});
