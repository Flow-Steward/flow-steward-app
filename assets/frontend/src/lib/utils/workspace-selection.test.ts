import { describe, expect, it, vi } from "vitest";

import {
  persistWorkspaceSelection,
  readStoredWorkspaceId,
  resolveWorkspaceSelection,
  WORKSPACE_SELECTED_EVENT,
  WORKSPACE_STORAGE_KEY,
} from "@/lib/utils/workspace-selection";

describe("workspace selection helpers", () => {
  it("keeps the current workspace when it still exists", () => {
    expect(
      resolveWorkspaceSelection("workspace-test", [
        { id: "workspace-test" },
        { id: "workspace-other" },
      ]),
    ).toBe("workspace-test");
  });

  it("falls back to the only available workspace when the stored one is stale", () => {
    expect(
      resolveWorkspaceSelection("workspace-deleted", [{ id: "workspace-test" }]),
    ).toBe("workspace-test");
  });

  it("falls back to the first workspace when the stored one is stale and several remain", () => {
    // There is no "all workspaces" scope to clear to, so a stale id lands on a
    // real workspace rather than on the empty aggregate this used to return.
    expect(
      resolveWorkspaceSelection("workspace-deleted", [
        { id: "workspace-test" },
        { id: "workspace-other" },
      ]),
    ).toBe("workspace-test");
  });

  it("picks the first workspace when nothing is stored and several exist", () => {
    expect(
      resolveWorkspaceSelection("", [{ id: "workspace-test" }, { id: "workspace-other" }]),
    ).toBe("workspace-test");
  });

  it("stays empty only when there is no workspace to point at", () => {
    expect(resolveWorkspaceSelection("", [])).toBe("");
    expect(resolveWorkspaceSelection("workspace-deleted", [])).toBe("");
  });

  it("reads and trims the stored workspace id", () => {
    const storage = {
      getItem: vi.fn(() => " workspace-test "),
      removeItem: vi.fn(),
      setItem: vi.fn(),
    };
    expect(readStoredWorkspaceId(storage)).toBe("workspace-test");
  });

  it("persists the workspace id and emits a sync event", () => {
    const listener = vi.fn();
    globalThis.addEventListener(WORKSPACE_SELECTED_EVENT, listener as EventListener);
    const storage = {
      getItem: vi.fn(),
      removeItem: vi.fn(),
      setItem: vi.fn(),
    };

    persistWorkspaceSelection("workspace-test", storage);

    expect(storage.setItem).toHaveBeenCalledWith(WORKSPACE_STORAGE_KEY, "workspace-test");
    expect(listener).toHaveBeenCalled();
    globalThis.removeEventListener(WORKSPACE_SELECTED_EVENT, listener as EventListener);
  });

  it("removes the stored workspace id when selection is cleared", () => {
    const storage = {
      getItem: vi.fn(),
      removeItem: vi.fn(),
      setItem: vi.fn(),
    };

    persistWorkspaceSelection("", storage);

    expect(storage.removeItem).toHaveBeenCalledWith(WORKSPACE_STORAGE_KEY);
  });
});
