import { describe, expect, it, vi } from "vitest";
import { fallbackContext } from "@/lib/i18n/context";
import { localizedIF_OPERATOR_OPTIONS } from "./detail-automation-core";

import {
  connectorDefaultOutputMappingForOperation,
  connectorInputMappingRows,
  connectorInputMappingFromRows,
  defaultOverrideForStepKind,
  editableOverrideForRawStep,
  ifConditionDraftFromUnknown,
  ifConditionFromDraft,
  isPlatformOwnedConnectorInput,
  missingRequiredConnectorInputNames,
  overrideDraftForStepKindChange,
  overrideForStepSave,
  withoutPlatformOwnedConnectorInputs,
} from "@/pages/workflows/detail/detail-step-editing";

describe("workflow authoring is independent of the platform language", () => {
  it("keeps an explicit supported blank If editable while protecting unsupported shapes", () => {
    const condition = { selector: "", operator: "equals", value: "" };
    const draft = ifConditionDraftFromUnknown(condition);
    expect(draft).toMatchObject(condition);
    expect(draft.invalidReason).toBeUndefined();
    expect(draft.invalidSource).toBeUndefined();
    expect(ifConditionFromDraft(draft)).toEqual(condition);
    expect(ifConditionDraftFromUnknown("state.count == state.other").invalidReason).toBeDefined();
    expect(ifConditionDraftFromUnknown({ operator: "equals", value: "" }).invalidReason).toBeDefined();
    expect(ifConditionDraftFromUnknown({ selector: 1, operator: "equals", value: "" }).invalidReason).toBeDefined();
    expect(ifConditionDraftFromUnknown({ selector: "", operator: "unknown", value: "" }).invalidReason).toBeDefined();
    expect(ifConditionFromDraft(ifConditionDraftFromUnknown({ selector: "state.plan", operator: "equals", value: "ready" })))
      .toEqual({ selector: "state.plan", operator: "equals", value: "ready" });
  });

  it.each(["always_true", "always_false"])("retains IF-step constant operator %s", (operator) => {
    expect(localizedIF_OPERATOR_OPTIONS()).toContainEqual(expect.objectContaining({ value: operator }));
    expect(ifConditionFromDraft({ selector: "", operator, value: "" })).toEqual({ operator });
  });
  it.each(["UI fixture", "0", "true", "null", 0, true, null])("preserves comparison operand %j through repair, save and reopen", (value) => {
    const draft = ifConditionDraftFromUnknown(`state.fixture == ${JSON.stringify(value)}`);
    const saved = ifConditionFromDraft(draft);
    expect(saved.value).toEqual(value);
    expect(ifConditionFromDraft(ifConditionDraftFromUnknown(saved))).toEqual(saved);
  });
  it("offers a semantics-preserving fix for supported raw equality expressions", () => {
    expect(ifConditionDraftFromUnknown('state.task_summary == "UI fixture"')).toMatchObject({
      selector: "state.task_summary", operator: "equals", value: '"UI fixture"', canApplyFix: true,
    });
    expect(ifConditionDraftFromUnknown('state.count != 0')).toMatchObject({
      selector: "state.count", operator: "not_equals", value: "0", canApplyFix: true,
    });
    expect(ifConditionDraftFromUnknown('state.count == state.other').canApplyFix).not.toBe(true);
    expect(ifConditionDraftFromUnknown('state.count == "state.other"').canApplyFix).not.toBe(true);
    expect(ifConditionDraftFromUnknown('state.count == "0"').value).toBe('"0"');
    expect(ifConditionDraftFromUnknown({ selector: "", operator: "equals" }).canApplyFix).not.toBe(true);
  });
  it("keeps executable literals and authored defaults unchanged under a hostile translator", () => {
    const translator = vi.spyOn(fallbackContext, "t").mockReturnValue("Išversta");
    try {
      for (const kind of ["batch", "if", "template", "switch", "transform", "http_request", "set_variable", "collect"]) {
        expect(defaultOverrideForStepKind(kind)).not.toContain("Išversta");
      }
      expect(translator).not.toHaveBeenCalled();
    } finally {
      translator.mockRestore();
    }
  });
});

