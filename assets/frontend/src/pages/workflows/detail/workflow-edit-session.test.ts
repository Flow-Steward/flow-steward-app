import { appendGraphIntent, emptyGraphJournal } from "@/pages/workflows/builder/canvas-history";
import { definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import {
  authoringContentOf,
  buildEditSession,
  contentWithDefinition,
  newEditSessionId,
  parseStoredEditSession,
  sessionHasLocalWork,
  storableFormDraft,
  WORKFLOW_EDIT_SESSION_SCHEMA_VERSION,
} from "./workflow-edit-session";

/** A control credential: it must never reach the browser's database. */
const TYPED_PASSWORD = "typed-control-password"; // pragma: allowlist secret

const identity = { accountId: "account-a", userId: "user-a", workflowId: "wf" };
const workflow = {
  workflow_id: "wf",
  revision: 12,
  enabled: true,
  source_install_id: "install-1",
  display_name: "Flow",
  description: "Described",
  steps: [{ step_id: "a", step_kind: "agent" }],
  groups: [],
  for_each: [],
  settings: {
    execution_graph: { schema_version: 1, entry: { kind: "step", id: "a" }, edges: [] },
    canvas_annotations: [{ id: "note", text: "kept by its own service" }],
    canvas_annotations_revision: 3,
    callable: { expected_inputs: [
      { name: "login", type: "secret", default_configured: true },
      { name: "sample", type: "file", sample_file: { artifact_id: "file-1" } },
    ] },
  },
};

function session(overrides: Record<string, unknown> = {}) {
  const content = authoringContentOf(workflow);
  return {
    ...buildEditSession({
      sessionId: "session-1",
      identity,
      editSequence: 3,
      origin: { revision: 12, content },
      serverCheckpoint: { revision: 12, content },
      journal: appendGraphIntent(emptyGraphJournal(), [{ op: "create_step", step: { step_id: "b", step_kind: "agent" } }], {
        intentToken: { controller: Symbol("toast"), serial: 1 },
      }),
      currentDefinition: definitionFromWorkflow(workflow),
      formDrafts: {},
    }),
    ...overrides,
  };
}

describe("the edit session record", () => {
  it.each(["missing", "throwing"] as const)("uses secure random bytes when randomUUID is %s", (mode) => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.set(Array.from({ length: 16 }, (_, index) => index));
      return bytes;
    });
    vi.stubGlobal("crypto", {
      randomUUID: mode === "missing" ? undefined : () => { throw new Error("randomUUID unavailable"); },
      getRandomValues,
    });
    try {
      expect(newEditSessionId()).toBe("session-000102030405060708090a0b0c0d0e0f");
      expect(getRandomValues).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("fails clearly when secure random bytes are unavailable", () => {
    vi.stubGlobal("crypto", undefined);
    try {
      expect(() => newEditSessionId()).toThrow("Secure random session IDs are unavailable");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps only the author's content: no enabled, provenance, notes or input authority", () => {
    const content = authoringContentOf(workflow);
    expect(Object.keys(content).sort()).toEqual(["description", "display_name", "for_each", "groups", "settings", "steps"]);
    expect(content.settings).not.toHaveProperty("canvas_annotations");
    expect(content.settings).not.toHaveProperty("canvas_annotations_revision");
    expect(JSON.stringify(content)).not.toMatch(/default_configured|sample_file|file-1/);
  });

  it("never stores files, functions or session-only tokens", () => {
    const content = authoringContentOf({ ...workflow, description: new Blob(["bytes"]), steps: [{ step_id: "a", run: () => undefined, upload: new File(["x"], "x.txt") }] });
    expect(content).not.toHaveProperty("description");
    expect(content.steps).toEqual([{ step_id: "a" }]);
    const record = session();
    expect(record.history.items[0]).not.toHaveProperty("intentToken");
    expect(() => structuredClone(record)).not.toThrow();
    expect(JSON.parse(JSON.stringify(record))).toEqual(record);
  });

  it("keeps the author's boundary rows, not the read projection of the valid ones", () => {
    const raw = [{ name: "ok", type: "string" }, { name: "", type: "unknown" }];
    const content = authoringContentOf({ ...workflow, steps: [{
      step_id: "a", step_kind: "agent", boundary_contract: { entry: [raw[0]] }, raw_expected_inputs: raw, expected_input_issues: [{ code: "x" }],
    }] });
    expect(content.steps).toEqual([{ step_id: "a", step_kind: "agent", boundary_contract: { entry: raw } }]);
  });

  it("puts the local definition into the checkpoint's content", () => {
    const local = definitionFromWorkflow({ ...workflow, steps: [...workflow.steps, { step_id: "b", step_kind: "agent" }] });
    const content = contentWithDefinition(authoringContentOf(workflow), local);
    expect((content.steps as Array<{ step_id: string }>).map((step) => step.step_id)).toEqual(["a", "b"]);
    expect(content.display_name).toBe("Flow");
  });

  it("keeps a form's credential fields only as 'enter again'", () => {
    const draft = storableFormDraft(
      { display_name: "Draft", headers: { Authorization: "Bearer x", accept: "json" }, password: TYPED_PASSWORD, secretDraft: TYPED_PASSWORD, note: "" },
      ["note"],
    );
    expect(JSON.stringify(draft)).not.toContain(TYPED_PASSWORD);
    expect(JSON.stringify(draft)).not.toContain("Bearer x");
    expect(draft.value).toEqual({ display_name: "Draft", headers: { accept: "json" } });
    expect(draft.reentry.sort()).toEqual(["headers.Authorization", "password", "secretDraft"]);
  });

  it("recovers only a whole record of this account, user and workflow", () => {
    const record = JSON.parse(JSON.stringify(session()));
    expect(parseStoredEditSession(record, identity)).toMatchObject({ status: "ok" });
    expect(parseStoredEditSession({ ...record, schemaVersion: WORKFLOW_EDIT_SESSION_SCHEMA_VERSION + 1 }, identity)).toEqual({ status: "unavailable", reason: "version" });
    expect(parseStoredEditSession("{not json", identity)).toEqual({ status: "unavailable", reason: "corrupt" });
    expect(parseStoredEditSession({ ...record, history: { ...record.history, cursor: 9 } }, identity)).toEqual({ status: "unavailable", reason: "corrupt" });
    expect(parseStoredEditSession({ ...record, serverCheckpoint: undefined }, identity)).toEqual({ status: "unavailable", reason: "checkpoint" });
    for (const other of [{ ...identity, userId: "user-b" }, { ...identity, accountId: "account-b" }, { ...identity, workflowId: "other" }]) {
      expect(parseStoredEditSession(record, other)).toEqual({ status: "unavailable", reason: "identity" });
    }
  });

  it("has local work while it holds history, unapplied changes or drafts", () => {
    expect(sessionHasLocalWork(session())).toBe(true);
    expect(sessionHasLocalWork({ history: emptyGraphJournal(), formDrafts: {} })).toBe(false);
    expect(sessionHasLocalWork({ history: emptyGraphJournal(), formDrafts: { step: { value: {}, reentry: [] } } })).toBe(true);
  });
});
