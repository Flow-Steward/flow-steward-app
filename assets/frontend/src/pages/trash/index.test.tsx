import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { TrashPage } from "@/pages/trash";
import {
  fetchLifecyclePlan,
  fetchTrashList,
  hardDeleteTrashItem,
  restoreTrashItem,
  type LifecyclePlanResponse,
} from "@/pages/trash/api";
import type * as TrashApiModule from "@/pages/trash/api";

vi.mock("@/pages/trash/api", async () => {
  const actual = await vi.importActual<typeof TrashApiModule>("@/pages/trash/api");
  return {
    ...actual,
    fetchTrashList: vi.fn(),
    fetchLifecyclePlan: vi.fn(),
    executeLifecyclePlan: vi.fn(),
    restoreTrashItem: vi.fn(),
    hardDeleteTrashItem: vi.fn(),
  };
});

const mockedFetchTrashList = vi.mocked(fetchTrashList);
const mockedFetchLifecyclePlan = vi.mocked(fetchLifecyclePlan);
const mockedRestoreTrashItem = vi.mocked(restoreTrashItem);
const mockedHardDeleteTrashItem = vi.mocked(hardDeleteTrashItem);

function renderPage(locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages: {}, t: (key, text = "", params) => interpolate(locale === "lt" ? ({ "trash.columns.type": "Tipas", "dashboard.composer.commands_hint_prefix": "Įveskite" } as Record<string, string>)[key] || text : text, params) }}><QueryClientProvider client={queryClient}>
      <TrashPage />
    </QueryClientProvider></I18nContext.Provider>,
  );
}

