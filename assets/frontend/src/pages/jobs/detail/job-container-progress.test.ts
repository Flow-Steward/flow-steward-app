import type { JobDefinitionPayload, JobExecutionStepsPage } from "./api";
import { jobContainerProgress } from "./job-container-progress";

const definition: JobDefinitionPayload = {
  job_id: "job", available: true, schema_version: 1, workflow_id: "author", snapshot_id: "archive", revision: 4, digest: "digest",
  steps: [{ step_id: "member", for_each_id: "items", lane_id: "work" }],
  groups: [{ group_id: "batch", body: { kind: "sequence", entry: { kind: "for_each", id: "items" } } }],
  for_each: [{ for_each_id: "items", group_id: "batch", body: { kind: "lanes", lanes: [{ lane_id: "work", entry: { kind: "step", id: "member" } }] } }],
  execution_graph: { schema_version: 1, entry: { kind: "group", id: "batch" }, edges: [] },
};
function progress() {
  return {
    schema_version: 1, job_id: "job", project_id: "project", workflow_id: "author", snapshot_id: "archive", revision: 4, digest: "digest",
    covered_from_start: true, truncated: false, checkpoint_terminal: true, last_sequence: 8,
    passes: [
      { container: { kind: "group", id: "batch" }, execution_id: "group-pass", parent_frames: [], started_sequence: 1, updated_sequence: 7,
        finished_sequence: 7, current: false, outcome: "error", failure: { code: "bad", message: "Item failed", boundary: "body", step_id: "member" } },
      { container: { kind: "for_each", id: "items" }, execution_id: "items-pass", parent_frames: [{ kind: "group", id: "batch", execution_id: "group-pass" }],
        started_sequence: 2, updated_sequence: 8, finished_sequence: 6, current: false, outcome: "error",
        for_each: { item_index: 0, source_count: 1, selected_count: 1, processed_count: 0, applied_limit: 100, truncated: false, broke_at: null },
        lanes: { item_index: 0, completed_count: 0, total_count: 1, rows: [{ lane_id: "work", status: "failed" }] },
        failure: { code: "bad", message: "Item failed", boundary: "parallel", step_id: "member", step_execution_id: "member-pass", attempt: 1,
          lane_id: "work", item_index: 0, handling: { kind: "route", execution_id: "group-pass", transition_id: "a".repeat(64) } } },
    ],
  };
}
function page(value: unknown): JobExecutionStepsPage {
  return { job_id: "job", steps: [], page: { has_more: true, next_cursor: "history" }, container_progress: value };
}

it("retains exact accepted handling after the D061 tail and after the parent lifecycle finished", () => {
  const value = progress();
  // The optional D061 tail and step-history pagination cannot veto this independently committed aggregate.
  expect(jobContainerProgress("job", "project", definition, page(value))).toEqual({ progress: value });
  expect(jobContainerProgress("job", "project", definition, { ...page(value), run_trace: { truncated: true, transitions: [] } })).toEqual({ progress: value });
  expect(value.passes[1].updated_sequence).toBeGreaterThan(value.passes[0].finished_sequence);
});

it.each([
  ["project", { project_id: "other" }], ["archive", { snapshot_id: "other" }], ["partial", { covered_from_start: false }],
  ["truncated", { truncated: true }], ["hidden passes", { passes: [] }],
])("rejects %s without acquiring authority from candidate data", (_reason, changes) => {
  expect(jobContainerProgress("job", "project", definition, page({ ...progress(), ...changes }))).toBeUndefined();
});
it("requires independently verified project authority even for an internally consistent public payload", () => {
  expect(jobContainerProgress("job", undefined, definition, page(progress()))).toBeUndefined();
});
it.each(["foreign handling", "foreign step", "wrong parent", "wrong item", "invented lanes", "wrong counts"])("rejects %s while preserving the caller's independent trace", (reason) => {
  const value = progress(), item = value.passes[1];
  if (!item.failure?.handling || !item.for_each || !item.lanes) throw new Error("Expected exact failure fixture");
  if (reason === "foreign handling") item.failure.handling.execution_id = "other-pass";
  else if (reason === "foreign step") item.failure.step_id = "other-step";
  else if (reason === "wrong parent") item.parent_frames[0].execution_id = "other-pass";
  else if (reason === "wrong item") item.lanes.item_index = 1;
  else if (reason === "invented lanes") item.lanes.rows[0].lane_id = "other-lane";
  else item.lanes.completed_count = 1;
  const run_trace = { independently: "owned elsewhere" };
  const first = { ...page(value), run_trace };
  expect(jobContainerProgress("job", "project", definition, first)).toBeUndefined();
  expect(first.run_trace).toBe(run_trace);
});
it("keeps item selection truncation distinct from aggregate coverage truncation", () => {
  const value = progress(), item = value.passes[1];
  if (!item.for_each) throw new Error("Expected selected pass");
  item.for_each.source_count = 101;
  item.for_each.selected_count = 100;
  item.for_each.truncated = true;
  expect(jobContainerProgress("job", "project", definition, page(value))).toEqual({ progress: value });
});
it("distinguishes complete empty evidence from legacy absence", () => {
  const empty = { ...progress(), passes: [], last_sequence: 0 };
  expect(jobContainerProgress("job", "project", definition, page(empty))).toEqual({ progress: empty });
  expect(jobContainerProgress("job", "project", definition, { job_id: "job", steps: [], page: {} })).toBeUndefined();
});


it("accepts a nested member failure propagated to its inherited enclosing Group", () => {
  const evidence = jobContainerProgress("job", "project", definition, page(progress()));
  expect(evidence?.progress.passes[0]).toMatchObject({
    container: { kind: "group", id: "batch" }, outcome: "error", failure: { step_id: "member" },
  });
});
it("rejects a Group failure whose member inherits a different enclosing Group", () => {
  const foreign: JobDefinitionPayload = { ...definition,
    steps: [...(definition.steps || []), { step_id: "foreign-member", for_each_id: "foreign-items" }],
    groups: [...(definition.groups || []), { group_id: "foreign-group", body: { kind: "sequence", entry: { kind: "for_each", id: "foreign-items" } } }],
    for_each: [...(definition.for_each || []), { for_each_id: "foreign-items", group_id: "foreign-group", body: { kind: "sequence", entry: { kind: "step", id: "foreign-member" } } }],
  };
  const value = progress();
  value.passes[0].failure.step_id = "foreign-member";
  expect(jobContainerProgress("job", "project", foreign, page(value))).toBeUndefined();
});
it("rejects an archive step declaring both immediate owners instead of accepting the forged direct Group claim", () => {
  const invalid: JobDefinitionPayload = { ...definition, steps: [{ ...definition.steps![0], group_id: "batch" }] };
  expect(jobContainerProgress("job", "project", invalid, page(progress()))).toBeUndefined();
});


it.each(["groups", "for_each", "steps"] as const)("ignores a nullable %s archive row without throwing or mutating the independent trace", (field) => {
  const malformed = { ...definition };
  // Deliberately malformed transport input; keep the public DTO's declared types strict.
  Object.assign(malformed, { [field]: [null] });
  const run_trace = { schema_version: 1, job_id: "job", workflow_id: "author", snapshot_id: "archive", revision: 4, digest: "digest",
    covered_from_start: true, truncated: false, terminal: true, last_sequence: 0, transitions: [] };
  const first = { ...page(progress()), run_trace };
  expect(jobContainerProgress("job", "project", malformed, first)).toBeUndefined();
  expect(first.run_trace).toBe(run_trace);
});
