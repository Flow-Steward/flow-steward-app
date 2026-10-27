/**
 * Test doubles for the edit session's browser storage: a minimal IndexedDB
 * (one transaction at a time, structured-clone copies, an optional quota
 * failure), a copyable sessionStorage, and a Web Locks manager. A real browser
 * run is still required for reload and duplicate-tab evidence; these only make
 * the session logic observable in jsdom.
 */

type Store = Map<string, unknown>;
type DatabaseState = { stores: Map<string, Store>; keyPath: string };
type Handler = (() => void) | null;

export function fakeIndexedDB(options: { failWrites?: boolean } = {}) {
  const databases = new Map<string, DatabaseState>();
  const control = { failWrites: Boolean(options.failWrites), writes: 0 };

  function database(state: DatabaseState) {
    return {
      objectStoreNames: { contains: (name: string) => state.stores.has(name) },
      createObjectStore(name: string, settings: { keyPath: string }) {
        state.stores.set(name, new Map());
        state.keyPath = settings.keyPath;
      },
      onversionchange: null as Handler,
      close() {},
      transaction(storeName: string) {
        const operations: Array<() => void> = [];
        const transaction = { error: null as unknown, oncomplete: null as Handler, onerror: null as Handler, onabort: null as Handler, objectStore: () => store };
        const request = (run: (target: { result?: unknown }) => void) => {
          const target: { result?: unknown; onsuccess: Handler; onerror: Handler } = { onsuccess: null, onerror: null };
          operations.push(() => { run(target); target.onsuccess?.(); });
          return target;
        };
        const records = () => state.stores.get(storeName)!;
        const store = {
          put: (value: Record<string, unknown>) => request((target) => {
            if (control.failWrites) throw new DOMException("The quota was exceeded.", "QuotaExceededError");
            control.writes += 1;
            records().set(String(value[state.keyPath]), structuredClone(value));
            target.result = value[state.keyPath];
          }),
          get: (key: string) => request((target) => {
            const value = records().get(key);
            target.result = value === undefined ? undefined : structuredClone(value);
          }),
          delete: (key: string) => request(() => { records().delete(key); }),
        };
        setTimeout(() => {
          try {
            operations.forEach((operation) => operation());
            transaction.oncomplete?.();
          } catch (error) {
            transaction.error = error;
            transaction.onerror?.();
            transaction.onabort?.();
          }
        }, 0);
        return transaction;
      },
    };
  }

  const factory = {
    open(name: string) {
      const request = { result: null as unknown, error: null, onsuccess: null as Handler, onerror: null as Handler, onupgradeneeded: null as Handler, onblocked: null as Handler };
      setTimeout(() => {
        let state = databases.get(name);
        const created = !state;
        state ??= { stores: new Map(), keyPath: "" };
        databases.set(name, state);
        request.result = database(state);
        if (created) request.onupgradeneeded?.();
        request.onsuccess?.();
      }, 0);
      return request;
    },
    deleteDatabase(name: string) {
      databases.delete(name);
      return {};
    },
  };

  return {
    factory: factory as unknown as IDBFactory,
    control,
    /** A stored record as the browser would hand it back, or undefined. */
    record: (sessionId: string) => {
      for (const state of databases.values()) {
        for (const records of state.stores.values()) {
          if (records.has(sessionId)) return structuredClone(records.get(sessionId)) as Record<string, unknown>;
        }
      }
      return undefined;
    },
    recordIds: () => [...databases.values()].flatMap((state) => [...state.stores.values()].flatMap((records) => [...records.keys()])),
    put: (sessionId: string, value: unknown) => {
      for (const state of databases.values()) state.stores.get("sessions")?.set(sessionId, structuredClone(value));
    },
  };
}

/** A sessionStorage whose copy is what a duplicated tab gets. */
export function fakeTabStorage(initial?: Map<string, string>) {
  const values = new Map(initial ?? []);
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
    duplicate: () => fakeTabStorage(new Map(values)),
  };
}

/** The Web Locks API for one browser: a held name is not available to another tab. */
export function fakeLocks() {
  const held = new Set<string>();
  return {
    held,
    request(name: string, _options: { ifAvailable: boolean }, callback: (lock: unknown) => Promise<void> | void) {
      if (held.has(name)) return Promise.resolve(callback(null));
      held.add(name);
      return Promise.resolve(callback({ name })).finally(() => held.delete(name));
    },
  };
}
