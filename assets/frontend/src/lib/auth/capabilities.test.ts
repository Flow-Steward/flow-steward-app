import { hasCapability } from "@/lib/auth/capabilities";

describe("frontend role capability model", () => {
  it.each([
    ["admin", "administer_platform", true],
    ["operator", "administer_platform", false],
    ["viewer", "administer_platform", false],
    ["admin", "read_registry", true],
    ["operator", "read_registry", false],
    ["viewer", "read_registry", false],
    ["admin", "create_projects", true],
    ["operator", "create_projects", false],
    ["viewer", "create_projects", false],
    ["admin", "mutate_projects", true],
    ["operator", "mutate_projects", false],
    ["viewer", "mutate_projects", false],
    // ADR 0066 amendment: the operator does the work an admin configured.
    ["admin", "run_workflows", true],
    ["operator", "run_workflows", true],
    ["viewer", "run_workflows", false],
    ["operator", "manage_secret_values", true],
    ["viewer", "manage_secret_values", false],
  ] as const)("maps %s / %s to %s", (role, capability, expected) => {
    expect(hasCapability([role], capability)).toBe(expected);
  });
});