describe("tabular write draft defaults", () => {
  it("materializes missing CSV in both the editable draft and Save payload", () => {
    const override = { tabular_feed: { operation: "write", input: { dataset_handle: "state.data" } } };
    expect(editableOverrideForRawStep({ step_kind: "tabular_feed", override }).tabular_feed).toMatchObject({ format: "csv" });
    expect(overrideForStepSave(override, "tabular_feed").tabular_feed).toMatchObject({ format: "csv" });
    expect(override.tabular_feed).not.toHaveProperty("format");
  });
  it.each(["csv", "tsv", "json", "ndjson", "parquet", "xlsx", "", null, "unsupported"])("preserves explicit format %s", (format) => {
    const override = { tabular_feed: { operation: "write", format } };
    expect(editableOverrideForRawStep({ step_kind: "tabular_feed", override })).toEqual(override);
    expect(overrideForStepSave(override, "tabular_feed")).toEqual(override);
  });
  it.each(["read", "transform", "diff"])("does not add a format to %s", (operation) => {
    const override = { tabular_feed: { operation } };
    expect(editableOverrideForRawStep({ step_kind: "tabular_feed", override })).toEqual(override);
    expect(overrideForStepSave(override, "tabular_feed")).toEqual(override);
  });
});

describe("connector default output mapping", () => {
  it("does not map optional-only Gmail list_drafts outputs", () => {
    expect(connectorDefaultOutputMappingForOperation({
      operation_id: "list_drafts",
      returns: {
        type: "object",
        properties: {
          drafts: { type: "array", items: { type: "object" } },
          nextPageToken: { type: "string" },
        },
      },
    })).toEqual({});
  });

  it("does not map optional-only sparse Gmail create_draft outputs", () => {
    expect(connectorDefaultOutputMappingForOperation({
      operation_id: "create_draft",
      returns: {
        type: "object",
        properties: {
          id: { type: "string" },
          messageId: { type: "string" },
          threadId: { type: "string" },
          raw: { type: "string" },
          date: { type: "string" },
          sender: { type: "string" },
          snippet: { type: "string" },
          subject: { type: "string" },
          htmlBody: { type: "string" },
          labelIds: { type: "array", items: { type: "string" } },
          ccRecipients: { type: "array", items: { type: "string" } },
          toRecipients: { type: "array", items: { type: "string" } },
          bccRecipients: { type: "array", items: { type: "string" } },
          plaintextBody: { type: "string" },
        },
      },
    })).toEqual({});
  });

  it("maps only the explicitly required subset of a schema", () => {
    expect(connectorDefaultOutputMappingForOperation({
      returns: {
        type: "object",
        properties: {
          id: { type: "string" },
          messageId: { type: "string" },
          threadId: { type: "string" },
          snippet: { type: "string" },
        },
        required: ["id", "messageId", "threadId"],
      },
    })).toEqual({
      id: "state.id",
      messageId: "state.messageId",
      threadId: "state.threadId",
    });
  });

  it("maps a projected MCP whole-object result through its canonical wrapper", () => {
    expect(connectorDefaultOutputMappingForOperation({
      operation_id: "get_spreadsheet",
      returns: {
        type: "object",
        properties: {
          result: {
            type: "object",
            additionalProperties: {},
          },
        },
        required: ["result"],
      },
    })).toEqual({
      result: "state.result",
    });
  });

  it("does not map unspecified array outputs and maps an explicitly required row", () => {
    expect(connectorDefaultOutputMappingForOperation({
      returns: [
        { name: "message_id", type: "string" },
        { name: "definitely_no_external_effect", type: "boolean" },
        { name: "delivery_status", type: "string", required: true },
      ],
    })).toEqual({
      delivery_status: "state.delivery_status",
    });
  });
});

