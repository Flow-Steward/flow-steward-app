import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { filterStorageKey } from "./use-filter-state";
import { useFilterSearchParams, useLatestFilterSearchParams } from "./use-filter-search-params";

const KEYS = ["status", "project_id"];
function wrapperAt(path: string) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <AuthContext.Provider value={{ session: { user: { id: "user-1", account_id: "account-1" } } } as AuthContextValue}><MemoryRouter initialEntries={[path]}>{children}</MemoryRouter></AuthContext.Provider>;
  };
}
function save(query: string) {
  localStorage.setItem(filterStorageKey("account-1", "user-1", "query/jobs"), JSON.stringify(query));
}

it("restores repeated filters before rendering and preserves unrelated URL state and hash", () => {
  save("status=failed&status=waiting&project_id=p1&page=9&dialog=delete");
  const view = renderHook(() => ({ params: useFilterSearchParams("jobs", KEYS)[0], location: useLocation() }), { wrapper: wrapperAt("/jobs?tab=history#list") });
  expect(view.result.current.params.getAll("status")).toEqual(["failed", "waiting"]);
  expect(view.result.current.params.get("project_id")).toBe("p1");
  expect(view.result.current.params.get("tab")).toBe("history");
  expect(view.result.current.params.has("page")).toBe(false);
  expect(view.result.current.params.has("dialog")).toBe(false);
  expect(view.result.current.location.hash).toBe("#list");
});

it("uses explicit filter links as the complete filter selection", () => {
  save("status=failed&project_id=p1");
  const view = renderHook(() => useFilterSearchParams("jobs", KEYS), { wrapper: wrapperAt("/jobs?project_id=p2") });
  expect(view.result.current[0].get("project_id")).toBe("p2");
  expect(view.result.current[0].has("status")).toBe(false);
});

it("keeps cleared filters cleared after reload and back navigation", () => {
  save("status=failed");
  const view = renderHook(() => ({ filters: useFilterSearchParams("jobs", KEYS), navigate: useNavigate() }), { wrapper: wrapperAt("/jobs") });
  act(() => view.result.current.filters[1](new URLSearchParams()));
  expect(view.result.current.filters[0].has("status")).toBe(false);
  view.unmount();
  const utils = renderHook(() => useFilterSearchParams("jobs", KEYS), { wrapper: wrapperAt("/jobs") });
  expect(utils.result.current[0].has("status")).toBe(false);
});

it("supports latest-query updates without dropping two selections in the same event", () => {
  const view = renderHook(() => useLatestFilterSearchParams("jobs", KEYS), { wrapper: wrapperAt("/jobs#list") });
  act(() => {
    view.result.current[1]((params) => { params.set("status", "failed"); return params; });
    view.result.current[1]((params) => { params.set("project_id", "p1"); return params; });
  });
  expect(view.result.current[0].get("status")).toBe("failed");
  expect(view.result.current[0].get("project_id")).toBe("p1");
});

it("honors back navigation without restoring filters over the historical URL", () => {
  const view = renderHook(() => ({ filters: useFilterSearchParams("jobs", KEYS), navigate: useNavigate() }), { wrapper: wrapperAt("/jobs") });
  act(() => view.result.current.filters[1]({ status: "failed" }));
  expect(view.result.current.filters[0].get("status")).toBe("failed");
  act(() => { void view.result.current.navigate(-1); });
  expect(view.result.current.filters[0].has("status")).toBe(false);
});


it("restores a catalog only when its tab becomes active and keeps its selection while inactive", () => {
  save("status=failed");
  const view = renderHook(({ active }) => useLatestFilterSearchParams("jobs", KEYS, active), { wrapper: wrapperAt("/jobs"), initialProps: { active: false } });
  expect(view.result.current[0].has("status")).toBe(false);
  view.rerender({ active: true });
  expect(view.result.current[0].get("status")).toBe("failed");
  view.rerender({ active: false });
  act(() => view.result.current[1]((params) => { params.delete("status"); return params; }));
  expect(localStorage.getItem(filterStorageKey("account-1", "user-1", "query/jobs"))).toBe(JSON.stringify("status=failed"));
  view.rerender({ active: true });
  expect(view.result.current[0].get("status")).toBe("failed");
});


