import { canvasGraphFixture, fetchSpy, mockedFetchWorkflowCanvasGraph, mockedFetchWorkflowDetail, workflowDetailFixture } from "./index.test-helpers";
import type { WorkflowDetail } from "@/pages/workflows/shared/api";

export const input = { request: "state.inputs.request", flag: false, zero: 0, nullable: null, text: "false", code: "001", blank: "", object: { nested: null }, list: [false, 0, null] };
export const output = { result: "state.result", summary: "state.summary" };
const participants = [{ participant_key: "writer", agent_id: "writer" }, { participant_key: "reviewer", agent_id: "reviewer" }];
const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { "content-type": "application/json" } });
export function install(patch: Record<string, unknown> = {}) {
  let row: Record<string, unknown> = { step_id: "agent_a", display_name: "Agent A", step_kind: "agent", agent_id: "writer", participant_key: "", block_key: "legacy-preview", input_mapping_jsonb: structuredClone(input), output_mapping_jsonb: structuredClone(output), override: { extra_prompt: "Keep me", agent: { input_mapping: { request: "state.shadowed" } } }, ...patch };
  let revision = 7;
  let held: (() => void) | undefined;
  let gate: Promise<void> | undefined;
  let saveGate: Promise<void> | undefined;
  let releaseSave: (() => void) | undefined;
  const reads: unknown[] = [];
  let readStarted = 0;
  const detail = () => { const base: WorkflowDetail = workflowDetailFixture(); return { ...base, enabled: false, revision, settings: { ...base.settings, participants }, steps: [structuredClone(row)] }; };
  mockedFetchWorkflowDetail.mockImplementation(async () => {
    readStarted += 1;
    if (gate) {
      await gate;
    }
    const value = detail();
    reads.push(value);
    return value;
  });
  mockedFetchWorkflowCanvasGraph.mockImplementation(async () => canvasGraphFixture({ enabled: false, entry: { kind: "step", id: "agent_a" }, steps: [{ id: "agent_a", step_id: "agent_a", kind: "agent", display_name: row.display_name, ports: [{ outcome: { name: "success" }, label: "Next" }] }] }));
  const ordinary = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (url, init) => {
    if (String(url).startsWith("/api/agents")) return response([{ agent_id: "writer", display_name: "Writer" }, { agent_id: "reviewer", display_name: "Reviewer" }]);
    if (String(url).startsWith("/api/published-blocks")) return response([{ block_key: "block.a", display_name: "Block A", source_kind: "agent", source_ref: "writer", enabled: true }, { block_key: "block.b", display_name: "Block B", source_kind: "agent", source_ref: "writer", enabled: true }]);
    if (url === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      if (body.expected_revision !== revision || body.operations.length !== 1 || body.operations[0].op !== "update_step_config") throw new Error("Expected one actual revision-guarded patch");
      if (saveGate) await saveGate;
      row = { ...row, ...structuredClone(body.operations[0].patch) }; revision += 1;
      return response({ revision, applied: true, valid: false, issues: [], workflow: {} });
    }
    if (!ordinary) throw new Error("Missing transport fixture");
    return ordinary(url, init);
  });
  return { holdSave() { saveGate = new Promise<void>((resolve) => { releaseSave = resolve; }); }, acceptSave() { releaseSave?.(); saveGate = undefined; }, row: () => structuredClone(row), reads, started: () => readStarted, holdRead() { row.display_name = "Server Agent A"; row.output_mapping_jsonb = { result: "state.changed", summary: "state.server_summary" }; revision += 1; gate = new Promise<void>((resolve) => { held = resolve; }); }, release() { held?.(); gate = undefined; } };
}
