import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider } from "react-router";

import { gridColumnLink, gridLinkHref, inAppPath, withPageParameter } from "@/pages/extensions/extension-grid-links";
import type { ManifestRenderContext } from "@/pages/extensions/extension-manifest-render-model";
import { renderManifestComponent } from "@/pages/extensions/extension-manifest-renderer";

const SEARCH = "?project_id=p-1&extension_connection=c-1&extension_page=runs&p.status=open&other=x";

describe("grid links", () => {
  it("builds a link to another page of the extension with encoded row values", () => {
    const href = gridLinkHref(
      { page: "run_detail", params: { run_id: "{row.run_id}", note: "id {row.meta.kind}" } },
      { run_id: "a b&c", meta: { kind: "x/y" } },
      { extensionId: "acme.store", search: SEARCH },
    );

    const url = new URL(href, "https://app.test");
    expect(url.pathname).toBe("/extensions/acme.store");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      project_id: "p-1",
      extension_connection: "c-1",
      extension_page: "run_detail",
      "p.run_id": "a b&c",
      "p.note": "id x/y",
    });
    expect(href).toContain("p.run_id=a+b%26c");
  });

  it("gives no link when the row cannot fill a parameter", () => {
    expect(gridLinkHref({ page: "run_detail", params: { run_id: "{row.run_id}" } }, {}, { extensionId: "acme.store", search: "" })).toBe("");
  });

  it("follows only same-app paths a row provides", () => {
    expect(inAppPath("/jobs/42")).toBe("/jobs/42");
    for (const unsafe of ["https://evil.test/x", "javascript:alert(1)", "//evil.test", "/\\evil.test", "/jobs/4 2", "jobs/42", ""]) {
      expect(inAppPath(unsafe)).toBe("");
    }
    expect(gridLinkHref({ path_field: "job" }, { job: "/jobs/7" }, { extensionId: "e", search: "" })).toBe("/jobs/7");
    // The app's own base belongs to the router.
    vi.stubEnv("BASE_URL", "/app/");
    expect(inAppPath("/app/jobs/7")).toBe("/jobs/7");
    vi.unstubAllEnvs();
  });

  it("reads a link column without a link declaration as an in-app path column", () => {
    expect(gridColumnLink({ key: "job", type: "link" })).toEqual({ path_field: "job" });
    expect(gridColumnLink({ key: "job", type: "text" })).toBeNull();
  });

  it("sets and clears one page parameter in the address", () => {
    expect(withPageParameter("?extension_page=runs&p.status=open", "status", "done")).toBe("?extension_page=runs&p.status=done");
    expect(withPageParameter("?extension_page=runs&p.status=open", "status", "")).toBe("?extension_page=runs");
  });
});

function context(overrides: Partial<ManifestRenderContext> = {}): ManifestRenderContext {
  return {
    busy: false,
    canManageProject: true,
    connectionCreationBlockedReason: "",
    callbacks: [],
    callbackStatuses: {},
    componentErrors: {},
    componentQueryParams: {},
    componentQueryData: {
      runs: { items: [{ run_id: "r-1", name: "First run", job_path: "/jobs/9" }], total: 1 },
    },
    extensionId: "acme.store",
    extensionInstallId: "inst-1",
    extensionReleaseId: "rel-1",
    extensionDescriptorSha256: "",
    formValuesByComponent: {},
    oauthConnectionTypes: [],
    projectId: "p-1",
    projectConnections: [],
    projectState: [],
    onCallbackSetup: vi.fn(),
    onComponentQueryParamsChange: vi.fn(),
    onFieldChange: vi.fn(),
    onExternalResourcePickerBootstrap: vi.fn(),
    onManifestAction: vi.fn(),
    onConnectionSave: vi.fn(),
    onConnectionDelete: vi.fn(),
    onConnectionOAuth: vi.fn(),
    onConnectionOAuthDisconnect: vi.fn(),
    onConnectionOAuthDisconnectConfirm: vi.fn(),
    onConnectionOAuthDisconnectCancel: vi.fn(),
    onConnectionSelect: vi.fn(),
    onProjectStateSave: vi.fn(),
    onProjectStateDelete: vi.fn(),
    onProjectStateSelect: vi.fn(),
    ...overrides,
  } as ManifestRenderContext;
}

const GRID = {
  component_id: "runs",
  type: "grid",
  title: "Runs",
  columns: [
    { key: "name", label: "Run", type: "link", link: { page: "run_detail", params: { run_id: "{row.run_id}" } } },
    { key: "job_path", label: "Job", type: "link" },
  ],
  row_link: { page: "run_detail", params: { run_id: "{row.run_id}" } },
  filters: [
    { param: "status", label: "Status", type: "select", options: [{ value: "open", label: "Open" }, { value: "done", label: "Done" }] },
    { param: "q", label: "Search", type: "text" },
  ],
};

function mountGrid(renderContext: ManifestRenderContext, entry = "/extensions/acme.store?project_id=p-1&extension_page=runs") {
  const router = createMemoryRouter(
    [
      { path: "/extensions/:extensionId", element: <>{renderManifestComponent(GRID, renderContext)}</> },
      { path: "/jobs/:jobId", element: <p>Job page</p> },
    ],
    { initialEntries: [entry] },
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return router;
}

describe("grid with links and filters", () => {
  it("links a cell to another page of the extension and opens it in the app", () => {
    const router = mountGrid(context());

    const link = screen.getByRole("link", { name: "First run" });
    expect(link).toHaveAttribute("href", expect.stringContaining("extension_page=run_detail"));
    expect(link).toHaveAttribute("href", expect.stringContaining("p.run_id=r-1"));
    fireEvent.click(link);
    expect(router.state.location.search).toContain("p.run_id=r-1");
  });

  it("opens the row's target when the row is clicked outside its links", () => {
    const router = mountGrid(context());

    fireEvent.click(screen.getByRole("cell", { name: "First run" }));
    expect(router.state.location.search).toContain("extension_page=run_detail");
  });

  it("follows a row's in-app path without leaving the app", () => {
    const router = mountGrid(context());

    fireEvent.click(screen.getByRole("link", { name: "/jobs/9" }));
    expect(router.state.location.pathname).toBe("/jobs/9");
  });

  it("keeps a filter in the address and returns the grid to its first page", () => {
    const onComponentQueryParamsChange = vi.fn();
    const router = mountGrid(context({ onComponentQueryParamsChange }));

    fireEvent.click(screen.getByRole("combobox", { name: "Status" }));
    fireEvent.click(screen.getByRole("option", { name: "Done" }));
    expect(router.state.location.search).toContain("p.status=done");
    expect(onComponentQueryParamsChange).toHaveBeenCalledWith("runs", expect.objectContaining({ page: 1 }));

    const search = screen.getByRole("searchbox", { name: "Search" });
    fireEvent.change(search, { target: { value: "north" } });
    fireEvent.keyDown(search, { key: "Enter" });
    expect(router.state.location.search).toContain("p.q=north");
    expect(router.state.location.search).toContain("p.status=done");
  });
});
