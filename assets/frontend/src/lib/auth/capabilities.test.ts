import { CAPABILITY_PERMISSIONS, type FrontendCapability, hasCapability } from "@/lib/auth/capabilities";
import { sessionPermissions } from "@/lib/auth/permissions";
import { ROLE_PERMISSIONS } from "@/test/role-permissions";

/**
 * ADR 0076: the interface decides from the permissions the server sends. Each role's
 * list is the catalogue matrix (`src/test/fixtures/role-permissions.json`), so these
 * rows prove the visible behaviour the role-name table gave before is unchanged.
 */
describe("frontend capabilities from permissions", () => {
  it.each([
    ["admin", "administer_platform", true],
    ["operator", "administer_platform", false],
    ["viewer", "administer_platform", false],
    ["admin", "manage_users", true],
    ["operator", "manage_users", false],
    ["viewer", "manage_users", false],
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
    ["admin", "manage_secret_values", true],
    ["operator", "manage_secret_values", true],
    ["viewer", "manage_secret_values", false],
  ] as const)("%s / %s → %s", (role, capability, expected) => {
    expect(hasCapability(ROLE_PERMISSIONS[role], capability)).toBe(expected);
    expect(hasCapability(new Set(ROLE_PERMISSIONS[role]), capability)).toBe(expected);
  });

  it("names one catalogue permission per capability", () => {
    expect(CAPABILITY_PERMISSIONS).toEqual({
      administer_platform: "settings.manage",
      manage_users: "users.manage",
      create_projects: "projects.write",
      mutate_projects: "projects.write",
      read_registry: "registry.manage",
      run_workflows: "workflows.run",
      manage_secret_values: "secrets.value.write",
    });
    for (const key of Object.values(CAPABILITY_PERMISSIONS)) expect(ROLE_PERMISSIONS.admin).toContain(key);
  });

  it.each(Object.keys(CAPABILITY_PERMISSIONS) as FrontendCapability[])("grants nothing without permissions: %s", (capability) => {
    expect(hasCapability(null, capability)).toBe(false);
    expect(hasCapability([], capability)).toBe(false);
  });

  it("reads the session user's permissions and never a role name", () => {
    expect(sessionPermissions({ user: { permissions: ["jobs.read", " settings.manage "] } as never }).has("settings.manage")).toBe(true);
    // A principal that is not role-based, or a session without the list, holds none.
    expect(sessionPermissions({ user: { roles: ["admin"], permissions: null } as never }).size).toBe(0);
    expect(sessionPermissions({ user: { roles: ["admin"] } as never }).size).toBe(0);
    expect(sessionPermissions(null).size).toBe(0);
  });
});
