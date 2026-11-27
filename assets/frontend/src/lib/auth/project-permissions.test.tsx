import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";

import type { AuthSession } from "@/lib/auth/AuthProvider";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import {
  sessionHasProjectLimit,
  sessionProjectPermissions,
  sessionWorkflowPermissions,
  sessionWorkflowReachable,
  sessionWorkspacePermissions,
  useProjectLimitAllows,
  useProjectPermissions,
  useWorkflowLimitAllows,
  useWorkflowPermissions,
  useWorkspacePermissions,
} from "@/lib/auth/project-permissions";
import { permissionsForRoles, sessionUser } from "@/test/role-permissions";

type Session = Pick<AuthSession, "user">;

const operator = permissionsForRoles(["operator"]);

function limited(projectPermissions: Record<string, string[]>): Session {
  return { user: sessionUser("operator", { project_permissions: projectPermissions }) };
}

function wrapper(session: Session | null) {
  return function Wrapper({ children }: Readonly<{ children: ReactNode }>) {
    return <AuthContext.Provider value={{ session } as AuthContextValue}>{children}</AuthContext.Provider>;
  };
}

describe("sessionProjectPermissions", () => {
  it("gives a person without a limit their own permissions in every project", () => {
    const session: Session = { user: sessionUser("operator") };
    expect(sessionHasProjectLimit(session)).toBe(false);
    expect([...sessionProjectPermissions(session, "p-1")].sort()).toEqual([...operator].sort());
    expect(sessionHasProjectLimit({ user: sessionUser("operator", { project_permissions: null }) })).toBe(false);
  });

  it("gives a limited person the project's keys, and none in a project not theirs", () => {
    const session = limited({ "p-1": ["jobs.read", "workflows.read"] });
    expect(sessionHasProjectLimit(session)).toBe(true);
    expect([...sessionProjectPermissions(session, "p-1")].sort()).toEqual(["jobs.read", "workflows.read"]);
    expect(sessionProjectPermissions(session, "p-2").size).toBe(0);
  });

  it("never exceeds the person's own permissions", () => {
    const session = limited({ "p-1": ["jobs.read", "users.manage"] });
    expect([...sessionProjectPermissions(session, "p-1")]).toEqual(["jobs.read"]);
  });

  it("keeps the limit when no project is left", () => {
    const session = limited({});
    expect(sessionHasProjectLimit(session)).toBe(true);
    expect(sessionProjectPermissions(session, "p-1").size).toBe(0);
  });

  it("uses the person's own permissions where no project is named", () => {
    const session = limited({ "p-1": ["jobs.read"] });
    expect(sessionProjectPermissions(session, "").has("workflows.run")).toBe(operator.includes("workflows.run"));
  });
});

describe("useProjectPermissions", () => {
  it("reads the project's keys from the session", () => {
    const { result } = renderHook(() => useProjectPermissions("p-1"), { wrapper: wrapper(limited({ "p-1": ["jobs.read"] })) });
    expect([...result.current]).toEqual(["jobs.read"]);
  });

  it("holds nothing outside an AuthProvider", () => {
    const { result } = renderHook(() => useProjectPermissions("p-1"));
    expect(result.current.size).toBe(0);
  });
});

describe("useProjectLimitAllows", () => {
  it("allows everything without a limit, so other rules keep deciding", () => {
    expect(renderHook(() => useProjectLimitAllows("p-1", "workflows.run")).result.current).toBe(true);
    const { result } = renderHook(() => useProjectLimitAllows("p-1", "workflows.run"), { wrapper: wrapper({ user: sessionUser("viewer") }) });
    expect(result.current).toBe(true);
  });

  it("allows a limited person only what the project grants", () => {
    const session = limited({ "p-1": ["workflows.run"], "p-2": ["jobs.read"] });
    expect(renderHook(() => useProjectLimitAllows("p-1", "workflows.run"), { wrapper: wrapper(session) }).result.current).toBe(true);
    expect(renderHook(() => useProjectLimitAllows("p-2", "workflows.run"), { wrapper: wrapper(session) }).result.current).toBe(false);
    expect(renderHook(() => useProjectLimitAllows("p-3", "workflows.run"), { wrapper: wrapper(session) }).result.current).toBe(false);
  });
});

