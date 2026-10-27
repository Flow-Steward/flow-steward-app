import type { QueryClient } from "@tanstack/react-query";

import { HttpError } from "@/lib/http/errors";
import { queryKeys } from "@/lib/query/keys";

export const AUTH_SESSION_RETRIES = 2;
export const AUTH_SESSION_RETRY_DELAY_MS = 400;

/**
 * Whether a failed session read could succeed on a retry. A signed-out answer
 * is a 401 and never reaches here as an error; a 5xx, a timeout or a dropped
 * connection says nothing about the session.
 */
export function sessionErrorIsTransient(error: unknown): boolean {
  if (error instanceof HttpError) {
    return error.status >= 500 || error.status === 0 || error.status === 408 || error.status === 429;
  }
  return true;
}

function isAuthSessionQuery(queryKey: readonly unknown[]): boolean {
  return (
    queryKey[0] === queryKeys.authSession[0] &&
    queryKey[1] === queryKeys.authSession[1]
  );
}

/** The browser database of workflow builder edit sessions (local copies and Undo histories). */
export const WORKFLOW_EDIT_SESSION_DATABASE = "fs-workflow-edit-sessions";
/** This tab's reference to its edit session, per account, user and workflow. */
export const WORKFLOW_EDIT_SESSION_REFERENCE_PREFIX = "fs.workflowEditSession:";

type TabStorage = Pick<Storage, "key" | "removeItem"> & { readonly length: number };
type UserBrowserData = {
  sessionStorage?: TabStorage | null;
  indexedDB?: Pick<IDBFactory, "deleteDatabase"> | null;
};

function defaultUserBrowserData(): UserBrowserData {
  try {
    return { sessionStorage: globalThis.sessionStorage ?? null, indexedDB: globalThis.indexedDB ?? null };
  } catch {
    return {};
  }
}

/** Sign-out forgets this browser's edit sessions: their references and their recovery records. */
function purgeEditSessions(data: UserBrowserData): void {
  try {
    const storage = data.sessionStorage;
    if (storage) {
      const keys: string[] = [];
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key?.startsWith(WORKFLOW_EDIT_SESSION_REFERENCE_PREFIX)) keys.push(key);
      }
      keys.forEach((key) => storage.removeItem(key));
    }
  } catch {
    // Tab storage may be unavailable; the recovery records below are still removed.
  }
  try {
    data.indexedDB?.deleteDatabase(WORKFLOW_EDIT_SESSION_DATABASE);
  } catch {
    // An unavailable database holds nothing to remove.
  }
}

export async function purgeAuthenticatedClientState(
  queryClient: QueryClient,
  storage: Pick<Storage, "clear"> | null,
  userBrowserData: UserBrowserData = defaultUserBrowserData(),
): Promise<void> {
  const isUserQuery = (query: { queryKey: readonly unknown[] }) =>
    !isAuthSessionQuery(query.queryKey);
  await queryClient.cancelQueries({ predicate: isUserQuery });
  queryClient.removeQueries({ predicate: isUserQuery });
  try {
    storage?.clear();
  } catch {
    // Storage may be unavailable in restricted browser contexts; cache purge remains authoritative.
  }
  purgeEditSessions(userBrowserData);
}
