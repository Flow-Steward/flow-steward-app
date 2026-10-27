import { stepPorts, groupPorts } from "./change-set";

it("always exposes error on nonterminal steps and groups, never on stops", () => {
  for (const kind of ["agent", "http_request", "if", "switch", "approval", "wait"]) {
    expect(stepPorts({ step_id: "s", step_kind: kind }).some((port) => port.outcome.name === "error")).toBe(true);
  }
  expect(groupPorts({})).toContainEqual({ outcome: { name: "error" }, label: "On error" });
  for (const kind of ["stop_success", "stop_fail"]) expect(stepPorts({ step_id: "s", step_kind: kind })).toEqual([]);
});
