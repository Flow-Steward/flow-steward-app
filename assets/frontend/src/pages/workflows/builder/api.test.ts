import {
  applyWorkflowChanges,
  edgeKey,
  isWorkflowRevisionConflict,
  outcomeFromHandleId,
  outcomeHandleId,
  sameNodeRef,
  workflowChangeErrorIssues,
} from "@/pages/workflows/builder/api";

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("workflows builder change api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends one change set with the revision it was made on", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ workflow_id: "wf-alpha", revision: 8, valid: true, issues: [], workflow: {} }),
    );

    const result = await applyWorkflowChanges("wf-alpha", {
      expected_revision: 7,
      operations: [
        { op: "create_step", step: { step_id: "draft", step_kind: "agent", group_id: null, lane_id: null } },
        {
          op: "connect",
          edge: {
            source: { kind: "step", id: "draft" },
            outcome: { name: "success" },
            target: { kind: "step", id: "finish" },
          },
        },
      ],
    });

    expect(result.revision).toBe(8);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/workflows/wf-alpha/changes");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      expected_revision: 7,
      operations: [
        { op: "create_step", step: { step_id: "draft", step_kind: "agent", group_id: null, lane_id: null } },
        {
          op: "connect",
          edge: {
            source: { kind: "step", id: "draft" },
            outcome: { name: "success" },
            target: { kind: "step", id: "finish" },
          },
        },
      ],
    });
  });

  it("recognises the revision conflict and keeps it apart from a refused set", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            code: "workflow_revision_conflict",
            message: "The workflow changed after it was loaded. Reload before saving.",
            workflow_id: "wf-alpha",
            expected_revision: 7,
          },
        },
        409,
      ),
    );
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            error: "validation_failed",
            issues: [{ code: "workflow.change.outcome_taken", path: "operations[0].edge.outcome", message: "Taken" }],
          },
        },
        400,
      ),
    );

    const conflict = await applyWorkflowChanges("wf-alpha", { expected_revision: 7, operations: [] }).catch(
      (error: unknown) => error,
    );
    expect(isWorkflowRevisionConflict(conflict)).toBe(true);
    expect(workflowChangeErrorIssues(conflict)).toEqual([]);

    const refused = await applyWorkflowChanges("wf-alpha", { expected_revision: 7, operations: [] }).catch(
      (error: unknown) => error,
    );
    expect(isWorkflowRevisionConflict(refused)).toBe(false);
    expect(workflowChangeErrorIssues(refused)).toEqual([
      { code: "workflow.change.outcome_taken", path: "operations[0].edge.outcome", message: "Taken" },
    ]);
  });

  it("names every port by its outcome, a switch case by its stable case_id", () => {
    expect(outcomeHandleId({ name: "success" })).toBe("success");
    expect(outcomeHandleId({ name: "case", case_id: "case_2" })).toBe("case:case_2");
    expect(outcomeFromHandleId("case:case_2")).toEqual({ name: "case", case_id: "case_2" });
    expect(outcomeFromHandleId("approved")).toEqual({ name: "approved" });
    // A handle that is not a named outcome never becomes a default "first exit".
    expect(outcomeFromHandleId("out")).toBeNull();
    expect(outcomeFromHandleId("case:")).toBeNull();
    expect(
      edgeKey({ source: { kind: "group", id: "loop" }, outcome: { name: "error" } }),
    ).toBe("group:loop:error");
  });

  it("compares group exits by group and lane", () => {
    expect(
      sameNodeRef(
        { kind: "group_exit", group_id: "fanout", lane_id: "a" },
        { kind: "group_exit", group_id: "fanout", lane_id: "a" },
      ),
    ).toBe(true);
    expect(
      sameNodeRef(
        { kind: "group_exit", group_id: "fanout", lane_id: "a" },
        { kind: "group_exit", group_id: "fanout", lane_id: "b" },
      ),
    ).toBe(false);
    expect(sameNodeRef({ kind: "step", id: "a" }, { kind: "group", id: "a" })).toBe(false);
  });
});
