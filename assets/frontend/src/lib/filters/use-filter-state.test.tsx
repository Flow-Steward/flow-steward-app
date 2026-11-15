import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { z } from "@/lib/validation/zod";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { filterStorageKey, useFilterState } from "./use-filter-state";

function wrapper({ children }: { children: ReactNode }) {
  return <AuthContext.Provider value={{ session: { user: { id: "user-1", account_id: "account-1" } } } as AuthContextValue}>{children}</AuthContext.Provider>;
}

it("restores filters before first render after remount and persists reset", () => {
  const view = renderHook(() => useFilterState("jobs/status", z.string(), ""), { wrapper });
  act(() => view.result.current[1]("failed"));
  view.unmount();
  const utils = renderHook(() => useFilterState("jobs/status", z.string(), ""), { wrapper });
  expect(utils.result.current[0]).toBe("failed");
  act(() => utils.result.current[1](""));
  utils.unmount();
  const { result: thirdResult } = renderHook(() => useFilterState("jobs/status", z.string(), ""), { wrapper });
  expect(thirdResult.current[0]).toBe("");
});

it("supports functional updates and structured filters", () => {
  const schema = z.record(z.string(), z.string());
  const view = renderHook(() => useFilterState("registry/filters", schema, {}), { wrapper });
  act(() => view.result.current[1]((current) => ({ ...current, status: "enabled" })));
  view.unmount();
  const utils = renderHook(() => useFilterState("registry/filters", schema, {}), { wrapper });
  expect(utils.result.current[0]).toEqual({ status: "enabled" });
});

it("separates page/entity scopes and switches them without leaking the previous value", () => {
  const view = renderHook(({ scope }) => useFilterState(scope, z.string(), ""), { wrapper, initialProps: { scope: "project/a" } });
  act(() => view.result.current[1]("a-filter"));
  view.rerender({ scope: "project/b" });
  expect(view.result.current[0]).toBe("");
  act(() => view.result.current[1]("b-filter"));
  view.rerender({ scope: "project/a" });
  expect(view.result.current[0]).toBe("a-filter");
});

it("separates users and accounts", () => {
  expect(filterStorageKey("a", "u1", "jobs")).not.toBe(filterStorageKey("a", "u2", "jobs"));
  expect(filterStorageKey("a", "u1", "jobs")).not.toBe(filterStorageKey("b", "u1", "jobs"));
});

it.each(["broken JSON", '42', 'null', '{"status":42}'])("ignores malformed or obsolete saved state: %s", (raw) => {
  localStorage.setItem(filterStorageKey("account-1", "user-1", "jobs/status"), raw);
  const view = renderHook(() => useFilterState("jobs/status", z.string(), ""), { wrapper });
  expect(view.result.current[0]).toBe("");
});

it("validates enum values", () => {
  localStorage.setItem(filterStorageKey("account-1", "user-1", "status"), JSON.stringify("retired"));
  const view = renderHook(() => useFilterState("status", z.enum(["all", "active"]), "all"), { wrapper });
  expect(view.result.current[0]).toBe("all");
});

it("continues filtering when storage reads and writes fail", () => {
  const read = vi.spyOn(localStorage, "getItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
  const write = vi.spyOn(localStorage, "setItem").mockImplementation(() => { throw new DOMException("Full", "QuotaExceededError"); });
  const view = renderHook(() => useFilterState("jobs/status", z.string(), ""), { wrapper });
  act(() => view.result.current[1]("failed"));
  expect(view.result.current[0]).toBe("failed");
  read.mockRestore();
  write.mockRestore();
});

it("does not persist without an authenticated identity", () => {
  const view = renderHook(() => useFilterState("jobs/status", z.string(), ""));
  act(() => view.result.current[1]("failed"));
  expect(localStorage.length).toBe(0);
});


it("serializes only the schema-owned state, keeping transient menus closed", () => {
  const schema = z.object({ status: z.string(), menu: z.unknown().transform(() => null) });
  const view = renderHook(() => useFilterState<{ status: string; menu: string | null }>("search", schema, { status: "", menu: null }), { wrapper });
  act(() => view.result.current[1]({ status: "failed", menu: "add" }));
  expect(view.result.current[0].menu).toBe("add");
  expect(JSON.parse(localStorage.getItem(filterStorageKey("account-1", "user-1", "search")) || "null")).toEqual({ status: "failed", menu: null });
});


it("ignores a late filter update from a previous entity scope", () => {
  const view = renderHook(({ scope }) => useFilterState(scope, z.string(), ""), { wrapper, initialProps: { scope: "project/a" } });
  const previousSetter = view.result.current[1];
  view.rerender({ scope: "project/b" });
  act(() => previousSetter("old-scope-filter"));
  expect(view.result.current[0]).toBe("");
  expect(localStorage.getItem(filterStorageKey("account-1", "user-1", "project/b"))).toBe(JSON.stringify(""));
});
