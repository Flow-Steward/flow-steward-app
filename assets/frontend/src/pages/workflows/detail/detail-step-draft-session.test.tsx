import { renderHook, waitFor } from "@testing-library/react";
import { STEP_DRAFT_KEY, stepDraftRestore, useStepDraftSession, type StepEditorValues } from "./detail-step-draft-session";
import { storableFormDraft } from "./workflow-edit-session";

/** A control credential typed into the step's parameters. */
const TYPED_TOKEN = "typed-control-token"; // pragma: allowlist secret

const values: StepEditorValues = {
  stepId: "fetch", displayName: "Fetch orders", kind: "http_request",
  overrideDraft: JSON.stringify({ http_request: { url: "https://example.test/orders" } }),
  agentId: "", optional: false, blockKey: "",
};

describe("the step editor's draft in the edit session", () => {
  it("restores the parameters, unless a credential had to be left out", () => {
    const plain = stepDraftRestore(storableFormDraft({ ...values, overrideDraft: undefined, override: { http_request: { url: "u" } } }));
    expect(plain?.values.overrideDraft && JSON.parse(plain.values.overrideDraft)).toEqual({ http_request: { url: "u" } });
    const withToken = stepDraftRestore(storableFormDraft({ stepId: "fetch", override: { http_request: { headers: { Authorization: TYPED_TOKEN } } } }));
    expect(withToken?.values.overrideDraft).toBeNull();
    expect(withToken?.reentry).toEqual(["override.http_request.headers.Authorization"]);
    expect(JSON.stringify(withToken)).not.toContain(TYPED_TOKEN);
    expect(stepDraftRestore(storableFormDraft({ name: "settings" }))).toBeNull();
  });

  it("reopens the step's editor with the recovered values, then keeps them while unsaved", async () => {
    const stored = storableFormDraft({ stepId: "fetch", displayName: "Recovered", kind: "http_request", override: { http_request: { url: "u" } }, agentId: "", optional: true, blockKey: "" });
    const setFormDraft = vi.fn();
    const openStep = vi.fn(async () => true);
    const apply = vi.fn();
    const view = renderHook(({ unsaved, current }) => useStepDraftSession({
      session: { status: "active", formDrafts: { [STEP_DRAFT_KEY]: stored }, setFormDraft },
      enabled: true, values: current, unsaved, stepExists: (id) => id === "fetch", openStep, apply,
    }), { initialProps: { unsaved: false, current: values } });
    await waitFor(() => expect(view.result.current.restored).toBe(true));
    expect(openStep).toHaveBeenCalledWith("fetch");
    expect(apply).toHaveBeenCalledWith(expect.objectContaining({ values: expect.objectContaining({ displayName: "Recovered", optional: true }) }));
    // The restore runs first: a clean editor at load does not erase the recovered draft before it.
    expect(setFormDraft.mock.invocationCallOrder[0]).toBeGreaterThan(openStep.mock.invocationCallOrder[0]);

    view.rerender({ unsaved: true, current: { ...values, overrideDraft: JSON.stringify({ http_request: { headers: { token: TYPED_TOKEN } } }) } });
    const [key, draft, sensitive] = setFormDraft.mock.calls.at(-1)!;
    expect(key).toBe(STEP_DRAFT_KEY);
    expect(sensitive).toEqual([]);
    expect(storableFormDraft(draft, sensitive).reentry).toEqual(["override.http_request.headers.token"]);
    view.rerender({ unsaved: true, current: { ...values, overrideDraft: "{ not json" } });
    expect(setFormDraft.mock.calls.at(-1)?.[2]).toEqual(["override"]);
    view.rerender({ unsaved: false, current: values });
    expect(setFormDraft).toHaveBeenLastCalledWith(STEP_DRAFT_KEY, null);
  });
});
