import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import {
  isInAppLinkPath,
  normalizeResourceReportSurface,
  reportLinkHref,
  type ResourceReportSurface,
} from "@/pages/settings/hub/resource-report";
import { ResourceReportPanel } from "@/pages/settings/hub/resource-report-panel";

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

const mockedFetchJson = vi.mocked(fetchJson);

const LIST_PATH = "/api/modules/acme.journal/journal";

function declaration(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    kind: "resource_report",
    list_path: LIST_PATH,
    items_key: "items",
    total_key: "total",
    columns: [
      { key: "decided_at", label: "Decided at", type: "datetime" },
      { key: "workflow_name", label: "Workflow", type: "text" },
      { key: "action", label: "Action", type: "badge" },
      { key: "job_label", label: "Job", type: "link", link_path: "/jobs/{job_id}" },
      { key: "target_label", label: "Target", type: "link", link_key: "target" },
    ],
    filters: [
      { key: "workflow_id", label: "Workflow", type: "text" },
      { key: "date_from", label: "Date from", type: "date" },
      { key: "action", label: "Action", type: "select", options: [{ value: "approve", label: "Approved" }] },
    ],
    page_size: 2,
    empty_text: "No approval decisions are recorded yet.",
    ...overrides,
  };
}

function surfaceOf(overrides: Record<string, unknown> = {}): ResourceReportSurface {
  const surface = normalizeResourceReportSurface(declaration(overrides));
  if (!surface) throw new Error("the fixture declaration must be valid");
  return surface;
}

function renderPanel(surface: ResourceReportSurface = surfaceOf()) {
  return render(
    <MemoryRouter>
      <ResourceReportPanel surface={surface} title="Approval journal" />
    </MemoryRouter>,
  );
}

function table(): HTMLElement {
  return screen.getByRole("table", { name: "Approval journal" });
}

function requestedUrls(): URL[] {
  return mockedFetchJson.mock.calls.map(([input]) => new URL(String(input), "http://app.test"));
}

const ROW = {
  id: "entry-1",
  decided_at: "2026-11-25T09:30:00Z",
  workflow_name: "Invoice approval",
  action: "approve",
  job_id: "job-1",
  job_label: "Open job",
  target: "/projects/p-1",
  target_label: "Project",
};

