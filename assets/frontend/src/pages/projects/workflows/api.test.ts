import {
  fetchProjectWorkflowBindings,
  normalizeWorkflowBindingsSavePayload,
  saveProjectWorkflowBindings,
} from "@/pages/projects/workflows/api";

describe("project workflows api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("normalizes workflow bindings payload before save", () => {
    expect(
      normalizeWorkflowBindingsSavePayload({
        workflow_ids: ["wf_one", "wf_two"],
        operator_default_workflow_id: "wf_two",
      }),
    ).toEqual({
      workflow_ids: ["wf_one", "wf_two"],
      operator_default_workflow_id: "wf_two",
    });

    expect(
      normalizeWorkflowBindingsSavePayload({
        workflow_ids: ["wf_alpha"],
        operator_default_workflow_id: "",
      }),
    ).toEqual({
      workflow_ids: ["wf_alpha"],
      operator_default_workflow_id: "",
    });
  });

  it("leaves the default out when the caller does not set it", () => {
    expect(normalizeWorkflowBindingsSavePayload({ workflow_ids: ["wf_one"] })).toEqual({
      workflow_ids: ["wf_one"],
    });
  });

  it("calls workflow bindings endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ project_id: "7", workflow_ids: ["wf_a"], operator_default_workflow_id: "wf_a", project_workflows: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(fetchProjectWorkflowBindings("7")).resolves.toEqual({
      project_id: "7",
      workflow_ids: ["wf_a"],
      operator_default_workflow_id: "wf_a",
      project_workflows: [],
    });

    await expect(
      saveProjectWorkflowBindings("7", {
        workflow_ids: ["wf_a"],
        operator_default_workflow_id: "wf_a",
      }),
    ).resolves.toEqual({ status: "ok" });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/projects/7/workflows");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/projects/7/workflows");
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe("PUT");
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe(
      JSON.stringify({
        workflow_ids: ["wf_a"],
        operator_default_workflow_id: "wf_a",
      }),
    );
  });
});
