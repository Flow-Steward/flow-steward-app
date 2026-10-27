import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
export const WORKSPACE_STORAGE_KEY = "platform:selected-workspace-id";
export const WORKSPACE_SELECTED_EVENT = "platform:workspace-selected";
export const WORKSPACES_CHANGED_EVENT = "platform:workspaces-changed";

type BrowserStorage = Pick<Storage, "getItem" | "removeItem" | "setItem">;
type WorkspaceLike = { id: string };

function browserStorage(): BrowserStorage | null {
  if (globalThis.window === undefined) {
    return null;
  }
  return globalThis.localStorage;
}

export function normalizeWorkspaceId(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim();
}

export function readStoredWorkspaceId(storage: BrowserStorage | null = browserStorage()): string {
  if (!storage) {
    return "";
  }
  return normalizeWorkspaceId(storage.getItem(WORKSPACE_STORAGE_KEY));
}

export function persistWorkspaceSelection(
  workspaceId: string,
  storage: BrowserStorage | null = browserStorage(),
) {
  const normalizedWorkspaceId = normalizeWorkspaceId(workspaceId);
  if (storage) {
    if (normalizedWorkspaceId) {
      storage.setItem(WORKSPACE_STORAGE_KEY, normalizedWorkspaceId);
    } else {
      storage.removeItem(WORKSPACE_STORAGE_KEY);
    }
  }
  if (globalThis.window !== undefined) {
    globalThis.dispatchEvent(
      new CustomEvent(WORKSPACE_SELECTED_EVENT, {
        detail: { workspaceId: normalizedWorkspaceId },
      }),
    );
  }
  return normalizedWorkspaceId;
}

/**
 * Signal that the set of available workspaces changed (e.g. one was deleted), so
 * any mounted surface holding its own workspace list (the app shell selector, the
 * registry page) can reload it without a full page refresh.
 */
export function notifyWorkspacesChanged() {
  if (globalThis.window !== undefined) {
    globalThis.dispatchEvent(new CustomEvent(WORKSPACES_CHANGED_EVENT));
  }
}

export function resolveWorkspaceSelection<T extends WorkspaceLike>(
  selectedWorkspaceId: string,
  workspaces: T[],
): string {
  const normalizedSelectedWorkspaceId = normalizeWorkspaceId(selectedWorkspaceId);
  const normalizedWorkspaces = Array.isArray(workspaces)
    ? workspaces.filter((workspace) => normalizeWorkspaceId(workspace?.id))
    : [];
  // There is no "all workspaces" scope. A session is always pointed at one
  // workspace, so both the unset case and the stale case fall back to the first
  // available one rather than to the empty aggregate they used to return.
  // The empty string survives only when there is genuinely nothing to point at.
  const firstWorkspaceId = normalizedWorkspaces.length
    ? normalizeWorkspaceId(normalizedWorkspaces[0].id)
    : "";
  if (!normalizedSelectedWorkspaceId) {
    return firstWorkspaceId;
  }
  const selectedWorkspaceExists = normalizedWorkspaces.some(
    (workspace) => normalizeWorkspaceId(workspace.id) === normalizedSelectedWorkspaceId,
  );
  return selectedWorkspaceExists ? normalizedSelectedWorkspaceId : firstWorkspaceId;
}