describe("TrashPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchTrashList.mockResolvedValue({
      items: [
        {
          category: "application",
          entity_type: "project",
          entity_id: "project-1",
          label: "Project One",
          deleted_at: "2026-05-12T10:00:00Z",
          deleted_by: "alice",
        },
        {
          category: "registry",
          entity_type: "registry_tool",
          entity_id: "tool-1",
          label: "Tool One",
          deleted_at: "2026-05-12T11:00:00Z",
          deleted_by: "bob",
        },
        {
          category: "registry",
          entity_type: "registry_skill",
          entity_id: "skill-1",
          label: "Skill One",
          deleted_at: "2026-05-12T12:00:00Z",
          deleted_by: "carol",
        },
      ],
      total: 3,
    });
    mockedFetchLifecyclePlan.mockResolvedValue({
      action_type: "hard_delete",
      plan_version: "version-from-plan",
      root_entity: { entity_type: "project", entity_id: "project-1", label: "Project One" },
      tree: {
        node_id: "project:project-1",
        entity_type: "project",
        entity_id: "project-1",
        label: "Project One",
        selectable: true,
        selected: true,
        status: "trashed",
        children: [],
      },
      warnings: [],
      validation_errors: [],
    } satisfies LifecyclePlanResponse);
  });

  it("uses noun Type translations for the trash column and filter without changing owner labels", async () => {
    renderPage("lt");
    await screen.findByText("Project One");
    expect(screen.getByRole("columnheader", { name: "Tipas" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    expect(await screen.findByRole("menuitem", { name: "Tipas" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Įveskite" })).not.toBeInTheDocument();
    expect(screen.getByText("Tool One")).toBeInTheDocument();
  });

  it("says nothing definitive about an empty trash until the first load settles", async () => {
    // A5-014: the page rendered the grid's "No deleted items found" state while
    // the first query was still in flight, on a destructive lifecycle surface.
    // The audit saw zero items, then 151 after Refresh.
    let resolveList: (value: { items: unknown[] }) => void = () => {};
    mockedFetchTrashList.mockReturnValue(
      new Promise((resolve) => {
        resolveList = resolve as (value: { items: unknown[] }) => void;
      }) as ReturnType<typeof fetchTrashList>,
    );

    renderPage();

    expect(await screen.findByText("Loading deleted items")).toBeInTheDocument();
    expect(screen.queryByText("No deleted items found")).not.toBeInTheDocument();
    expect(screen.queryByText(/showing 0 of 0/i)).not.toBeInTheDocument();

    resolveList({ items: [] });

    expect(await screen.findByText(/showing 0 of 0/i)).toBeInTheDocument();
    expect(screen.getByText("No deleted items found")).toBeInTheDocument();
  });

  it("opens lifecycle modal for bulk purge of a single project instead of a direct confirmation", async () => {
    renderPage();

    await waitFor(() => expect(mockedFetchTrashList).toHaveBeenCalled());
    // Select the project row, then purge through the bulk bar.
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Project Project One" }));
    fireEvent.click(screen.getByRole("button", { name: "Purge Selected" }));

    await waitFor(() => {
      expect(mockedFetchLifecyclePlan).toHaveBeenCalledWith(
        "project",
        "project-1",
        "hard_delete",
        "",
      );
    });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Hard delete plan")).toBeInTheDocument();
    expect(within(dialog).getAllByText("Project One").length).toBeGreaterThan(0);
  });

  it("shows category and id columns and filters by category through Add filter", async () => {
    const user = userEvent.setup();
    renderPage();

    const table = await screen.findByRole("table", { name: "Trash" });
    expect(within(table).getByRole("columnheader", { name: "Category" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "ID" })).toBeInTheDocument();
    // Three rows plus the header.
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(4));

    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Category" }));
    await user.click(await screen.findByRole("menuitem", { name: "Registry" }));

    await waitFor(() => {
      expect(within(table).getAllByRole("row")).toHaveLength(3);
    });
    expect(within(table).getByText("tool-1")).toBeInTheDocument();
    expect(within(table).getByText("skill-1")).toBeInTheDocument();
    expect(within(table).queryByText("project-1")).not.toBeInTheDocument();
    expect(screen.getByText("Category · Registry")).toBeInTheDocument();
    expect(screen.getByText("Showing 2 of 3")).toBeInTheDocument();

    // The type filter is a server filter: it narrows the request itself.
    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Type" }));
    await user.click(await screen.findByRole("menuitem", { name: "Skills" }));
    await waitFor(() =>
      expect(mockedFetchTrashList).toHaveBeenLastCalledWith(expect.objectContaining({ entityType: "registry_skill" })),
    );
  });

  it("keeps row lifecycle actions visible in the trash grid", async () => {
    const user = userEvent.setup();
    renderPage();

    const triggers = await screen.findAllByRole("button", { name: /^Actions for / });
    await user.click(triggers[0]);
    expect(await screen.findByRole("menuitem", { name: "Restore" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Purge" })).toBeInTheDocument();
  });

  it("returns focus to the live Restore action after an asynchronous lifecycle plan loads", async () => {
    let resolvePlan: (value: LifecyclePlanResponse) => void = () => undefined;
    mockedFetchTrashList.mockResolvedValue({
      items: [
        {
          category: "registry",
          entity_type: "mcp_connector",
          entity_id: "mcp_audit_fixed_docs",
          project_id: "20000000-0000-0000-0000-000000000001",
          label: "Audit Fixed Docs 20260822",
          deleted_at: "2026-08-22T12:00:00Z",
          deleted_by: "admin",
        },
      ],
      total: 1,
    });
    mockedFetchLifecyclePlan.mockReturnValue(
      new Promise((resolve) => {
        resolvePlan = resolve;
      }),
    );

    renderPage();

    const opener = await screen.findByRole("button", { name: /^Actions for / });
    opener.focus();
    expect(opener).toHaveFocus();

    fireEvent.click(opener);
    fireEvent.click(await screen.findByRole("menuitem", { name: "Restore" }));
    expect(mockedFetchLifecyclePlan).toHaveBeenCalledWith(
      "mcp_connector",
      "mcp_audit_fixed_docs",
      "restore",
      "20000000-0000-0000-0000-000000000001",
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled());

    resolvePlan({
      action_type: "restore",
      plan_version: "restore-plan-version",
      root_entity: {
        entity_type: "mcp_connector",
        entity_id: "mcp_audit_fixed_docs",
        label: "Audit Fixed Docs 20260822",
      },
      tree: {
        node_id: "mcp_connector:mcp_audit_fixed_docs",
        entity_type: "mcp_connector",
        entity_id: "mcp_audit_fixed_docs",
        label: "Audit Fixed Docs 20260822",
        selectable: true,
        selected: true,
        status: "trashed",
        children: [],
      },
      warnings: [],
      validation_errors: [],
    });

    await screen.findByRole("dialog", { name: "Restore plan" });
    // `DsDialog` owns focus placement and lands on its close button, so the
    // dialog no longer needs a `tabIndex={-1}` heading to receive focus. What
    // the contract requires is that focus moved into the dialog.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close Restore plan" })).toHaveFocus(),
    );
    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("confirms a purge through the canon dialog and names it as irreversible", async () => {
    const user = userEvent.setup();
    mockedHardDeleteTrashItem.mockResolvedValue({ status: "deleted" });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Tool Tool One" }));
    await user.click(await screen.findByRole("menuitem", { name: "Purge" }));

    expect(mockedHardDeleteTrashItem).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog", { name: "Purge tool “Tool One”?" });
    expect(within(dialog).getByText("This cannot be undone.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Purge tool" }));

    await waitFor(() => expect(mockedHardDeleteTrashItem).toHaveBeenCalledWith("registry_tool", "tool-1", ""));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(await screen.findByText("Tool “Tool One” purged.")).toBeInTheDocument();
  });

  it("confirms a row restore in place of the row, with a primary action and the selection untouched", async () => {
    const user = userEvent.setup();
    mockedRestoreTrashItem.mockResolvedValue({ status: "restored" });
    renderPage();

    const table = await screen.findByRole("table", { name: "Trash" });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Tool Tool One" }));
    await user.click(screen.getByRole("button", { name: "Actions for Skill Skill One" }));
    await user.click(await screen.findByRole("menuitem", { name: "Restore" }));

    // No dialog: the question replaces the row itself.
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    const strip = within(table).getByRole("group", { name: "Restore skill “Skill One”?" });
    expect(within(strip).getByRole("button", { name: "No" })).toHaveFocus();
    expect(within(table).queryByText("skill-1")).not.toBeInTheDocument();
    const confirm = within(strip).getByRole("button", { name: "Restore skill" });
    expect(confirm).toHaveClass("primary");
    // A selection update rerenders the page while this row owns confirmation.
    // The direct row-render callback must keep the same live confirmation DOM.
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Tool Tool One" }));
    expect(within(table).getByRole("group", { name: "Restore skill “Skill One”?" })).toBe(strip);
    expect(within(strip).getByRole("button", { name: "No" })).toHaveFocus();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Tool Tool One" }));
    await user.click(confirm);

    await waitFor(() => expect(mockedRestoreTrashItem).toHaveBeenCalledWith("registry_skill", "skill-1", ""));
    expect(await screen.findByText("Skill “Skill One” restored.")).toBeInTheDocument();
    // A row action never touches the selection.
    expect(screen.getByRole("checkbox", { name: "Select Tool Tool One" })).toBeChecked();
  });

  it("cancels a row restore on Escape and returns focus to the row's actions", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Skill Skill One" }));
    await user.click(await screen.findByRole("menuitem", { name: "Restore" }));
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("group", { name: /Restore skill/ })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Actions for Skill Skill One" })).toHaveFocus());
    expect(mockedRestoreTrashItem).not.toHaveBeenCalled();
  });

  it("keeps the dialog open with the reason when the only item fails", async () => {
    const user = userEvent.setup();
    mockedHardDeleteTrashItem.mockRejectedValue(new Error("purge blocked by a reference"));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Tool Tool One" }));
    await user.click(await screen.findByRole("menuitem", { name: "Purge" }));
    const dialog = screen.getByRole("alertdialog", { name: "Purge tool “Tool One”?" });
    await user.click(within(dialog).getByRole("button", { name: "Purge tool" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("purge blocked by a reference");
    expect(within(dialog).getByRole("button", { name: "Purge tool" })).toBeEnabled();
  });

  it("confirms a bulk restore inside the bar and keeps only the failed item selected", async () => {
    const user = userEvent.setup();
    mockedRestoreTrashItem
      .mockResolvedValueOnce({ status: "restored" })
      .mockRejectedValueOnce(new Error("restore blocked"));

    renderPage();

    await waitFor(() => expect(mockedFetchTrashList).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Tool Tool One" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Skill Skill One" }));
    const bar = screen.getByRole("region", { name: "Bulk actions" });
    await user.click(within(bar).getByRole("button", { name: "Restore Selected" }));

    // The question replaces the bar's buttons; the count it agrees to stays.
    const question = within(bar).getByRole("group", { name: "Restore 2 items?" });
    expect(within(bar).getByText("2 selected")).toBeInTheDocument();
    expect(within(bar).queryByRole("button", { name: "Restore Selected" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await user.click(within(question).getByRole("button", { name: "Restore 2 items" }));

    expect(await screen.findByText("1 of 2 items restored.")).toBeInTheDocument();
    expect(screen.getByText(/Skill One: restore blocked/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select Skill Skill One" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Select Tool Tool One" })).not.toBeChecked();
    expect(mockedHardDeleteTrashItem).not.toHaveBeenCalled();
  });

  it("cancels a bulk restore on Escape without clearing the selection", async () => {
    const user = userEvent.setup();
    renderPage();

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Tool Tool One" }));
    const bar = screen.getByRole("region", { name: "Bulk actions" });
    await user.click(within(bar).getByRole("button", { name: "Restore Selected" }));
    await user.keyboard("{Escape}");

    expect(within(bar).getByRole("button", { name: "Restore Selected" })).toBeInTheDocument();
    await waitFor(() => expect(within(bar).getByRole("button", { name: "Restore Selected" })).toHaveFocus());
    expect(screen.getByRole("checkbox", { name: "Select Tool Tool One" })).toBeChecked();
  });

  it("confirms a bulk purge through the dialog, listing the objects", async () => {
    const user = userEvent.setup();
    mockedHardDeleteTrashItem.mockResolvedValue({ status: "deleted" });
    renderPage();

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Tool Tool One" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Skill Skill One" }));
    await user.click(screen.getByRole("button", { name: "Purge Selected" }));

    const dialog = screen.getByRole("alertdialog", { name: "Purge 2 items?" });
    expect(within(dialog).getByRole("list", { name: "Affected objects" })).toHaveTextContent("Tool · Tool One");
    expect(within(dialog).getByText("This cannot be undone.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Purge 2 items" }));
    expect(await screen.findByText("2 items purged.")).toBeInTheDocument();
  });

  it("moves focus to the page title when the last row is purged", async () => {
    const user = userEvent.setup();
    mockedFetchTrashList.mockResolvedValue({
      items: [{ category: "registry", entity_type: "registry_tool", entity_id: "tool-1", label: "Tool One" }],
      total: 1,
    });
    mockedHardDeleteTrashItem.mockResolvedValue({ status: "deleted" });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Tool Tool One" }));
    await user.click(await screen.findByRole("menuitem", { name: "Purge" }));
    await user.click(screen.getByRole("button", { name: "Purge tool" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: "Trash", level: 1 })).toHaveFocus());
  });
});