it("switches account preferences without saving the old account URL as the new selection", () => {
  let accountId = "account-1";
  localStorage.setItem(filterStorageKey("account-2", "user-1", "query/jobs"), JSON.stringify("project_id=p2"));
  function Wrapper({ children }: { children: ReactNode }) {
    return <AuthContext.Provider value={{ session: { user: { id: "user-1", account_id: accountId } } } as AuthContextValue}><MemoryRouter initialEntries={["/jobs?status=failed"]}>{children}</MemoryRouter></AuthContext.Provider>;
  }
  const view = renderHook(() => useFilterSearchParams("jobs", KEYS), { wrapper: Wrapper });
  expect(view.result.current[0].get("status")).toBe("failed");
  accountId = "account-2";
  view.rerender();
  expect(view.result.current[0].get("project_id")).toBe("p2");
  expect(view.result.current[0].has("status")).toBe(false);
});

it("restores the new account selection after switching accounts on an inactive tab", () => {
  let accountId = "account-1";
  localStorage.setItem(filterStorageKey("account-2", "user-1", "query/jobs"), JSON.stringify("project_id=p2"));
  function Wrapper({ children }: { children: ReactNode }) {
    return <AuthContext.Provider value={{ session: { user: { id: "user-1", account_id: accountId } } } as AuthContextValue}><MemoryRouter initialEntries={["/jobs?status=failed"]}>{children}</MemoryRouter></AuthContext.Provider>;
  }
  const view = renderHook(({ active }) => useLatestFilterSearchParams("jobs", KEYS, active), { wrapper: Wrapper, initialProps: { active: true } });
  view.rerender({ active: false });
  accountId = "account-2";
  view.rerender({ active: false });
  act(() => view.result.current[1]((params) => { params.set("tab", "catalog"); return params; }));
  view.rerender({ active: true });
  expect(view.result.current[0].get("project_id")).toBe("p2");
  expect(view.result.current[0].has("status")).toBe(false);
  expect(view.result.current[0].get("tab")).toBe("catalog");
  expect(localStorage.getItem(filterStorageKey("account-2", "user-1", "query/jobs"))).toBe(JSON.stringify("project_id=p2"));
});

it.each(["b-explicit", ""])("honors a new explicit selection while the new account tab is inactive (%s)", (projectId) => {
  let accountId = "account-1";
  localStorage.setItem(filterStorageKey("account-2", "user-1", "query/jobs"), JSON.stringify("project_id=p2"));
  function Wrapper({ children }: { children: ReactNode }) {
    return <AuthContext.Provider value={{ session: { user: { id: "user-1", account_id: accountId } } } as AuthContextValue}><MemoryRouter initialEntries={["/jobs?status=failed"]}>{children}</MemoryRouter></AuthContext.Provider>;
  }
  const view = renderHook(({ active }) => useLatestFilterSearchParams("jobs", KEYS, active), { wrapper: Wrapper, initialProps: { active: true } });
  view.rerender({ active: false });
  accountId = "account-2";
  view.rerender({ active: false });
  act(() => view.result.current[1]((params) => {
    params.delete("status");
    params.delete("project_id");
    params.set("project_id", projectId);
    return params;
  }));
  view.rerender({ active: true });
  expect(view.result.current[0].get("project_id")).toBe(projectId);
  expect(view.result.current[0].has("status")).toBe(false);
  expect(localStorage.getItem(filterStorageKey("account-2", "user-1", "query/jobs"))).toBe(JSON.stringify(`project_id=${projectId}`));
});

it.each(["b-explicit", ""])("honors an explicit link that also activates the new account tab (%s)", (projectId) => {
  let accountId = "account-1";
  localStorage.setItem(filterStorageKey("account-2", "user-1", "query/jobs"), JSON.stringify("project_id=p2"));
  function Wrapper({ children }: { children: ReactNode }) {
    return <AuthContext.Provider value={{ session: { user: { id: "user-1", account_id: accountId } } } as AuthContextValue}><MemoryRouter initialEntries={["/jobs?status=failed"]}>{children}</MemoryRouter></AuthContext.Provider>;
  }
  const view = renderHook(() => ({
    filters: useLatestFilterSearchParams("jobs", KEYS, !new URLSearchParams(useLocation().search).has("tab")),
    navigate: useNavigate(),
  }), { wrapper: Wrapper });
  act(() => { void view.result.current.navigate("/jobs?tab=catalog&status=failed"); });
  accountId = "account-2";
  view.rerender();
  act(() => { void view.result.current.navigate(`/jobs?project_id=${projectId}`); });
  expect(view.result.current.filters[0].get("project_id")).toBe(projectId);
  expect(view.result.current.filters[0].has("status")).toBe(false);
});
