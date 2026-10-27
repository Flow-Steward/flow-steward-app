import { hasCapability } from "@/lib/auth/capabilities";

describe("frontend role capability model", () => {
  it.each([
    ["admin", "administer_platform", true],
    ["operator", "administer_platform", false],
    ["viewer", "administer_platform", false],
    ["operator", "read_registry", true],
    ["viewer", "read_registry", false],
    ["admin", "create_projects", true],
    ["operator", "create_projects", false],
    ["viewer", "create_projects", false],
    ["operator", "mutate_projects", true],
    ["viewer", "mutate_projects", false],
  ] as const)("maps %s / %s to %s", (role, capability, expected) => {
    expect(hasCapability([role], capability)).toBe(expected);
  });
});
