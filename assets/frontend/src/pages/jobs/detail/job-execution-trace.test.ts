import { describe, expect, it } from "vitest";
import { jobExecutionTrace, runtimeEdgeKey, type JobTraceTransition } from "./job-execution-trace";
import { runtimeEdgeMarks } from "@/pages/workflows/builder/runtime-edge-marks";
import { archivedDefinitionCanvasGraph } from "./job-definition-graph";
import type { JobDefinitionPayload } from "./api";

const group = { kind: "group" as const, id: "g", execution_id: "group-pass" };
const item = { kind: "for_each" as const, id: "items", execution_id: "items-pass", item_index: 0 };
const routes = [
  { source: { kind: "step" as const, id: "send" }, outcome: { name: "success" as const }, target: { kind: "for_each_exit" as const, for_each_id: "items", lane_id: "a" } },
  { source: { kind: "for_each" as const, id: "items" }, outcome: { name: "success" as const }, target: { kind: "group_exit" as const, group_id: "g", lane_id: null } },
  { source: { kind: "group" as const, id: "g" }, outcome: { name: "success" as const }, target: { kind: "step" as const, id: "done" } },
];
const definition: JobDefinitionPayload = { job_id: "run", available: true, workflow_id: "workflow", snapshot_id: "archive", revision: 3,
  digest: "digest", schema_version: 1, steps: [{ step_id: "send", step_kind: "transform", for_each_id: "items", lane_id: "a" }, { step_id: "done", step_kind: "stop_success" }],
  groups: [{ group_id: "g", body: { kind: "sequence", entry: { kind: "for_each", id: "items" } } }],
  for_each: [{ for_each_id: "items", group_id: "g", over: "state.items", item_name: "item", body: { kind: "lanes", lanes: [{ lane_id: "a", entry: { kind: "step", id: "send" } }] } }],
  execution_graph: { schema_version: 1, entry: { kind: "group", id: "g" }, edges: routes } };
const facts: JobTraceTransition[] = [
  { ...routes[0], id: "a".repeat(64), sequence: 1, step_execution_id: "send-0", attempt: 1, frames: [group, item], lane_id: "a" },
  { ...routes[0], id: "b".repeat(64), sequence: 2, step_execution_id: "send-1", attempt: 2, frames: [group, { ...item, item_index: 1 }], lane_id: "a" },
  { ...routes[1], id: "c".repeat(64), sequence: 3, container_execution_id: "items-pass", frames: [group, { ...item, item_index: 2 }] },
  { ...routes[2], id: "d".repeat(64), sequence: 4, container_execution_id: "group-pass", frames: [group] },
];
function page(transitions = facts) {
  return { job_id: "run", steps: [], page: {}, run_trace: { schema_version: 1, job_id: "run", workflow_id: "workflow", snapshot_id: "archive", revision: 3,
    digest: "digest", covered_from_start: true, terminal: false, truncated: false, last_sequence: transitions.length, transitions } };
}
describe("recorded edge scope and repetition", () => {
  it("retains actual frame, item, attempt and lane facts while coloring one authored edge per route", () => {
    const evidence = jobExecutionTrace("run", definition, page());
    expect(evidence?.trace.transitions).toEqual(facts);
    expect(evidence?.edges).toEqual([routes[0], routes[0], routes[1], routes[2]]);
    const graph = archivedDefinitionCanvasGraph(definition)!;
    expect([...runtimeEdgeMarks(evidence, graph)]).toEqual(routes.map(runtimeEdgeKey));
    expect([...runtimeEdgeMarks(evidence, { ...graph, workflow: { ...graph.workflow, revision: 4 } })]).toEqual([]);
    expect([...runtimeEdgeMarks(evidence, { ...graph, edges: [{ ...routes[0], target: { kind: "for_each_exit", for_each_id: "items", lane_id: "b" } }] })]).toEqual([]);
  });
  it.each(["lane", "exit", "frame", "container", "attempt", "order", "duplicate", "item"])("refuses an invalid %s claim without projecting nearby routes", (reason) => {
    const changed = facts.map((fact) => ({ ...fact }));
    if (reason === "lane") changed[0].lane_id = "b";
    if (reason === "exit") changed[0].target = { kind: "for_each_exit", for_each_id: "items", lane_id: "b" };
    if (reason === "frame") changed[0].frames = [item];
    if (reason === "container") changed[2].container_execution_id = "foreign-pass";
    if (reason === "attempt") changed[0].attempt = 0;
    if (reason === "order") changed[1].sequence = 3;
    if (reason === "duplicate") changed[1].id = changed[0].id;
    if (reason === "item") changed[0].frames = [group, { ...item, item_index: -1 }];
    expect(jobExecutionTrace("run", definition, page(changed))).toBeUndefined();
  });
  it("distinguishes a valid empty committed prefix from absent legacy evidence", () => {
    expect(jobExecutionTrace("run", definition, page([]))).toEqual({ trace: page([]).run_trace, edges: [] });
    expect(jobExecutionTrace("run", definition, { job_id: "run", steps: [], page: {} })).toBeUndefined();
  });
  it("accepts supported long identifiers without a frontend authoring cap", () => {
    const id = "x".repeat(300);
    const route = { source: { kind: "step" as const, id }, outcome: { name: "success" as const }, target: { kind: "step" as const, id: "done" } };
    const archived = { ...definition, steps: [{ step_id: id }, { step_id: "done" }], groups: [], for_each: [], execution_graph: { schema_version: 1, edges: [route] } };
    expect(jobExecutionTrace("run", archived, page([{ ...route, id: "z".repeat(64), sequence: 1, step_execution_id: id, attempt: 1, frames: [] }]))?.edges).toEqual([route]);
  });
});


it("does not conflate delimiter-containing authored source and case identities", () => {
  const left = { source: { kind: "step" as const, id: "choose:case" }, outcome: { name: "case" as const, case_id: "a" }, target: { kind: "step" as const, id: "done" } };
  const right = { source: { kind: "step" as const, id: "choose" }, outcome: { name: "case" as const, case_id: "case:a" }, target: left.target };
  expect(runtimeEdgeKey(left)).not.toBe(runtimeEdgeKey(right));
});