describe("connector required input projection", () => {
  const parameters = [
    { name: "query", required: true, type: "string" },
    {
      name: "file_id",
      required: true,
      type: "string",
      widget_type: "resource_picker",
    },
    { name: "page_size", required: false, type: "integer" },
  ];

  it("projects absent required fields without inventing optional mappings", () => {
    expect(connectorInputMappingRows({}, parameters)).toEqual([
      { key: "query", mode: "state", value: "" },
      { key: "file_id", mode: "state", value: "" },
    ]);
  });

  it("reports only required mappings without a usable source or literal", () => {
    expect(
      missingRequiredConnectorInputNames(
        {
          query: "state.filters.query",
          file_id: "literal:",
          page_size: "",
        },
        parameters,
      ),
    ).toEqual(["file_id"]);
  });
});

describe("overrideForStepSave connector identity boundary", () => {
  it("keeps connector identity top-level by removing it only from the runtime override", () => {
    const result = overrideForStepSave({
      connector: {
        connector_id: "extension.fixture-storage",
        connector_install_id: "install-fixture",
        operation_id: "search_messages",
        input_mapping: { query: "state.query" },
        output_mapping: { messages: "state.messages" },
        connection_binding: { connection_id: "storage-invoices" },
      },
    }, "connector");

    expect(result.connector).toEqual({
      operation_id: "search_messages",
      input_mapping: { query: "state.query" },
      output_mapping: { messages: "state.messages" },
      connection_binding: { connection_id: "storage-invoices" },
    });
  });

  it("restores the extension identity for an exact saved extension connector", () => {
    const editable = editableOverrideForRawStep(
      {
        step_kind: "connector",
        connector_id: "extension.fixture-storage",
        connector_install_id: "install-fixture",
        override: {
          connector: {
            operation_id: "search_storage_files",
            connection_binding: { connection_id: "storage-stock" },
          },
        },
      },
      "connector",
    );

    expect(editable.connector).toMatchObject({
      connector_id: "extension.fixture-storage",
      connector_install_id: "install-fixture",
      extension_id: "fixture-storage",
      operation_id: "search_storage_files",
    });
  });
});

describe("editableOverrideForRawStep http_request backfill (BUG-4)", () => {
  it("backfills required http_request defaults for a step persisted with an empty override", () => {
    const result = editableOverrideForRawStep(
      { step_kind: "http_request", override: {} },
      "http_request",
    );
    const http = result.http_request as Record<string, unknown>;
    expect(http.method).toBe("GET");
    expect(http.timeout_seconds).toBe(30);
    expect(http.response_target).toBeDefined();
    expect(http.expected_statuses).toEqual([200]);
  });

  it("preserves stored http_request values and only fills missing keys", () => {
    const result = editableOverrideForRawStep(
      {
        step_kind: "http_request",
        override: {
          http_request: {
            url: "https://example.test/items",
            method: "POST",
            expected_statuses: [201, 202],
          },
        },
      },
      "http_request",
    );
    const http = result.http_request as Record<string, unknown>;
    // Stored values win.
    expect(http.url).toBe("https://example.test/items");
    expect(http.method).toBe("POST");
    expect(http.expected_statuses).toEqual([201, 202]);
    // Missing required key still backfilled so save passes validation.
    expect(http.timeout_seconds).toBe(30);
  });

  it("does not interpret legacy body aliases as the canonical body template", () => {
    const result = editableOverrideForRawStep(
      {
        step_kind: "http_request",
        override: {
          http_request: {
            body: "legacy text",
            json: { sku: "legacy-json" },
          },
        },
      },
      "http_request",
    );
    const http = result.http_request as Record<string, unknown>;

    expect(http.body_template).toBe("");
    expect(http.body).toBe("legacy text");
    expect(http.json).toEqual({ sku: "legacy-json" });
  });
});

