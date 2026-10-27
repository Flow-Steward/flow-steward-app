import {
  WORKFLOW_EDIT_SESSION_DATABASE,
  WORKFLOW_EDIT_SESSION_REFERENCE_PREFIX,
} from "@/lib/auth/session-state";
import type { WorkflowEditSession, WorkflowEditSessionIdentity } from "@/pages/workflows/detail/workflow-edit-session";

/**
 * Where an edit session lives in the browser.
 *
 * The record is in IndexedDB, one per session id, written as one transaction
 * after every completed author action: a write counts only once its
 * transaction completed. The tab keeps only a reference to its session id in
 * sessionStorage, so a reload finds the same session and another tab never
 * writes over it. A duplicated tab copies the reference; the Web Locks API
 * tells the copy that the session already has an owner, and the copy forks the
 * record into its own id before its first write.
 */

const STORE = "sessions";
const DATABASE_VERSION = 1;

export type WorkflowEditSessionStore = {
  /** The raw stored record, or null; the model parses and validates it. */
  load: (sessionId: string) => Promise<unknown>;
  save: (session: WorkflowEditSession) => Promise<void>;
  remove: (sessionId: string) => Promise<void>;
};

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });
}

/** The IndexedDB store of edit sessions, or null where the browser offers no IndexedDB. */
export function createWorkflowEditSessionStore(
  factory: IDBFactory | null | undefined = (() => {
    try { return globalThis.indexedDB; } catch { return undefined; }
  })(),
): WorkflowEditSessionStore | null {
  if (!factory) return null;
  let database: Promise<IDBDatabase> | null = null;
  const open = () => {
    database ??= new Promise<IDBDatabase>((resolve, reject) => {
      let request: IDBOpenDBRequest;
      try {
        request = factory.open(WORKFLOW_EDIT_SESSION_DATABASE, DATABASE_VERSION);
      } catch (error) {
        reject(error instanceof Error ? error : new Error("IndexedDB is unavailable"));
        return;
      }
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE, { keyPath: "sessionId" });
      };
      request.onsuccess = () => {
        const opened = request.result;
        // A sign-out elsewhere deletes the database: let it, and reopen on the next use.
        opened.onversionchange = () => { opened.close(); database = null; };
        resolve(opened);
      };
      request.onerror = () => reject(request.error ?? new Error("IndexedDB is unavailable"));
      request.onblocked = () => reject(new Error("IndexedDB is blocked by another tab"));
    }).catch((error: unknown) => {
      database = null;
      throw error;
    });
    return database;
  };
  return {
    async load(sessionId) {
      const db = await open();
      const transaction = db.transaction(STORE, "readonly");
      const result = await requestResult(transaction.objectStore(STORE).get(sessionId));
      return result ?? null;
    },
    async save(session) {
      const db = await open();
      const transaction = db.transaction(STORE, "readwrite");
      const done = transactionDone(transaction);
      transaction.objectStore(STORE).put(session);
      await done;
    },
    async remove(sessionId) {
      const db = await open();
      const transaction = db.transaction(STORE, "readwrite");
      const done = transactionDone(transaction);
      transaction.objectStore(STORE).delete(sessionId);
      await done;
    },
  };
}

type TabStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function tabStorage(storage?: TabStorage | null): TabStorage | null {
  if (storage !== undefined) return storage;
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

export function sessionReferenceKey(identity: WorkflowEditSessionIdentity): string {
  return `${WORKFLOW_EDIT_SESSION_REFERENCE_PREFIX}${JSON.stringify([identity.accountId, identity.userId, identity.workflowId])}`;
}

export function readSessionReference(identity: WorkflowEditSessionIdentity, storage?: TabStorage | null): string | null {
  try {
    return tabStorage(storage)?.getItem(sessionReferenceKey(identity)) || null;
  } catch {
    return null;
  }
}

export function writeSessionReference(identity: WorkflowEditSessionIdentity, sessionId: string, storage?: TabStorage | null): void {
  try {
    tabStorage(storage)?.setItem(sessionReferenceKey(identity), sessionId);
  } catch {
    // Without tab storage a reload starts a new session; the record itself is still written.
  }
}

export function clearSessionReference(identity: WorkflowEditSessionIdentity, storage?: TabStorage | null): void {
  try {
    tabStorage(storage)?.removeItem(sessionReferenceKey(identity));
  } catch {
    // Nothing to clear.
  }
}

export type SessionOwnership = { owned: boolean; release: () => void };
type LockManagerLike = {
  request: (name: string, options: { ifAvailable: boolean }, callback: (lock: unknown) => Promise<void> | void) => Promise<unknown>;
};

function lockManager(locks?: LockManagerLike | null): LockManagerLike | null {
  if (locks !== undefined) return locks;
  try {
    return (globalThis.navigator as Navigator & { locks?: LockManagerLike } | undefined)?.locks ?? null;
  } catch {
    return null;
  }
}

/**
 * Hold the session for this tab while it is open. False when another live tab
 * (a duplicate) already holds it. Without the Web Locks API a tab owns its
 * session; a copied reference can then not be told apart from a reload.
 */
export function claimSessionOwnership(sessionId: string, locks?: LockManagerLike | null): Promise<SessionOwnership> {
  const manager = lockManager(locks);
  if (!manager) return Promise.resolve({ owned: true, release: () => undefined });
  return new Promise<SessionOwnership>((resolve) => {
    let release = () => undefined as void;
    const held = new Promise<void>((done) => { release = done; });
    manager.request(`fs-workflow-edit-session:${sessionId}`, { ifAvailable: true }, (lock) => {
      if (!lock) {
        resolve({ owned: false, release: () => undefined });
        return undefined;
      }
      resolve({ owned: true, release });
      return held;
    }).catch(() => resolve({ owned: true, release: () => undefined }));
  });
}
