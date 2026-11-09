import { describe, expect, it } from "vitest";

import { connectionRepairOperations, stepConnectionId } from "./detail-connection-repair";

const steps = [
  { step_id: "export", connection_binding_jsonb: { connection_id: "conn_old" } },
  { step_id: "update", connection_binding_jsonb: { connection_id: "conn_old", subject_key: "shop" } },
  { step_id: "other_shop", connection_binding_jsonb: { connection_id: "conn_other" } },
  { step_id: "legacy", override: { connector: { connection_binding: { connection_id: "conn_old" } } } },
  { step_id: "plain" },
];

describe("moving a workflow off a deleted connection", () => {
  it("reads a step's binding the way the runtime does: its own column first", () => {
    expect(stepConnectionId({ connection_binding_jsonb: { connection_id: "a" }, override: { connector: { connection_binding: { connection_id: "b" } } } })).toBe("a");
    expect(stepConnectionId({ override: { connector: { connection_binding: { connection_id: "b" } } } })).toBe("b");
  });

  it("moves every other step on the deleted connection to the new one, keeping their other binding keys", () => {
    const operations = connectionRepairOperations({
      steps, savedStepId: "export", previousId: "conn_old", nextId: "conn_new", availableIds: new Set(["conn_new", "conn_other"]),
    });
    expect(operations).toEqual([
      { op: "update_step_config", step_id: "update", patch: { connection_binding_jsonb: { connection_id: "conn_new", subject_key: "shop" } } },
      { op: "update_step_config", step_id: "legacy", patch: { connection_binding_jsonb: { connection_id: "conn_new" } } },
    ]);
  });

  it("moves nothing when the old connection still exists, the list is not loaded, or nothing changed", () => {
    const base = { steps, savedStepId: "export", previousId: "conn_old", nextId: "conn_new" };
    expect(connectionRepairOperations({ ...base, availableIds: new Set(["conn_new", "conn_old"]) })).toEqual([]);
    expect(connectionRepairOperations({ ...base, availableIds: new Set() })).toEqual([]);
    expect(connectionRepairOperations({ ...base, nextId: "conn_old", availableIds: new Set(["conn_new"]) })).toEqual([]);
  });

  it("leaves a step the unapplied structure already changes", () => {
    const operations = connectionRepairOperations({
      steps, savedStepId: "export", previousId: "conn_old", nextId: "conn_new", availableIds: new Set(["conn_new"]),
      isTouched: (id) => id === "update",
    });
    expect(operations.map((operation) => operation.step_id)).toEqual(["legacy"]);
  });
});