describe("defaultOverrideForStepKind", () => {
  it("keeps connector identity out of the default runtime override", () => {
    const parsed = JSON.parse(defaultOverrideForStepKind("connector"));

    expect(parsed.connector).toEqual({
      operation_id: "",
      input_mapping: {},
      connection_binding: {},
    });
  });

  it("returns only the canonical http_request contract keys", () => {
    const parsed = JSON.parse(defaultOverrideForStepKind("http_request"));
    expect(parsed.http_request).toEqual({
      auth_mode: "none",
      auth_ref: "",
      body_template: "",
      expected_statuses: [200],
      headers: {},
      max_response_bytes: 5 * 1024 * 1024,
      method: "GET",
      query_params: {},
      response_mode: "inline",
      response_target: "",
      timeout_seconds: 30,
      url: "",
    });
  });

  it("does not rewrite legacy http_request body aliases when a step is saved", () => {
    expect(
      overrideForStepSave(
        {
          http_request: {
            body: "legacy body",
            json: { canonical_precedence: true },
            method: "POST",
            response_target: "state.response",
            url: "https://example.test/items",
          },
        },
        "http_request",
      ),
    ).toEqual({
      http_request: {
        body: "legacy body",
        json: { canonical_precedence: true },
        method: "POST",
        response_target: "state.response",
        url: "https://example.test/items",
      },
    });
  });

  // R2-20: sample read paths looked like real values and failed on Apply.
  it.each(["transform", "file_from_base64", "template", "tabular_feed"])("starts a new %s with no sample input to read", (kind) => {
    const text = defaultOverrideForStepKind(kind);
    for (const sample of ["state.inputs.payload", "state.outputs.image_response", "generated.png", "state.outputs.header", "state.outputs.body", "state.feed_artifact", "{{ header }}"]) {
      expect(text).not.toContain(sample);
    }
  });

  it("returns a complete transform template", () => {
    const parsed = JSON.parse(defaultOverrideForStepKind("transform"));
    expect(parsed.transform).toMatchObject({
      input: { source: "" },
      mode: "object",
      result_target: "state.transform_result",
      output_mode: "mapped_only",
    });
    expect(parsed.transform.fields).toEqual([
      { from: "", to: "", type: "text", actions: [] },
    ]);
  });

  it("returns an empty string for agent steps", () => {
    expect(defaultOverrideForStepKind("agent")).toBe("");
  });
});

describe("loop controls of a sequence For each", () => {
  it("starts a Collect as an append with no target, so the author names an existing variable", () => {
    expect(JSON.parse(defaultOverrideForStepKind("collect"))).toEqual({
      collect: { target: "", mode: "append", source: "" },
    });
  });

  it.each(["break", "continue"])("gives %s no settings and drops the previous kind's block", (kind) => {
    expect(defaultOverrideForStepKind(kind)).toBe("");
    expect(overrideDraftForStepKindChange(defaultOverrideForStepKind("collect"), kind)).toBe("");
  });
});

describe("AC17-R65: platform-owned connector inputs never reach the step author", () => {
  // The owner could not have assembled this workflow. The decisive field of a store extension step was
  // a free-text repeater whose key had to be guessed as exactly `connection_id`, and the same
  // opaque conn_... had to be typed a second time as a `connection_ref` literal. The runtime
  // resolves and injects connection_ref itself
  // (core/application/orchestration/connector_step_runtime.py:2053), spreading the author's
  // mapping first, so the typed value was overwritten before the extension ever saw it.

  it("omits a declared connection_ref from the editable parameters", () => {
    const rows = connectorInputMappingRows({}, [
      { name: "connection_ref", required: true },
      { name: "first", required: true },
    ]);

    expect(rows.map((row) => row.key)).toEqual(["first"]);
  });

  it("omits a connection_ref that a previously saved step already carries", () => {
    const rows = connectorInputMappingRows(
      { connection_ref: "conn_f216787108067548", first: "state.first" },
      [{ name: "first", required: true }],
    );

    expect(rows.map((row) => row.key)).toEqual(["first"]);
  });

  it("strips it from what gets persisted, so exports stop carrying an install-scoped id", () => {
    expect(
      withoutPlatformOwnedConnectorInputs({
        connection_ref: "conn_f216787108067548",
        first: "state.first",
      }),
    ).toEqual({ first: "state.first" });
  });

  it("leaves the author's own inputs alone", () => {
    expect(isPlatformOwnedConnectorInput("first")).toBe(false);
    expect(isPlatformOwnedConnectorInput("after")).toBe(false);
    expect(isPlatformOwnedConnectorInput("connection_ref")).toBe(true);
  });
});

