import { describe, expect, it } from "vitest";

import {
  createWorkflowInterfaceFieldDraft,
  fieldDraftFromContract,
  httpRequestBodyEditorText,
  workflowFieldPayload,
  workflowOutputFieldPayload,
} from "@/pages/workflows/detail/detail-interface";

const SECRET = "SHOULD-NEVER-REACH-THE-BROWSER-5512";

describe("workflow input defaults in the editor", () => {
  it("saves a configured default alongside the field", () => {
    const [payload] = workflowFieldPayload([
      createWorkflowInterfaceFieldDraft({
        name: "region",
        type: "string",
        default: "eu-west-1",
        defaultConfigured: true,
      }),
    ]);

    expect(payload.default).toBe("eu-west-1");
  });

  it("keeps an explicitly configured empty string", () => {
    const [payload] = workflowFieldPayload([
      createWorkflowInterfaceFieldDraft({
        name: "prefix",
        type: "string",
        default: "",
        defaultConfigured: true,
      }),
    ]);

    expect(payload).toHaveProperty("default", "");
  });

  it("omits the key entirely when no default is configured", () => {
    // `default: null` would read as a configured null on the way back in.
    const [payload] = workflowFieldPayload([
      createWorkflowInterfaceFieldDraft({ name: "region", type: "string" }),
    ]);

    expect("default" in payload).toBe(false);
  });

  it("keeps a boolean default of false", () => {
    // The falsy trap: clearing a checkbox is a value, not the absence of one.
    const [payload] = workflowFieldPayload([
      createWorkflowInterfaceFieldDraft({
        name: "dry_run",
        type: "boolean",
        default: false,
        defaultConfigured: true,
      }),
    ]);

    expect(payload.default).toBe(false);
  });

  it("sends a number default as a number", () => {
    const [payload] = workflowFieldPayload([
      createWorkflowInterfaceFieldDraft({
        name: "retries",
        type: "number",
        default: "3",
        defaultConfigured: true,
      }),
    ]);

    expect(payload.default).toBe(3);
  });

  it("sends an object default as parsed JSON", () => {
    const [payload] = workflowFieldPayload([
      createWorkflowInterfaceFieldDraft({
        name: "headers",
        type: "object",
        default: '{"x-trace":"on"}',
        defaultConfigured: true,
      }),
    ]);

    expect(payload.default).toEqual({ "x-trace": "on" });
  });

  it("rejects an unparseable configured object instead of silently clearing it", () => {
    // Sending the raw string would store a default the workflow can never use,
    // and the failure would surface at a step rather than in the editor.
    expect(() =>
      workflowFieldPayload([
        createWorkflowInterfaceFieldDraft({
          name: "headers",
          type: "object",
          default: "{not json",
          defaultConfigured: true,
        }),
      ]),
    ).toThrow(/headers.*valid object/i);
  });

  it("rejects a blank configured number instead of coercing it to zero", () => {
    expect(() =>
      workflowFieldPayload([
        createWorkflowInterfaceFieldDraft({
          name: "retries",
          type: "number",
          default: "",
          defaultConfigured: true,
        }),
      ]),
    ).toThrow(/retries.*valid number/i);
  });

  it("rejects a configured select value outside its options", () => {
    expect(() =>
      workflowFieldPayload([
        createWorkflowInterfaceFieldDraft({
          name: "tier",
          type: "select",
          options: "gold, silver",
          default: "",
          defaultConfigured: true,
        }),
      ]),
    ).toThrow(/tier.*declared option/i);
  });

  it("never sends a value for a secret field", () => {
    // A secret is set through its own write-only endpoint; the contract update
    // must not be a second way in.
    const [payload] = workflowFieldPayload([
      createWorkflowInterfaceFieldDraft({
        name: "login",
        type: "secret",
        default: SECRET,
        defaultConfigured: true,
      }),
    ]);

    expect(JSON.stringify(payload)).not.toContain(SECRET);
    expect("default" in payload).toBe(false);
  });
});

describe("reading a contract back into the editor", () => {
  it("restores an ordinary default so it can be changed", () => {
    const draft = fieldDraftFromContract({
      name: "region",
      type: "string",
      default: "eu-west-1",
      default_configured: true,
    });

    expect(draft.default).toBe("eu-west-1");
    expect(draft.defaultConfigured).toBe(true);
  });

  it("restores a secret as configured, with no value to show", () => {
    const draft = fieldDraftFromContract({
      name: "login",
      type: "secret",
      default_configured: true,
    });

    expect(draft.defaultConfigured).toBe(true);
    expect(draft.default).toBe("");
  });

  it("restores an object default as editable JSON text", () => {
    const draft = fieldDraftFromContract({
      name: "headers",
      type: "object",
      default: { "x-trace": "on" },
      default_configured: true,
    });

    expect(JSON.parse(String(draft.default))).toEqual({ "x-trace": "on" });
  });
});

describe("workflow outputs in the editor", () => {
  it("cannot serialize secret types or configured defaults", () => {
    const payload = workflowOutputFieldPayload([
      createWorkflowInterfaceFieldDraft({
        name: "credential",
        type: "secret",
        defaultConfigured: true,
      }),
      createWorkflowInterfaceFieldDraft({
        name: "region",
        type: "string",
        default: "eu-west-1",
        defaultConfigured: true,
      }),
    ]);

    expect(payload).toEqual([
      { name: "region", type: "string", required: false },
    ]);
  });
});

describe("HTTP request body editor text", () => {
  it("returns an empty draft for a circular object instead of '[object Object]'", () => {
    const circular: Record<string, unknown> = {};
    circular.self = circular;

    expect(httpRequestBodyEditorText(circular)).toBe("");
  });

  it("reads a file input's test file and never sends it or a default back", () => {
    const draft = fieldDraftFromContract({
      name: "invoice",
      type: "file",
      default_configured: false,
      sample_file: { artifact_id: "art_1", filename: "invoice.pdf", size_bytes: 2048 },
    });

    expect(draft.sampleFile).toEqual({
      filename: "invoice.pdf",
      content_type: "",
      size_bytes: 2048,
    });
    const [payload] = workflowFieldPayload([{ ...draft, defaultConfigured: true, default: "x" }]);
    expect(payload).toEqual({ name: "invoice", type: "file", required: false });
    expect(workflowOutputFieldPayload([draft])).toEqual([]);
  });
});


describe("workflow interface row names", () => {
  it.each(["input", "output"] as const)("rejects blank or whitespace-only %s names without mutating the draft", (kind) => {
    const serialize = kind === "output" ? workflowOutputFieldPayload : workflowFieldPayload;
    for (const name of ["", "  \t "]) {
      const draft = createWorkflowInterfaceFieldDraft({ name, type: "object", required: true, description: "Retain this row", default: '{"region":"eu"}', defaultConfigured: true });
      expect(() => serialize([draft])).toThrow(/name.*required/i);
      expect(draft).toMatchObject({ name, type: "object", required: true, description: "Retain this row", default: '{"region":"eu"}', defaultConfigured: true });
    }
  });

  it("keeps empty collections valid for an untouched editor or explicit removal", () => {
    expect(workflowFieldPayload([])).toEqual([]);
    expect(workflowOutputFieldPayload([])).toEqual([]);
  });
});
