import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdvancedSearchDialog } from "@/components/search/advanced-search-dialog";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

const CONFIG = {
  default_category: "all",
  categories: [
    { value: "all", label: "Everything" },
    { value: "jobs", label: "Jobs / workflow runs" },
    { value: "events", label: "Events" },
    { value: "runtime_messages", label: "Runtime messages" },
    { value: "workflows", label: "Workflows" },
    { value: "kb", label: "Knowledge base" },
  ],
  group_labels: {
    jobs: "Jobs / workflow runs",
    runtime_messages: "Runtime messages",
    kb: "Knowledge base",
  },
  filters: {
    status: [
      { value: "queued", label: "Queued" },
      { value: "failed", label: "Failed" },
    ],
    updated: [
      { value: "24h", label: "Last 24 hours" },
      { value: "7d", label: "Last 7 days" },
    ],
  },
};

const JOB_ITEM = {
  group: "jobs",
  id: "4821",
  title: "Deploy production timed out",
  job_id: "4821",
  project_name: "Payments platform",
  deep_link: "/jobs/4821",
  timestamp: new Date().toISOString(),
  meta: { status: "failed", status_label: "Failed", status_tone: "red" },
};

const MESSAGE_ITEM = {
  group: "runtime_messages",
  id: "c-1",
  title: "Deployment timeout review",
  job_id: "4821",
  deep_link: "/jobs/4821",
};

function searchPayload(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    groups: [
      {
        group: "jobs",
        total: 5,
        items: [JOB_ITEM],
        has_more: true,
        maybe_more: true,
        next_cursor: "cursor-1",
      },
      {
        group: "runtime_messages",
        total: 1,
        items: [MESSAGE_ITEM],
        has_more: false,
        maybe_more: false,
        next_cursor: null,
      },
    ],
    best_match: JOB_ITEM,
    ...overrides,
  };
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "content-type": "application/json" },
    status: 200,
  });
}

let searchResponder: (url: string) => Response;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = stringFromUnknownValue(input);
      if (url.startsWith("/api/search/config")) {
        return jsonResponse(CONFIG);
      }
      if (url.startsWith("/api/projects")) {
        return jsonResponse({ projects: [{ id: "p-1", name: "Payments platform" }] });
      }
      if (url.startsWith("/api/search?")) {
        return searchResponder(url);
      }
      return new Response("{}", {
        headers: { "content-type": "application/json" },
        status: 200,
      });
    }),
  );
}

function searchUrls(): string[] {
  return vi
    .mocked(globalThis.fetch)
    .mock.calls.map(([input]) => stringFromUnknownValue(input))
    .filter((url) => url.startsWith("/api/search?"));
}

function renderDialog(props: Partial<Parameters<typeof AdvancedSearchDialog>[0]> = {}, locale: "en" | "lt" = "en") {
  const onOpenResult = vi.fn();
  const onClose = vi.fn();
  const messages: Record<string, string> = locale === "lt" ? { "shell.search.category.runtime_messages": "Vykdymo pranešimai", "react.shared.messages": "Pranešimai" } : {};
  const view = render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <AdvancedSearchDialog
      canSearchAccountWide
      onClose={onClose}
      onOpenResult={onOpenResult}
      open
      selectedWorkspaceId="ws-1"
      workspaceOptions={[
        { value: "ws-1", label: "Acme Operations" },
        { value: "ws-2", label: "Northwind Labs" },
      ]}
      {...props}
    />
    </I18nContext.Provider>,
  );
  return { ...view, onClose, onOpenResult };
}

async function typeQuery(value: string) {
  const combobox = screen.getByRole("combobox", { name: "Search Flow Steward" });
  fireEvent.change(combobox, { target: { value } });
  await vi.advanceTimersByTimeAsync(300);
  return combobox;
}