describe("the If condition read back after an edit", () => {
  // The editor writes a structured condition on the first keystroke. It was read back
  // as empty -- textOrEmpty() of an object is "" -- so the selector text vanished
  // (PR #61 hid it behind an uncontrolled input, and lost the suggestions).
  it("keeps a structured condition's selector", () => {
    expect(
      ifConditionDraftFromUnknown({ selector: "state.inputs.city.", operator: "truthy" }),
    ).toMatchObject({ selector: "state.inputs.city.", operator: "truthy" });
  });

  it("still reads an empty or blank condition as empty", () => {
    expect(ifConditionDraftFromUnknown("  ").selector).toBe("");
    expect(ifConditionDraftFromUnknown({}).selector).toBe("");
  });
});

describe("switching a step's kind", () => {
  it("drops the previous kind's block so an agent step can be saved", () => {
    // "Add step" creates a set_variable step; switching it to an agent used to
    // keep set_variable and the save failed: "Unknown step override key".
    const draft = JSON.stringify({ set_variable: { assignments: { a: 1 } }, extra_prompt: "Be brief." });

    expect(JSON.parse(overrideDraftForStepKindChange(draft, "agent"))).toEqual({ extra_prompt: "Be brief." });
  });

  it("gives the new kind its default block and keeps cross-cutting keys", () => {
    const draft = JSON.stringify({ set_variable: { assignments: {} }, add_tools: ["lookup"] });

    expect(JSON.parse(overrideDraftForStepKindChange(draft, "if"))).toEqual({
      ...JSON.parse(defaultOverrideForStepKind("if")),
      add_tools: ["lookup"],
    });
  });

  it("keeps the new kind's own block when it is already there", () => {
    const draft = JSON.stringify({ if: { on_true: "next" }, switch: { cases: {} } });

    expect(JSON.parse(overrideDraftForStepKindChange(draft, "if"))).toEqual({ if: { on_true: "next" } });
  });

  it("leaves an agent step with nothing to configure empty", () => {
    expect(overrideDraftForStepKindChange(JSON.stringify({ set_variable: {} }), "agent")).toBe("");
    expect(overrideDraftForStepKindChange("not json", "agent")).toBe("");
  });
});

describe("routing kinds keep their targets out of the step config", () => {
  it("gives if, switch, approval and wait defaults without any branch target", () => {
    const ifConfig = JSON.parse(defaultOverrideForStepKind("if")).if;
    expect(ifConfig).toEqual({ condition: { selector: "", operator: "equals", value: "" } });
    const switchConfig = JSON.parse(defaultOverrideForStepKind("switch")).switch;
    // A case is a port with a stable case_id; its target is an execution graph edge.
    expect(switchConfig).toEqual({ selector: "", cases: [{ case_id: "case_1", when: "" }] });
    expect(defaultOverrideForStepKind("switch")).not.toContain("task.mode");
    expect(JSON.parse(defaultOverrideForStepKind("approval"))).toEqual({
      approval: { message: "", show_outputs: [] },
    });
    expect(JSON.parse(defaultOverrideForStepKind("wait"))).toEqual({ wait: { duration_ms: 60000 } });
  });

  it("keeps a switch case id stable while its match value is edited", async () => {
    const { nextSwitchCaseId, switchCaseRows, switchCasesFromRows } = await import(
      "@/pages/workflows/detail/detail-step-editing"
    );
    const rows = switchCaseRows([{ case_id: "vip", when: "gold" }, { case_id: "case_2", when: "silver", label: "Silver" }]);
    expect(rows).toEqual([
      { case_id: "vip", when: "gold", label: "" },
      { case_id: "case_2", when: "silver", label: "Silver" },
    ]);
    const edited = rows.map((row) => (row.case_id === "vip" ? { ...row, when: "platinum" } : row));
    expect(switchCasesFromRows(edited)).toEqual([
      { case_id: "vip", when: "platinum" },
      { case_id: "case_2", when: "silver", label: "Silver" },
    ]);
    expect(nextSwitchCaseId(rows)).toBe("case_3");
  });
});


