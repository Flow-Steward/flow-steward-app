import { ROLE_PERMISSIONS } from "@/test/role-permissions";

import { canMutateJob, isCancellableJobStatus } from "./job-action-policy";

// ADR 0076: cancel, restart and acknowledge need jobs.write; the role lists are the
// catalogue matrix, so admin and operator keep the authority and a viewer has none.
it.each([
  ["admin", true], ["operator", true], ["viewer", false],
] as const)("keeps job mutation authority for %s: %s", (role, expected) => {
  expect(canMutateJob(new Set(ROLE_PERMISSIONS[role]))).toBe(expected);
});

it("gives no mutation authority without the jobs.write permission", () => {
  expect(canMutateJob(new Set())).toBe(false);
  expect(canMutateJob(new Set(["jobs.read", "jobs.manage"]))).toBe(false);
  expect(canMutateJob(new Set(["jobs.write"]))).toBe(true);
});

it.each([
  ["pending", true], ["running", true], ["in_progress", true], ["resume_pending", true],
  ["awaiting_approval", true], ["awaiting_timer", true], ["awaiting_child", true], ["awaiting_event", true],
  ["completed", false], ["failed", false], ["cancelled", false], ["external_wait", false],
  ["unknown", false], [null, false],
])("preserves cancellability for recorded status %s", (status, expected) => {
  expect(isCancellableJobStatus(status)).toBe(expected);
});
