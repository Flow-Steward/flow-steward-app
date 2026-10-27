import { applyWorkflowEditSession, compareWorkflowEditSession } from "./workflow-edit-session-api";
import type { WorkflowAuthoringContent } from "./workflow-edit-session";

const content = { settings: {}, steps: [], groups: [], for_each: [] } as WorkflowAuthoringContent;
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
const comparison = { changes: [], summary: { total: 0, added: 0, removed: 0, changed: 0 } };

afterEach(() => vi.restoreAllMocks());

describe("the edit session API", () => {
  it.each([undefined, 0, -1, 1.5, Number.NaN])("refuses to send a copy without a reviewed revision (%s)", async (revision) => {
    const transport = vi.spyOn(globalThis, "fetch");
    await expect(applyWorkflowEditSession("wf", { content, expected_revision: revision as number })).rejects.toThrow(/revision/i);
    expect(transport).not.toHaveBeenCalled();
  });

  it("sends exactly the reviewed copy and revision once", async () => {
    const transport = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ workflow_id: "wf", revision: 14, applied: true, valid: true, issues: [], blocking_issues: [] }));
    const result = await applyWorkflowEditSession("wf/x", { content, expected_revision: 13 });
    expect(result.revision).toBe(14);
    expect(transport).toHaveBeenCalledTimes(1);
    expect(String(transport.mock.calls[0][0])).toBe("/api/workflows/wf%2Fx/edit-session/apply");
    expect(JSON.parse(String(transport.mock.calls[0][1]?.body))).toEqual({ content, expected_revision: 13 });
  });

  it("compares without writing and requires the server to name its revision", async () => {
    const transport = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({
      workflow_id: "wf", current_revision: 13, current_content: {}, base_to_local: comparison, base_to_server: comparison, local_to_server: comparison,
    }));
    const result = await compareWorkflowEditSession("wf", { base_content: content, local_content: content });
    expect(result.current_revision).toBe(13);
    expect(String(transport.mock.calls[0][0])).toBe("/api/workflows/wf/edit-session/compare");
    transport.mockResolvedValueOnce(response({ workflow_id: "wf", current_revision: null }));
    await expect(compareWorkflowEditSession("wf", { base_content: content, local_content: content })).rejects.toThrow(/revision/i);
  });
});