describe("ResourceReportPanel", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
    mockedFetchJson.mockResolvedValue({ items: [ROW], total: 1 });
  });

  it("renders the declared columns and the rows, read only", async () => {
    renderPanel();

    await waitFor(() => expect(within(table()).getByText("Invoice approval")).toBeInTheDocument());
    for (const header of ["Decided at", "Workflow", "Action", "Job", "Target"]) {
      expect(within(table()).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    // A datetime column is formatted for the reader, not shown as the raw ISO value.
    expect(within(table()).queryByText(ROW.decided_at)).not.toBeInTheDocument();
    // Read only: no write controls at all.
    for (const name of ["Add", "Edit", "Delete"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });

  it("counts the records in the reader's words: one record, then a number of records", async () => {
    const view = renderPanel();
    await waitFor(() => expect(screen.getByText("1 record")).toBeInTheDocument());
    expect(screen.queryByText("1 records")).not.toBeInTheDocument();
    view.unmount();

    mockedFetchJson.mockResolvedValue({ items: [ROW, { ...ROW, id: "entry-2" }], total: 5 });
    renderPanel();
    await waitFor(() => expect(screen.getByText("5 records")).toBeInTheDocument());
  });

  it("sends limit and offset, pages, and sends applied filters by key", async () => {
    mockedFetchJson.mockResolvedValue({ items: [ROW, { ...ROW, id: "entry-2" }], total: 5 });
    const user = userEvent.setup();
    renderPanel();

    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledTimes(1));
    const [first] = requestedUrls();
    expect(first?.pathname).toBe(LIST_PATH);
    expect(Object.fromEntries(first?.searchParams ?? [])).toEqual({ limit: "2", offset: "0" });

    await waitFor(() => expect(screen.getByRole("button", { name: "Next page" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(requestedUrls().at(-1)?.searchParams.get("offset")).toBe("2"));

    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Workflow" }));
    const menu = await screen.findByRole("menu", { name: "Choose a filter" });
    fireEvent.change(within(menu).getByLabelText("Workflow"), { target: { value: "wf-1" } });
    await user.click(within(menu).getByRole("button", { name: "Apply" }));

    await waitFor(() =>
      expect(Object.fromEntries(requestedUrls().at(-1)?.searchParams ?? [])).toEqual({
        workflow_id: "wf-1",
        limit: "2",
        // A new filter starts again from the first page.
        offset: "0",
      }),
    );

    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Date from" }));
    const dateMenu = await screen.findByRole("menu", { name: "Choose a filter" });
    fireEvent.change(within(dateMenu).getByLabelText("Date from"), { target: { value: "2026-11-01" } });
    await user.click(within(dateMenu).getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(requestedUrls().at(-1)?.searchParams.get("date_from")).toBe("2026-11-01"));
    expect(requestedUrls().at(-1)?.searchParams.get("workflow_id")).toBe("wf-1");
  });

  it("shows the declared empty text when nothing matches", async () => {
    mockedFetchJson.mockResolvedValue({ items: [], total: 0 });
    renderPanel();

    await waitFor(() => expect(screen.getByText("No approval decisions are recorded yet.")).toBeInTheDocument());
  });

  it("shows the page-unavailable state when the account is not entitled", async () => {
    mockedFetchJson.mockRejectedValue(
      new HttpError({
        status: 403,
        url: LIST_PATH,
        payload: {
          code: "feature_not_entitled",
          message: "This account is not entitled to the feature 'approval_journal'",
          details: { feature: "approval_journal" },
        },
      }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByText("This page is not available.")).toBeInTheDocument());
    expect(screen.getByText("Page unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows any other failure as a warning beside the table", async () => {
    mockedFetchJson.mockRejectedValue(new HttpError({ status: 500, url: LIST_PATH, payload: { message: "Journal store is down" } }));
    renderPanel();

    await waitFor(() => expect(screen.getByText("Journal store is down")).toBeInTheDocument());
    expect(screen.queryByText("This page is not available.")).not.toBeInTheDocument();
  });

  it("draws only in-app relative links", async () => {
    mockedFetchJson.mockResolvedValue({
      items: [
        ROW,
        { ...ROW, id: "entry-2", job_id: "../admin", job_label: "Odd job", target: "https://evil.example/x", target_label: "Elsewhere" },
        { ...ROW, id: "entry-3", job_id: "", job_label: "No job", target: "//evil.example/x", target_label: "Protocol relative" },
        { ...ROW, id: "entry-4", job_id: "job-4", job_label: "Job four", target: "/api/modules/acme.journal/journal", target_label: "Api" },
      ],
      total: 4,
    });
    renderPanel();

    await waitFor(() => expect(within(table()).getByText("Odd job")).toBeInTheDocument());
    const hrefs = within(table()).getAllByRole("link").map((link) => link.getAttribute("href") ?? "");
    expect(hrefs).toHaveLength(3);
    expect(hrefs.filter((href) => href.endsWith("/jobs/job-1"))).toHaveLength(1);
    expect(hrefs.filter((href) => href.endsWith("/jobs/job-4"))).toHaveLength(1);
    expect(hrefs.filter((href) => href.endsWith("/projects/p-1"))).toHaveLength(1);
    for (const href of hrefs) {
      expect(href.startsWith("/")).toBe(true);
      expect(href.startsWith("//")).toBe(false);
    }
    for (const label of ["Odd job", "No job", "Elsewhere", "Protocol relative", "Api"]) {
      expect(within(table()).getByText(label)).toBeInTheDocument();
      expect(within(table()).queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
  });

  it("shows a plain label and the missing marker when link_if turns the link off", async () => {
    const surface = surfaceOf({
      columns: [
        { key: "workflow_name", label: "Workflow", type: "text" },
        {
          key: "job_id",
          label: "Job",
          type: "link",
          link_path: "/jobs/{job_id}",
          link_if: "job_exists",
          link_missing_label: "Deleted",
        },
      ],
    });
    mockedFetchJson.mockResolvedValue({
      items: [
        { ...ROW, id: "entry-1", job_id: "job-1", job_exists: true },
        { ...ROW, id: "entry-2", job_id: "job-2", job_exists: false },
        { ...ROW, id: "entry-3", job_id: "job-3" },
      ],
      total: 3,
    });
    renderPanel(surface);

    await waitFor(() => expect(within(table()).getByText("job-2")).toBeInTheDocument());
    const hrefs = within(table()).getAllByRole("link").map((link) => link.getAttribute("href") ?? "");
    expect(hrefs).toHaveLength(1);
    expect(hrefs[0]).toMatch(/\/jobs\/job-1$/);
    for (const label of ["job-2", "job-3"]) {
      expect(within(table()).queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
    expect(within(table()).getAllByText("Deleted")).toHaveLength(2);
  });

  it("drops a declared link column that leaves the app", () => {
    const surface = surfaceOf({
      columns: [
        { key: "action", label: "Action", type: "text" },
        { key: "job", label: "Outside", type: "link", link_path: "https://evil.example/jobs/{job_id}" },
        { key: "api", label: "Api", type: "link", link_path: "/api/jobs/{job_id}" },
      ],
    });

    expect(surface.columns.map((column) => column.key)).toEqual(["action"]);
  });
});

describe("resource_report declaration checks", () => {
  it.each([
    ["/jobs/{job_id}", true],
    ["/jobs/job-1", true],
    ["https://evil.example/jobs", false],
    ["//evil.example/jobs", false],
    ["javascript:alert(1)", false],
    ["jobs/1", false],
    ["/jobs/../admin", false],
    ["/api/jobs/1", false],
    ["/jobs/1?tab=x", false],
    ["/jobs/{job_id", false],
  ])("treats the link template %s as in-app: %s", (path, expected) => {
    expect(isInAppLinkPath(path, { template: true })).toBe(expected);
  });

  it("fills a link template from the row and refuses a row without the value", () => {
    const [column] = surfaceOf({ columns: [{ key: "job", label: "Job", type: "link", link_path: "/jobs/{job_id}" }] }).columns;
    expect(column).toBeDefined();
    if (!column) return;
    expect(reportLinkHref(column, { job_id: "a b/c" })).toMatch(/\/jobs\/a%20b%2Fc$/);
    expect(reportLinkHref(column, { job_id: "" })).toBeNull();
  });

  it("keeps link_if on link columns only and needs it for the missing label", () => {
    const [link, text, unconditional] = surfaceOf({
      columns: [
        { key: "job", label: "Job", type: "link", link_path: "/jobs/{job_id}", link_if: "job_exists", link_missing_label: "Gone" },
        { key: "note", label: "Note", type: "text", link_if: "job_exists", link_missing_label: "Gone" },
        { key: "other", label: "Other", type: "link", link_path: "/jobs/{job_id}", link_missing_label: "Gone" },
      ],
    }).columns;
    expect(link).toMatchObject({ linkIf: "job_exists", linkMissingLabel: "Gone" });
    expect(text).toMatchObject({ linkIf: "", linkMissingLabel: "" });
    expect(unconditional).toMatchObject({ linkIf: "", linkMissingLabel: "" });
    if (!link) return;
    expect(reportLinkHref(link, { job_id: "job-1", job_exists: true })).toMatch(/\/jobs\/job-1$/);
    expect(reportLinkHref(link, { job_id: "job-1", job_exists: false })).toBeNull();
    expect(reportLinkHref(link, { job_id: "job-1" })).toBeNull();
    const badKey = { key: "job", label: "Job", type: "link", link_path: "/jobs/{job_id}", link_if: "bad-key" };
    expect(normalizeResourceReportSurface(declaration({ columns: [badKey] }))).toBeUndefined();
  });

  it.each([
    [{ list_path: "/private/journal" }],
    [{ list_path: "//evil.example/api/journal" }],
    [{ list_path: "/api/modules/acme.journal/journal?limit=5" }],
    [{ columns: [] }],
    [{ columns: [{ key: "a", label: "A", type: "html" }] }],
    [{ kind: "resource_admin" }],
  ])("refuses an unusable declaration %j", (overrides) => {
    expect(normalizeResourceReportSurface(declaration(overrides))).toBeUndefined();
  });

  it("keeps paging keys out of the filters and bounds the page size", () => {
    const surface = surfaceOf({
      filters: [{ key: "limit", label: "Limit", type: "text" }, { key: "decided_by", label: "Decided by" }],
      page_size: 10_000,
    });
    expect(surface.filters.map((item) => item.key)).toEqual(["decided_by"]);
    expect(surface.pageSize).toBe(50);
  });
});
