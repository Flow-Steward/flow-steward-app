import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "@/lib/auth/AuthProvider";
import { dropAccountScopedQueries, purgeAuthenticatedClientState } from "@/lib/auth/session-state";
import { useAuth } from "@/lib/auth/useAuth";
import { queryKeys } from "@/lib/query/keys";

describe("purgeAuthenticatedClientState", () => {
  it("removes user query data and local storage while retaining the live auth observer cache", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(queryKeys.authSession, { authenticated: true });
    queryClient.setQueryData(queryKeys.projectDetail("project-a"), {
      name: "Private project",
    });
    globalThis.localStorage.setItem(
      "platform:selected-workspace-id",
      "workspace-a",
    );
    globalThis.localStorage.setItem("chat:project-a", "private-thread");

    await purgeAuthenticatedClientState(queryClient, globalThis.localStorage);

    expect(queryClient.getQueryData(queryKeys.authSession)).toEqual({
      authenticated: true,
    });
    expect(
      queryClient.getQueryData(queryKeys.projectDetail("project-a")),
    ).toBeUndefined();
    expect(globalThis.localStorage.length).toBe(0);
  });

  it("forgets the browser's workflow edit sessions on sign-out", async () => {
    const tab = new Map<string, string>([
      ["fs.workflowEditSession:account:user:wf", "session-1"],
      ["unrelated", "kept"],
    ]);
    const sessionStorage = {
      get length() { return tab.size; },
      key: (index: number) => [...tab.keys()][index] ?? null,
      removeItem: (key: string) => { tab.delete(key); },
    };
    const deleted: string[] = [];
    const indexedDB = { deleteDatabase: (name: string) => { deleted.push(name); return {} as IDBOpenDBRequest; } };

    await purgeAuthenticatedClientState(new QueryClient(), null, { sessionStorage, indexedDB });

    expect([...tab.keys()]).toEqual(["unrelated"]);
    expect(deleted).toEqual(["fs-workflow-edit-sessions"]);
  });
});

describe("dropAccountScopedQueries", () => {
  it("forgets the previous account's answers but keeps the session and browser storage", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(queryKeys.authSession, { authenticated: true });
    queryClient.setQueryData(queryKeys.projectDetail("project-a"), { name: "Old account project" });
    globalThis.localStorage.setItem("platform:theme", "dark");

    await dropAccountScopedQueries(queryClient);

    expect(queryClient.getQueryData(queryKeys.authSession)).toEqual({ authenticated: true });
    expect(queryClient.getQueryData(queryKeys.projectDetail("project-a"))).toBeUndefined();
    expect(globalThis.localStorage.getItem("platform:theme")).toBe("dark");
    globalThis.localStorage.clear();
  });
});

describe("registered page bootstrap normalization", () => {
  afterEach(() => vi.unstubAllGlobals());

  function RegisteredPageKeys() {
    const auth = useAuth();
    if (auth.status === "loading") return null;
    return createElement(
      "output",
      { "aria-label": "Registered pages" },
      JSON.stringify((auth.session?.registered_pages ?? []).map((page) => page.page_key)),
    );
  }

  it("keeps compiled and host-rendered rows and drops rows without a renderer", async () => {
    const resourceAdmin = {
      kind: "resource_admin",
      resources: [{ key: "records", title: "Records", list_path: "/api/modules/records", id_field: "id", fields: [{ key: "name" }] }],
    };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      auth: { authenticated: true, local_auth_enabled: true },
      registered_pages: [
        { page_key: "compiled", path: "/compiled", component_key: "private.fixture" },
        { page_key: "module_records", path: "/modules/records", control_surface: resourceAdmin },
        { page_key: "module_journal", path: "/modules/journal", control_surface: { kind: "resource_report", list_path: "/api/modules/journal", columns: [{ key: "a", label: "A" }] } },
        { page_key: "module_status", path: "/modules/status", control_surface: { kind: "status_document", status_path: "/api/modules/status", upload_path: "/api/modules/document", fields: [{ key: "state", label: "State" }] } },
        { page_key: "unknown_kind", path: "/modules/unknown", control_surface: { kind: "dashboard" } },
        { page_key: "bare", path: "/bare" },
        { page_key: "array_surface", path: "/modules/array", control_surface: ["resource_admin"] },
        { page_key: "relative", path: "modules/relative", control_surface: resourceAdmin },
      ],
    }), { status: 200, headers: { "content-type": "application/json" } })));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(createElement(QueryClientProvider, { client: queryClient }, createElement(AuthProvider, null, createElement(RegisteredPageKeys))));

    const keys = JSON.parse((await screen.findByLabelText("Registered pages")).textContent || "[]");
    expect(keys).toEqual(["compiled", "module_records", "module_journal", "module_status"]);
  });

  it("keeps a page that names a list of features only when the account holds one of them", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      auth: { authenticated: true, local_auth_enabled: true },
      feature_flags: { "acme.extra": true },
      registered_pages: [
        { page_key: "plain", path: "/plain", component_key: "private.plain" },
        { page_key: "any_of", path: "/any-of", component_key: "private.any", required_entitlement: ["acme.base", "acme.extra"] },
        { page_key: "single", path: "/single", component_key: "private.single", required_entitlement: "acme.extra" },
        { page_key: "missing", path: "/missing", component_key: "private.missing", required_entitlement: ["acme.base", "acme.other"] },
      ],
    }), { status: 200, headers: { "content-type": "application/json" } })));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(createElement(QueryClientProvider, { client: queryClient }, createElement(AuthProvider, null, createElement(RegisteredPageKeys))));

    const keys = JSON.parse((await screen.findByLabelText("Registered pages")).textContent || "[]");
    expect(keys).toEqual(["plain", "any_of", "single"]);
  });
});