describe("Advanced Search dialog", () => {
  it("localizes the full accessible runtime-messages tab name as well as its shorter visible label", async () => {
    renderDialog({}, "lt");
    const tab = await screen.findByRole("tab", { name: "Vykdymo pranešimai" });
    expect(tab).toHaveTextContent("Pranešimai");
    expect(screen.queryByRole("tab", { name: "Runtime messages" })).not.toBeInTheDocument();
  });
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    searchResponder = () => jsonResponse(searchPayload());
    stubFetch();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("renders supported categories and ignores retired knowledge and chat categories", async () => {
    renderDialog();

    await waitFor(() => {
      expect(screen.getAllByRole("tab")).toHaveLength(5);
    });
    const tabs = screen.getAllByRole("tab").map((tab) => tab.textContent);
    expect(tabs?.[0]).toContain("Everything");
    expect(tabs?.[3]).toContain("Messages");
    // The visible label is shortened; the accessible name keeps the backend vocabulary.
    expect(screen.getByRole("tab", { name: "Runtime messages" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Knowledge base" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Chats" })).not.toBeInTheDocument();
  });

  it("never reports an empty result before a query completes", async () => {
    renderDialog();

    expect(screen.getByText("Search across Flow Steward")).toBeInTheDocument();
    expect(screen.queryByText("No matching results")).not.toBeInTheDocument();
  });

  it("debounces the query and scopes the request to the active workspace", async () => {
    renderDialog();
    const combobox = screen.getByRole("combobox", { name: "Search Flow Steward" });

    fireEvent.change(combobox, { target: { value: "d" } });
    fireEvent.change(combobox, { target: { value: "de" } });
    fireEvent.change(combobox, { target: { value: "deploy" } });
    // Nothing is sent while the user is still typing.
    expect(searchUrls()).toEqual([]);

    await vi.advanceTimersByTimeAsync(300);

    await waitFor(() => expect(searchUrls().length).toBeGreaterThan(0));
    const urls = searchUrls();
    expect(urls.every((url) => url.includes("query=deploy"))).toBe(true);
    expect(urls.every((url) => url.includes("workspace_id=ws-1"))).toBe(true);
  });

  it("marks a capped count as approximate and sums Everything", async () => {
    renderDialog();
    await typeQuery("deploy");

    await waitFor(() => {
      // jobs reported maybe_more without an exact total, so its count cannot read as one.
      expect(screen.getByRole("tab", { name: /Jobs/ })).toHaveTextContent("5+");
    });
    expect(screen.getByRole("tab", { name: /Everything/ })).toHaveTextContent("6+");
  });

  it("drops retired knowledge results and promoted links from an older server response", async () => {
    searchResponder = () =>
      jsonResponse(
        searchPayload({
          groups: [
            {
              group: "kb",
              total: 1,
              items: [
                {
                  group: "kb",
                  id: "kb-1",
                  title: "Retired knowledge result",
                  project_id: "p-1",
                  deep_link: "/projects/p-1/kb",
                },
              ],
            },
            {
              group: "jobs",
              total: 1,
              items: [JOB_ITEM],
            },
          ],
          best_match: {
            group: "kb",
            id: "kb-1",
            title: "Retired knowledge result",
            project_id: "p-1",
            deep_link: "/projects/p-1/kb",
          },
        }),
      );
    renderDialog();
    await typeQuery("retired");

    await screen.findByRole("option", { name: /Deploy production timed out/ });
    expect(screen.queryByText("Retired knowledge result")).not.toBeInTheDocument();
    expect(screen.queryByText("Best match")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Everything/ })).toHaveTextContent("1");
  });

  it("opens runtime messages at their jobs", async () => {
    searchResponder = () => jsonResponse(searchPayload({
      groups: [
        { group: "runtime_messages", total: 1, items: [MESSAGE_ITEM] },
      ],
      best_match: null,
    }));
    const onOpenResult = vi.fn();
    renderDialog({ onOpenResult });
    await typeQuery("deployment");

    fireEvent.click(await screen.findByRole("option", { name: /Deployment timeout review/ }));

    expect(onOpenResult).toHaveBeenCalledWith(expect.objectContaining({ group: "runtime_messages", deep_link: "/jobs/4821" }));
  });

  it("promotes the server's best match and renders that row once", async () => {
    renderDialog();
    await typeQuery("deploy");

    await screen.findByText("Best match");
    // The response repeats the promoted item inside its group; the surface must not.
    expect(screen.getAllByRole("option", { name: /Deploy production timed out/ })).toHaveLength(1);
    expect(screen.getByRole("option", { name: /Failed/ })).toBeInTheDocument();
  });

  it("offers Load more only inside a selected category", async () => {
    renderDialog();
    await typeQuery("deploy");
    await screen.findByText("Best match");

    // Everything routes to more through the tabs, not a continuation control.
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /Jobs/ }));

    await screen.findByRole("button", { name: "Load more" });
    // A selected category is a flat list: no group headings, no promoted row.
    expect(screen.queryByText("Best match")).not.toBeInTheDocument();
  });

  it("appends a page without replacing the rows already rendered", async () => {
    renderDialog();
    await typeQuery("deploy");
    fireEvent.click(screen.getByRole("tab", { name: /Jobs/ }));
    await screen.findByRole("button", { name: "Load more" });

    searchResponder = () =>
      jsonResponse({
        groups: [
          {
            group: "jobs",
            total: 5,
            items: [{ ...JOB_ITEM, id: "4816", title: "Deploy staging recovered" }],
            has_more: false,
            maybe_more: false,
            next_cursor: null,
          },
        ],
      });

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));

    await screen.findByRole("option", { name: /Deploy staging recovered/ });
    expect(screen.getByRole("option", { name: /Deploy production timed out/ })).toBeInTheDocument();
    expect(searchUrls().some((url) => url.includes("cursor=cursor-1"))).toBe(true);
  });

  it("adds one filter, opens its value menu and sends the chosen refinement", async () => {
    renderDialog();
    await typeQuery("deploy");

    fireEvent.click(screen.getByRole("button", { name: /Add filter/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Status" }));

    // Adding a key immediately opens its value menu, because the key alone constrains nothing.
    const option = await screen.findByRole("menuitemradio", { name: "Failed" });
    fireEvent.click(option);

    await waitFor(() => {
      expect(searchUrls().some((url) => url.includes("status=failed"))).toBe(true);
    });
    // A key already active is no longer offered.
    fireEvent.click(screen.getByRole("button", { name: /Add filter/ }));
    expect(screen.queryByRole("menuitem", { name: "Status" })).not.toBeInTheDocument();
  });

  it("keeps the workspace scope non-removable without account-wide search", async () => {
    renderDialog({ canSearchAccountWide: false });

    await screen.findByRole("button", { name: /Acme Operations/ });
    expect(
      screen.queryByRole("button", { name: "Remove Workspace filter" }),
    ).not.toBeInTheDocument();
  });

  it("resolves Escape as menu, then query, then dialog", async () => {
    const { onClose } = renderDialog();
    const dialog = screen.getByRole("dialog", { name: "Advanced search" });
    await typeQuery("deploy");

    fireEvent.click(screen.getByRole("button", { name: /Add filter/ }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.getByRole("combobox", { name: "Search Flow Steward" })).toHaveValue("");
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("moves the active result with the arrows and opens it with Enter", async () => {
    const { onOpenResult } = renderDialog();
    const combobox = await typeQuery("deploy");
    await screen.findByText("Best match");

    fireEvent.keyDown(combobox, { key: "ArrowDown" });
    // Focus never leaves the combobox while the active option moves.
    expect(combobox).toHaveAttribute("aria-activedescendant");

    fireEvent.keyDown(combobox, { key: "Enter" });
    expect(onOpenResult).toHaveBeenCalledTimes(1);
  });

  it("keeps the query and filters recoverable after a failed request", async () => {
    searchResponder = () =>
      new Response(JSON.stringify({ error: "boom" }), {
        headers: { "content-type": "application/json" },
        status: 500,
      });
    renderDialog();
    await typeQuery("deploy");

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Search couldn’t load")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Search Flow Steward" })).toHaveValue("deploy");

    searchResponder = () => jsonResponse(searchPayload());
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    await vi.advanceTimersByTimeAsync(300);

    await screen.findByText("Best match");
  });

  it("reports an empty result only for a completed query", async () => {
    searchResponder = () => jsonResponse({ groups: [], best_match: null });
    renderDialog();
    await typeQuery("nothing matches this");

    await waitFor(() => {
      expect(screen.getByRole("listbox")).toHaveTextContent("No matching results");
    });
    // The same state is announced once through the polite live region.
    expect(screen.getByText("No matching results", { selector: "p" })).toBeInTheDocument();
  });
});
