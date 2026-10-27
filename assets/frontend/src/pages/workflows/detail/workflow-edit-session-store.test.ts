import {
  claimSessionOwnership,
  clearSessionReference,
  createWorkflowEditSessionStore,
  readSessionReference,
  writeSessionReference,
} from "./workflow-edit-session-store";
import { fakeIndexedDB, fakeLocks, fakeTabStorage } from "./workflow-edit-session.test-helpers";
import type { WorkflowEditSession } from "./workflow-edit-session";

const identity = { accountId: "account-a", userId: "user-a", workflowId: "wf" };
const record = (sessionId: string, editSequence = 1) => ({ sessionId, editSequence, schemaVersion: 1 }) as unknown as WorkflowEditSession;

describe("the edit session store", () => {
  it("is unavailable without IndexedDB", () => {
    expect(createWorkflowEditSessionStore(null)).toBeNull();
  });

  it("saves, loads and removes one record per session id as a copy", async () => {
    const browser = fakeIndexedDB();
    const store = createWorkflowEditSessionStore(browser.factory)!;
    const saved = record("one");
    await store.save(saved);
    await store.save(record("two"));
    const loaded = await store.load("one");
    expect(loaded).toEqual(saved);
    expect(loaded).not.toBe(saved);
    await store.remove("one");
    expect(await store.load("one")).toBeNull();
    expect(browser.recordIds()).toEqual(["two"]);
  });

  it("reports a failed transaction and keeps the last completed record", async () => {
    const browser = fakeIndexedDB();
    const store = createWorkflowEditSessionStore(browser.factory)!;
    await store.save(record("one", 1));
    browser.control.failWrites = true;
    await expect(store.save(record("one", 2))).rejects.toThrow(/quota/i);
    expect(browser.record("one")).toMatchObject({ editSequence: 1 });
  });
});

describe("this tab's session reference", () => {
  it("is kept per account, user and workflow", () => {
    const tab = fakeTabStorage();
    writeSessionReference(identity, "session-1", tab);
    expect(readSessionReference(identity, tab)).toBe("session-1");
    expect(readSessionReference({ ...identity, userId: "user-b" }, tab)).toBeNull();
    expect(readSessionReference({ ...identity, workflowId: "other" }, tab)).toBeNull();
    clearSessionReference(identity, tab);
    expect(readSessionReference(identity, tab)).toBeNull();
  });

  it("works without tab storage", () => {
    expect(readSessionReference(identity, null)).toBeNull();
    expect(() => writeSessionReference(identity, "x", null)).not.toThrow();
  });
});

describe("one owner per session", () => {
  it("tells a duplicated tab that the session already has an owner, until it is released", async () => {
    const locks = fakeLocks();
    const first = await claimSessionOwnership("session-1", locks);
    const second = await claimSessionOwnership("session-1", locks);
    expect(first.owned).toBe(true);
    expect(second.owned).toBe(false);
    first.release();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect((await claimSessionOwnership("session-1", locks)).owned).toBe(true);
  });

  it("owns its session where the browser has no Web Locks", async () => {
    expect((await claimSessionOwnership("session-1", null)).owned).toBe(true);
  });
});
