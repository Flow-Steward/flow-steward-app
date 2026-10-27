import { canMutateJob, isCancellableJobStatus } from "./job-action-policy";

it.each([
  [["admin"], true], [["operator"], true], [[" Admin "], true], [["viewer", "OPERATOR"], true],
  [["viewer"], false], [[], false], [[null, "administrator"], false],
])("preserves job mutation authority for roles %j", (roles, expected) => {
  expect(canMutateJob(roles as unknown[])).toBe(expected);
});

it.each([
  ["pending", true], ["running", true], ["in_progress", true], ["resume_pending", true],
  ["awaiting_approval", true], ["awaiting_timer", true], ["awaiting_child", true],
  ["completed", false], ["failed", false], ["cancelled", false], ["external_wait", false],
  ["unknown", false], [null, false],
])("preserves cancellability for recorded status %s", (status, expected) => {
  expect(isCancellableJobStatus(status)).toBe(expected);
});
