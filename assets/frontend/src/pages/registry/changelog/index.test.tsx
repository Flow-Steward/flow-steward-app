import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { useAuth } from "@/lib/auth/useAuth";
import { HttpError } from "@/lib/http/errors";
import {
  type ChangelogEntry,
  fetchChangelogDiff,
  fetchChangelogHistory,
  fetchChangelogList,
  fetchChangelogSearch,
  fetchRegistryStats,
  fetchRegistryValidation,
  fetchWorkflowCurrentRevision,
  restoreChangelogEntry,
} from "@/pages/registry/changelog/api";
import { RegistryChangelogPage } from "@/pages/registry/changelog";

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn() }));
vi.mock("@/pages/registry/changelog/api", () => ({
  fetchChangelogDiff: vi.fn(),
  fetchChangelogHistory: vi.fn(),
  fetchChangelogList: vi.fn(),
  fetchChangelogSearch: vi.fn(),
  fetchRegistryStats: vi.fn(),
  fetchRegistryValidation: vi.fn(),
  fetchWorkflowCurrentRevision: vi.fn(),
  restoreChangelogEntry: vi.fn(),
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedFetchChangelogDiff = vi.mocked(fetchChangelogDiff);
const mockedFetchChangelogHistory = vi.mocked(fetchChangelogHistory);
const mockedFetchChangelogList = vi.mocked(fetchChangelogList);
const mockedFetchChangelogSearch = vi.mocked(fetchChangelogSearch);
const mockedFetchRegistryStats = vi.mocked(fetchRegistryStats);
const mockedFetchRegistryValidation = vi.mocked(fetchRegistryValidation);
const mockedFetchWorkflowCurrentRevision = vi.mocked(fetchWorkflowCurrentRevision);
const mockedRestoreChangelogEntry = vi.mocked(restoreChangelogEntry);

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <RegistryChangelogPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

const WORKFLOW_ENTRIES: ChangelogEntry[] = [
  { id: "new-content", entity_type: "workflow", entity_id: "owned-workflow", action: "update", revision_kind: "content" },
  { id: "old-content", entity_type: "workflow", entity_id: "owned-workflow", action: "create", revision_kind: "content" },
];

function staleRevision(): HttpError {
  return new HttpError({
    status: 409,
    url: "/api/workflows/owned-workflow/restore",
    payload: { detail: "Workflow 'owned-workflow' changed since revision 4" },
  });
}

async function openWorkflowRevision(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText(/entry #new-content/u));
}

async function confirmRestore(user: ReturnType<typeof userEvent.setup>) {
  const restore = screen.getByRole("button", { name: "Restore selected revision" });
  await waitFor(() => expect(restore).toBeEnabled());
  await user.click(restore);
  await screen.findByRole("dialog", { name: "Restore workflow/owned-workflow" });
}

describe("RegistryChangelogPage validation issues", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedUseAuth.mockReturnValue({
      session: { user: { timezone: "UTC" } },
    } as ReturnType<typeof useAuth>);
    mockedFetchChangelogList.mockResolvedValue([]);
    mockedFetchChangelogHistory.mockResolvedValue([]);
    mockedFetchChangelogDiff.mockResolvedValue({});
    mockedFetchChangelogSearch.mockResolvedValue([]);
    mockedFetchRegistryStats.mockResolvedValue({});
    mockedFetchWorkflowCurrentRevision.mockResolvedValue(7);
    mockedFetchRegistryValidation.mockResolvedValue({
      valid: false,
      errors: ["Workflow 'wf-bad' phase 'build': agent 'missing' not found in registry"],
      warnings: ["Tool 'unused-tool' is enabled but used by no agent"],
    });
  });

  it("lists each validation issue with entity, rule, message, severity, and detail link", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Validate registry" }));

    expect(await screen.findByText("Validation failed • errors: 1 • warnings: 1")).toBeInTheDocument();
    expect(screen.getByText("missing_reference")).toBeInTheDocument();
    expect(screen.getByText("unused_entity")).toBeInTheDocument();
    expect(screen.getByText("error")).toBeInTheDocument();
    expect(screen.getByText("warning")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Workflow wf-bad" }),
    ).toHaveAttribute("href", "/registry/workflows/wf-bad");
    expect(
      screen.getByRole("link", { name: "Tool unused-tool" }),
    ).toHaveAttribute("href", "/registry/tools/unused-tool");
    expect(screen.getByText(/agent 'missing' not found in registry/u)).toBeInTheDocument();
  });

  it("fails the revision dialog closed when history access is denied", async () => {
    const user = userEvent.setup();
    mockedFetchChangelogList.mockResolvedValue([
      { id: "2", entity_type: "agent", entity_id: "global-agent", action: "update" },
      { id: "1", entity_type: "agent", entity_id: "global-agent", action: "create" },
    ]);
    mockedFetchChangelogHistory.mockRejectedValue(new Error("Access denied"));
    renderPage();

    await user.click(await screen.findByText(/entry #2/u));

    expect(await screen.findByText("Access denied")).toBeInTheDocument();
    expect(screen.queryByText("No history rows found.")).not.toBeInTheDocument();
    expect(screen.queryByText(/No previous revision available/u)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore selected revision" })).not.toBeInTheDocument();
  });

  it.each(["restore", "delete"])("shows %s as a lifecycle event without content removals or revision restore", async (action) => {
    const user = userEvent.setup();
    const entries: ChangelogEntry[] = [
      { id: "selected-event", entity_type: "workflow", entity_id: "owned-workflow", action, revision_kind: "lifecycle", snapshot: {
        workflow_id: "owned-workflow", display_name: "Before deletion", enabled: false,
        settings: { interfaces: [] }, _phases: [{ phase_id: "start", steps: [] }],
      } },
      { id: "full-snapshot", entity_type: "workflow", entity_id: "owned-workflow", action: "update", revision_kind: "content" },
    ];
    mockedFetchChangelogList.mockResolvedValue(entries);
    mockedFetchChangelogHistory.mockResolvedValue(entries);
    mockedFetchChangelogDiff.mockResolvedValue({
      changes: [{ path: "_phases[0].phase_id", change: "removed", from: "start", to: null }],
      summary: { total: 480, added: 4, removed: 476, changed: 0 },
    });
    renderPage();

    await user.click(await screen.findByText(/entry #selected-event/u));

    expect(await screen.findByText("Lifecycle event")).toBeInTheDocument();
    expect(screen.queryByText("Revision diff")).not.toBeInTheDocument();
    expect(screen.getByText(/not a content revision/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore selected revision" })).not.toBeInTheDocument();
    expect(screen.queryByText(/removed: 476|total: 0|No explicit diff rows/u)).not.toBeInTheDocument();
    expect(screen.queryByText("_phases[0].phase_id")).not.toBeInTheDocument();
    expect(fetchChangelogDiff).not.toHaveBeenCalled();
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });

  it.each(["restore", "delete"])("keeps content restore available after %s without requesting a content diff", async (action) => {
    const user = userEvent.setup();
    const entries: ChangelogEntry[] = [
      { id: "content-after-marker", entity_type: "workflow", entity_id: "owned-workflow", action: "update", revision_kind: "content" },
      { id: "previous-marker", entity_type: "workflow", entity_id: "owned-workflow", action, revision_kind: "lifecycle", snapshot: {
        workflow_id: "owned-workflow", display_name: "Before deletion", enabled: false,
        settings: { interfaces: [] }, _phases: [{ phase_id: "start", steps: [] }],
      } },
    ];
    mockedFetchChangelogList.mockResolvedValue(entries);
    mockedFetchChangelogHistory.mockResolvedValue(entries);
    renderPage();

    await user.click(await screen.findByText(/entry #content-after-marker/u));

    expect(await screen.findByText(/not a content comparison/i)).toBeInTheDocument();
    expect(screen.queryByText("Revision diff")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Restore selected revision" })).toBeEnabled());
    expect(screen.queryByText(/total: 0|No explicit diff rows/u)).not.toBeInTheDocument();
    expect(fetchChangelogDiff).not.toHaveBeenCalled();
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });

  it("renders the exact content diff and restores only after confirmation", async () => {
    const user = userEvent.setup();
    const entries: ChangelogEntry[] = [
      { id: "new-content", entity_type: "workflow", entity_id: "owned-workflow", action: "update", revision_kind: "content" },
      { id: "old-content", entity_type: "workflow", entity_id: "owned-workflow", action: "create", revision_kind: "content" },
    ];
    mockedFetchChangelogList.mockResolvedValue(entries);
    mockedFetchChangelogHistory.mockResolvedValue(entries);
    mockedFetchChangelogDiff.mockResolvedValue({
      comparison_kind: "content",
      changes: [{ path: "display_name", change: "changed", from: "Before", to: "After" }],
      summary: { total: 1, changed: 1 },
    });
    vi.mocked(restoreChangelogEntry).mockResolvedValue({});
    renderPage();

    await user.click(await screen.findByText(/entry #new-content/u));

    expect(await screen.findByText("display_name")).toBeInTheDocument();
    expect(fetchChangelogDiff).toHaveBeenCalledWith({
      entityType: "workflow", entityId: "owned-workflow", fromEntryId: "old-content", toEntryId: "new-content",
    });
    await confirmRestore(user);
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(restoreChangelogEntry).toHaveBeenCalledWith({
      entityType: "workflow", entityId: "owned-workflow", entryId: "new-content", expectedRevision: 7,
    }));
    expect(await screen.findByText("Restored workflow/owned-workflow from entry #new-content.")).toBeInTheDocument();
  });

  it("restores other entity types without a workflow revision", async () => {
    const user = userEvent.setup();
    const entries: ChangelogEntry[] = [
      { id: "tool-new", entity_type: "tool", entity_id: "t1", action: "update", revision_kind: "content" },
      { id: "tool-old", entity_type: "tool", entity_id: "t1", action: "create", revision_kind: "content" },
    ];
    mockedFetchChangelogList.mockResolvedValue(entries);
    mockedFetchChangelogHistory.mockResolvedValue(entries);
    mockedRestoreChangelogEntry.mockResolvedValue({});
    renderPage();

    await user.click(await screen.findByText(/entry #tool-new/u));
    await user.click(screen.getByRole("button", { name: "Restore selected revision" }));
    await user.click(await screen.findByRole("button", { name: "Restore" }));

    await waitFor(() => expect(restoreChangelogEntry).toHaveBeenCalledWith({
      entityType: "tool", entityId: "t1", entryId: "tool-new",
    }));
    expect(fetchWorkflowCurrentRevision).not.toHaveBeenCalled();
  });

  it("changelog_keeps_open_revision_on_background_refresh", async () => {
    const user = userEvent.setup();
    mockedFetchChangelogList.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedFetchChangelogHistory.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedFetchWorkflowCurrentRevision.mockResolvedValueOnce(4).mockResolvedValue(5);
    mockedRestoreChangelogEntry.mockRejectedValue(staleRevision());
    const { queryClient } = renderPage();

    await openWorkflowRevision(user);
    expect(await screen.findByText(/Current workflow revision: 4\./u)).toBeInTheDocument();
    const listReads = mockedFetchChangelogList.mock.calls.length;
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["registry", "changelog"] });
    });
    expect(mockedFetchChangelogList.mock.calls.length).toBeGreaterThan(listReads);
    expect(screen.getByText(/Current workflow revision: 4\./u)).toBeInTheDocument();

    await confirmRestore(user);
    await user.click(screen.getByRole("button", { name: "Restore" }));

    expect(await screen.findByText(/workflow changed after you opened this revision/u)).toBeInTheDocument();
    expect(restoreChangelogEntry).toHaveBeenCalledTimes(1);
    expect(restoreChangelogEntry).toHaveBeenCalledWith({
      entityType: "workflow", entityId: "owned-workflow", entryId: "new-content", expectedRevision: 4,
    });
    expect(fetchWorkflowCurrentRevision).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Restore selected revision" })).toBeDisabled();
    expect(screen.queryByText(/Current workflow revision/u)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Restored /u)).not.toBeInTheDocument();
  });

  it("changelog_restore_uses_new_context_only_after_reopen", async () => {
    const user = userEvent.setup();
    mockedFetchChangelogList.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedFetchChangelogHistory.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedFetchWorkflowCurrentRevision.mockResolvedValueOnce(4).mockResolvedValueOnce(5);
    mockedRestoreChangelogEntry.mockRejectedValueOnce(staleRevision()).mockResolvedValueOnce({});
    renderPage();

    await openWorkflowRevision(user);
    await confirmRestore(user);
    await user.click(screen.getByRole("button", { name: "Restore" }));
    expect(await screen.findByText(/workflow changed after you opened this revision/u)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close revision details" }));
    await openWorkflowRevision(user);
    expect(await screen.findByText(/Current workflow revision: 5\./u)).toBeInTheDocument();
    expect(screen.queryByText(/workflow changed after you opened this revision/u)).not.toBeInTheDocument();
    await confirmRestore(user);
    await user.click(screen.getByRole("button", { name: "Restore" }));

    await waitFor(() => expect(restoreChangelogEntry).toHaveBeenCalledTimes(2));
    expect(mockedRestoreChangelogEntry.mock.calls.map(([args]) => args.expectedRevision)).toEqual([4, 5]);
    expect(fetchWorkflowCurrentRevision).toHaveBeenCalledTimes(2);
  });

  it("keeps restore unavailable when the current revision cannot be read", async () => {
    const user = userEvent.setup();
    mockedFetchChangelogList.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedFetchChangelogHistory.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedFetchWorkflowCurrentRevision.mockRejectedValue(new Error("offline"));
    renderPage();

    await openWorkflowRevision(user);

    expect(await screen.findByText(/current revision could not be loaded/u)).toBeInTheDocument();
    const restore = screen.getByRole("button", { name: "Restore selected revision" });
    expect(restore).toBeDisabled();
    await user.click(restore);
    expect(screen.queryByRole("dialog", { name: "Restore workflow/owned-workflow" })).not.toBeInTheDocument();
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
    expect(fetchWorkflowCurrentRevision).toHaveBeenCalledTimes(1);
  });

  it("writes nothing when the selection moves to another workflow while confirming", async () => {
    const user = userEvent.setup();
    mockedFetchChangelogList.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedFetchChangelogHistory.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedRestoreChangelogEntry.mockResolvedValue({});
    const { queryClient } = renderPage();

    await openWorkflowRevision(user);
    await confirmRestore(user);
    mockedFetchChangelogList.mockResolvedValue([
      { id: "other-new", entity_type: "workflow", entity_id: "other-workflow", action: "update", revision_kind: "content" },
    ]);
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["registry", "changelog"] });
    });
    await waitFor(() => expect(fetchWorkflowCurrentRevision).toHaveBeenCalledWith("other-workflow"));
    await user.click(screen.getByRole("button", { name: "Restore" }));

    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });

  it("writes nothing when the revision view closes while confirming", async () => {
    const user = userEvent.setup();
    mockedFetchChangelogList.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedFetchChangelogHistory.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedRestoreChangelogEntry.mockResolvedValue({});
    renderPage();

    await openWorkflowRevision(user);
    await confirmRestore(user);
    fireEvent.click(screen.getByRole("button", { name: "Close revision details" }));
    await user.click(screen.getByRole("button", { name: "Restore" }));

    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });

  it("writes nothing when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    mockedFetchChangelogList.mockResolvedValue(WORKFLOW_ENTRIES);
    mockedFetchChangelogHistory.mockResolvedValue(WORKFLOW_ENTRIES);
    renderPage();

    await openWorkflowRevision(user);
    await confirmRestore(user);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Restore workflow/owned-workflow" })).not.toBeInTheDocument());
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });

  it("does not infer restorable content from an action when revision metadata is missing", async () => {
    const user = userEvent.setup();
    const entries: ChangelogEntry[] = [
      { id: "unknown-kind", entity_type: "workflow", entity_id: "owned-workflow", action: "update" },
      { id: "older-known", entity_type: "workflow", entity_id: "owned-workflow", action: "create", revision_kind: "content" },
    ];
    mockedFetchChangelogList.mockResolvedValue(entries);
    mockedFetchChangelogHistory.mockResolvedValue(entries);
    renderPage();

    await user.click(await screen.findByText(/entry #unknown-kind/u));

    expect(await screen.findByText(/revision type is unavailable/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore selected revision" })).not.toBeInTheDocument();
    expect(fetchChangelogDiff).not.toHaveBeenCalled();
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });
});
