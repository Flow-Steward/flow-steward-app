import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { purgeAuthenticatedClientState } from "@/lib/auth/session-state";
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