/** Viewer in workspace w-1 (projects p-1, p-2), operator in p-2, viewer on workflow wf-a only in p-3. */
function places(): Session {
  return {
    user: sessionUser("operator", {
      project_permissions: { "p-1": ["jobs.read"], "p-2": ["jobs.read", "workflows.run"], "p-3": ["jobs.read"] },
      workspace_permissions: { "w-1": ["jobs.read"], "w-2": ["jobs.read"] },
      workflow_permissions: { "p-2": { "wf-b": ["jobs.read"] }, "p-3": { "wf-a": ["jobs.read", "approvals.decide"] } },
      project_workflow_ids: { "p-3": ["wf-a"] },
    }),
  };
}

describe("workspace and workflow permissions (ADR 0076 §8b)", () => {
  it("reads a workspace's keys, and none for a workspace out of reach", () => {
    const session = places();
    expect([...sessionWorkspacePermissions(session, "w-1")]).toEqual(["jobs.read"]);
    expect(sessionWorkspacePermissions(session, "w-9").size).toBe(0);
    expect([...sessionWorkspacePermissions({ user: sessionUser("operator") }, "w-9")].sort()).toEqual([...operator].sort());
  });

  it("takes the workflow's grant, then the project's", () => {
    const session = places();
    expect([...sessionWorkflowPermissions(session, "p-2", "wf-b")]).toEqual(["jobs.read"]);
    expect([...sessionWorkflowPermissions(session, "p-2", "wf-other")].sort()).toEqual(["jobs.read", "workflows.run"]);
    expect([...sessionWorkflowPermissions(session, "p-2", "")].sort()).toEqual(["jobs.read", "workflows.run"]);
  });

  it("gives nothing for another workflow of a project reached only through workflow grants", () => {
    const session = places();
    expect([...sessionWorkflowPermissions(session, "p-3", "wf-a")].sort()).toEqual(["approvals.decide", "jobs.read"]);
    expect(sessionWorkflowPermissions(session, "p-3", "wf-z").size).toBe(0);
    expect(sessionWorkflowReachable(session, "p-3", "wf-a")).toBe(true);
    expect(sessionWorkflowReachable(session, "p-3", "wf-z")).toBe(false);
    expect(sessionWorkflowReachable(session, "p-1", "wf-z")).toBe(true);
    expect(sessionWorkflowReachable(session, "p-9", "wf-a")).toBe(false);
  });

  it("never exceeds the person's own permissions in a workflow", () => {
    const session = { user: sessionUser("viewer", { project_permissions: { "p-1": [] }, workflow_permissions: { "p-1": { "wf-a": ["workflows.run"] } } }) };
    expect(sessionWorkflowPermissions(session, "p-1", "wf-a").has("workflows.run")).toBe(false);
  });

  it("exposes the same through hooks", () => {
    const options = { wrapper: wrapper(places()) };
    expect([...renderHook(() => useWorkspacePermissions("w-2"), options).result.current]).toEqual(["jobs.read"]);
    expect(renderHook(() => useWorkflowPermissions("p-3", "wf-z"), options).result.current.size).toBe(0);
    expect(renderHook(() => useWorkflowLimitAllows("p-3", "wf-a", "approvals.decide"), options).result.current).toBe(true);
    expect(renderHook(() => useWorkflowLimitAllows("p-2", "wf-b", "workflows.run"), options).result.current).toBe(false);
    expect(renderHook(() => useWorkflowLimitAllows("p-2", "wf-c", "workflows.run"), options).result.current).toBe(true);
    expect(renderHook(() => useWorkflowLimitAllows("p-3", "wf-a", "workflows.run")).result.current).toBe(true);
  });
});