describe("HTTP response-limit preservation across editable backfill", () => {
  it.each([
    { mode: "inline", config: {}, expected: 5 * 1024 * 1024 },
    { mode: "", config: {}, expected: 5 * 1024 * 1024 },
    { mode: "artifact", config: {}, expected: 100 * 1024 * 1024 },
    { mode: " ARTIFACT ", config: {}, expected: 100 * 1024 * 1024 },
    { mode: "inline", config: { max_response_bytes: null }, expected: null },
    { mode: "artifact", config: { max_response_bytes: null }, expected: null },
    { mode: "inline", config: { max_response_bytes: "" }, expected: "" },
    { mode: "artifact", config: { max_response_bytes: "" }, expected: "" },
    { mode: "inline", config: { max_response_bytes: 4096 }, expected: 4096 },
    { mode: "artifact", config: { max_response_bytes: 12 * 1024 * 1024 }, expected: 12 * 1024 * 1024 },
  ])("uses the $mode omitted default without replacing explicit $expected", ({ mode, config, expected }) => {
    const http = {
      response_mode: mode,
      artifact_filename: "report-{{ state.inputs.invoice_id }}.json",
      method: { source: "state.inputs.method" },
      headers: { "X-Flag": "false", "X-Trace": { source: "state.inputs.trace" } },
      query_params: { reference: "001" },
      body_template: { source: "state.inputs.payload" },
      ...config,
    };
    const raw = { step_kind: "http_request", override: { http_request: http, optional: true } };
    const before = structuredClone(raw);
    const editable = editableOverrideForRawStep(raw);
    const saved = overrideForStepSave(editable, "http_request");
    expect(saved.http_request).toEqual({
      ...JSON.parse(defaultOverrideForStepKind("http_request")).http_request,
      ...http,
      max_response_bytes: expected,
    });
    expect(saved.optional).toBe(true);
    expect(raw).toEqual(before);
    expect(editableOverrideForRawStep({ ...raw, override: saved })).toEqual(saved);
  });

  it("keeps the new inline five-MiB draft explicit", () => {
    const override = JSON.parse(defaultOverrideForStepKind("http_request"));
    expect(override.http_request.response_mode).toBe("inline");
    expect(override.http_request.max_response_bytes).toBe(5 * 1024 * 1024);
    expect(editableOverrideForRawStep({ step_kind: "http_request", override }).http_request).toEqual(override.http_request);
  });
});


// C1/C2: these call existing public helpers, so the donor fails on data, not a missing import.
describe("Connector mapping presence and imported input alias preservation", () => {
  const parameters = [
    { name: "query", required: true }, { name: "filter", required: false },
    { name: "note", required: false }, { name: "zero", required: false },
    { name: "flag", required: false }, { name: "encoded_null", required: false },
  ];
  it("keeps present nullable and blank siblings when only query changes", () => {
    const initial = { query: "state.query", filter: null, note: "", zero: 0, flag: false, encoded_null: "literal:null" };
    const rows = connectorInputMappingRows(initial, parameters);
    const next = rows.map((row) => row.key === "query" ? { ...row, value: "state.changed" } : row);
    const result = connectorInputMappingFromRows(next);
    expect(result).toEqual({ ...initial, query: "state.changed", zero: "literal:0", flag: "literal:false" });
    expect(Object.hasOwn(result, "filter")).toBe(true);
    expect(Object.hasOwn(result, "note")).toBe(true);
    expect(JSON.stringify(result)).not.toContain("retainedValue");
    expect(initial).toEqual({ query: "state.query", filter: null, note: "", zero: 0, flag: false, encoded_null: "literal:null" });
  });
  it("does not turn absent optional inputs into present null or blank values", () => {
    expect(connectorInputMappingFromRows(connectorInputMappingRows({ query: "state.query" }, parameters))).toEqual({ query: "state.query" });
    expect(connectorInputMappingFromRows(connectorInputMappingRows({}, parameters))).toEqual({});
    expect(connectorInputMappingFromRows(connectorInputMappingRows({ empty: "literal:", nullable: "literal:null" }, []))).toEqual({ empty: "literal:", nullable: "literal:null" });
  });
  it("creates own mapping keys without changing the object prototype", () => {
    const mapping = JSON.parse('{"__proto__":null,"note":""}') as Record<string, unknown>;
    const result = connectorInputMappingFromRows(connectorInputMappingRows(mapping, []));
    expect(Object.getPrototypeOf(result)).toBe(Object.prototype);
    expect(Object.hasOwn(result, "__proto__")).toBe(true);
    expect(result["__proto__"]).toBeNull();
    expect(result.note).toBe("");
  });
  it.each([
    { top: { query: "literal:column" }, alias: { query: "literal:alias" }, canonical: { query: "literal:canonical" }, expected: { query: "literal:column" } },
    { top: {}, alias: { query: "literal:alias" }, canonical: { query: "literal:canonical" }, expected: { query: "literal:alias" } },
    { top: {}, alias: {}, canonical: { query: "literal:canonical" }, expected: { query: "literal:canonical" } },
    { top: {}, alias: {}, canonical: {}, expected: {} },
  ])("hydrates the runtime input precedence $expected.query and consumes its alias", ({ top, alias, canonical, expected }) => {
    const input = { step_kind: "connector", input_mapping_jsonb: top, override: { connector: { operation_id: "send_message", input_mapping_jsonb: alias, input_mapping: canonical, output_mapping_jsonb: { result: "state.result" } } } };
    const editable = editableOverrideForRawStep(input);
    expect(editable.connector).toMatchObject({ input_mapping: expected, output_mapping_jsonb: { result: "state.result" } });
    expect(editable.connector).not.toHaveProperty("input_mapping_jsonb");
    expect(input.override.connector.input_mapping_jsonb).toEqual(alias);
  });
  it("retains the legacy top-level mapping only after every runtime source is empty", () => {
    const editable = editableOverrideForRawStep({ step_kind: "connector", input_mapping: { query: "literal:legacy" }, override: { connector: {} } });
    expect(editable.connector).toMatchObject({ input_mapping: { query: "literal:legacy" } });
  });
  it("saves an own empty canonical mapping instead of resurrecting a retained alias", () => {
    const override = { connector: { connector_id: "extension.fixture.mail", input_mapping: {}, input_mapping_jsonb: { query: "literal:old" }, output_mapping_jsonb: { result: "state.result" }, connection_binding: { connection_id: "mail-1" } } };
    expect(overrideForStepSave(override, "connector").connector).toEqual({ input_mapping: {}, output_mapping_jsonb: { result: "state.result" }, connection_binding: { connection_id: "mail-1" } });
    expect(override.connector.input_mapping_jsonb).toEqual({ query: "literal:old" });
  });
  it("migrates an Advanced alias-only mapping without exposing platform-owned input", () => {
    const result = overrideForStepSave({ connector: { input_mapping_jsonb: { query: "literal:old", connection_ref: "literal:internal" } } }, "connector");
    expect(result).toEqual({ connector: { input_mapping: { query: "literal:old" } } });
  });
});
