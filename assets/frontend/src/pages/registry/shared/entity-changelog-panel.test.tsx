import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { RegistryEntityChangelogPanel } from "@/pages/registry/shared/entity-changelog-panel";
import { fetchChangelogDiff, restoreChangelogEntry } from "@/pages/registry/changelog/api";

vi.mock("@/pages/registry/changelog/api", () => ({
  fetchChangelogDiff: vi.fn(async () => ({ changes: [], summary: {} })),
  restoreChangelogEntry: vi.fn(async () => ({ ok: true })),
}));

function renderPanel(
  rows: Array<Record<string, unknown>>,
  props: Partial<Parameters<typeof RegistryEntityChangelogPanel>[0]> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RegistryEntityChangelogPanel
        entityId="demo_entity"
        entityType="tool"
        loading={false}
        rows={rows}
        {...props}
      />
    </QueryClientProvider>,
  );
}

describe("RegistryEntityChangelogPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchChangelogDiff).mockResolvedValue({ changes: [], summary: {} });
  });

  it("keeps deterministic selection state when history rows do not have numeric entry ids", () => {
    renderPanel([
      {
        id: "revision-alpha",
        action: "update",
        revision_kind: "content",
        author: "system",
        created_at: "2026-04-17T05:00:00Z",
      },
      {
        entry_id: 123,
        action: "update",
        revision_kind: "content",
        author: "admin",
        created_at: "2026-04-17T04:00:00Z",
      },
    ]);

    const firstEntryButton = screen.getByRole("button", { name: /Entry #revision-alpha/i });
    const secondEntryButton = screen.getByRole("button", { name: /Entry #123/i });

    // Selection is asserted through aria-current, not through a colour class.
    // The old probe was `ring-cyan-300/25`, the literal this transfer removes;
    // the intent — exactly one deterministic selection — is unchanged.
    expect(firstEntryButton).toHaveAttribute("aria-current", "true");
    expect(secondEntryButton).not.toHaveAttribute("aria-current");
    expect(screen.queryByRole("button", { name: /Restore this revision/i })).not.toBeInTheDocument();

    fireEvent.click(secondEntryButton);

    expect(firstEntryButton).not.toHaveAttribute("aria-current");
    expect(secondEntryButton).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("heading", { name: /Revision diff/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Restore this revision/i })).toBeEnabled();
  });

  it("restores a revision only after the accessible application dialog is confirmed", async () => {
    const mockedRestore = vi.mocked(restoreChangelogEntry);
    mockedRestore.mockClear();
    renderPanel([{ entry_id: 123, action: "update", revision_kind: "content", author: "admin", created_at: "2026-04-17T04:00:00Z" }]);

    fireEvent.click(screen.getByRole("button", { name: /Entry #123/i }));
    fireEvent.click(screen.getByRole("button", { name: /Restore this revision/i }));

    expect(await screen.findByRole("dialog", { name: "Restore tool/demo_entity" })).toBeInTheDocument();
    expect(mockedRestore).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => {
      expect(mockedRestore).toHaveBeenCalledWith({ entityType: "tool", entityId: "demo_entity", entryId: "123" });
    });
  });

  it.each(["restore", "delete"])("shows %s as a lifecycle event without content removals or revision restore", async (action) => {
    vi.mocked(fetchChangelogDiff).mockResolvedValue({
      changes: [{ path: "_phases[0].phase_id", change: "removed", from: "start", to: null }],
      summary: { total: 480, added: 4, removed: 476, changed: 0 },
    });
    renderPanel([
      { id: "selected-event", action, revision_kind: "lifecycle", snapshot: {
        tool_id: "demo_entity", display_name: "Before deletion", enabled: false,
        settings: { interfaces: [] },
      } },
      { id: "full-snapshot", action: "update", revision_kind: "content" },
    ]);

    fireEvent.click(screen.getByRole("button", { name: /Entry #selected-event/i }));

    expect(await screen.findByRole("heading", { name: "Lifecycle event" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Revision diff" })).not.toBeInTheDocument();
    expect(screen.getByText(/not a content revision/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Restore this revision/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/removed: 476|total: 0|No explicit diff rows/u)).not.toBeInTheDocument();
    expect(screen.queryByText("_phases[0].phase_id")).not.toBeInTheDocument();
    expect(fetchChangelogDiff).not.toHaveBeenCalled();
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });

  it.each(["restore", "delete"])("keeps content restore available after %s without requesting a content diff", async (action) => {
    renderPanel([
      { id: "content-after-marker", action: "update", revision_kind: "content" },
      { id: "previous-marker", action, revision_kind: "lifecycle", snapshot: {
        tool_id: "demo_entity", display_name: "Before deletion", enabled: false,
        settings: { interfaces: [] },
      } },
    ]);

    fireEvent.click(screen.getByRole("button", { name: /Entry #content-after-marker/i }));

    expect(await screen.findByText(/not a content comparison/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Revision diff" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore this revision" })).toBeEnabled();
    expect(screen.queryByText(/total: 0|No explicit diff rows/u)).not.toBeInTheDocument();
    expect(fetchChangelogDiff).not.toHaveBeenCalled();
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });

  it("requests the exact content pair and renders its diff", async () => {
    vi.mocked(fetchChangelogDiff).mockResolvedValue({
      comparison_kind: "content",
      changes: [{ path: "display_name", change: "changed", from: "Before", to: "After" }],
      summary: { total: 1, changed: 1 },
    });
    renderPanel([
      { id: "new-content", action: "update", revision_kind: "content" },
      { id: "old-content", action: "create", revision_kind: "content" },
    ]);
    fireEvent.click(screen.getByRole("button", { name: /Entry #new-content/i }));

    expect(await screen.findByText("display_name")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Revision diff" })).toBeInTheDocument();
    expect(fetchChangelogDiff).toHaveBeenCalledWith({
      entityType: "tool", entityId: "demo_entity", fromEntryId: "old-content", toEntryId: "new-content",
    });
  });

  it("does not infer restorable content from an action when revision metadata is missing", async () => {
    renderPanel([
      { id: "unknown-kind", action: "update" },
      { id: "older-known", action: "create", revision_kind: "content" },
    ]);
    fireEvent.click(screen.getByRole("button", { name: /Entry #unknown-kind/i }));

    expect(await screen.findByText(/revision type is unavailable/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Restore this revision/i })).not.toBeInTheDocument();
    expect(fetchChangelogDiff).not.toHaveBeenCalled();
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });

  it("names a workflow revision by its number and restores it against the revision on screen", async () => {
    const mockedRestore = vi.mocked(restoreChangelogEntry);
    renderPanel(
      [
        { id: "e2", revision: 2, restorable: true, action: "update", revision_kind: "content", author: "ana" },
        { id: "e1", revision: 1, restorable: true, action: "create", revision_kind: "content", author: "ana" },
      ],
      { entityType: "workflow", expectedRevision: 2, restoreNote: "Restore saves a new revision." },
    );

    fireEvent.click(screen.getByRole("button", { name: /Revision 1/i }));
    expect(screen.getByText("Restore saves a new revision.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Restore this revision/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));

    await waitFor(() => {
      expect(mockedRestore).toHaveBeenCalledWith({
        entityType: "workflow",
        entityId: "demo_entity",
        entryId: "e1",
        expectedRevision: 2,
      });
    });
  });

  it("does not offer restore for a revision the server marks as not restorable", () => {
    renderPanel(
      [
        {
          id: "legacy",
          revision: null,
          restorable: false,
          restore_unavailable_reason: "This entry does not record the workflow definition.",
          action: "update",
          revision_kind: "content",
        },
      ],
      { entityType: "workflow" },
    );

    fireEvent.click(screen.getByRole("button", { name: /Entry #legacy/i }));

    expect(screen.getByText("This entry does not record the workflow definition.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Restore this revision/i })).not.toBeInTheDocument();
  });

  it("keeps restore disabled while the page has unapplied changes", () => {
    renderPanel(
      [{ id: "e1", revision: 1, restorable: true, action: "update", revision_kind: "content" }],
      { entityType: "workflow", restoreDisabledReason: "Apply or discard the unapplied canvas changes first." },
    );

    fireEvent.click(screen.getByRole("button", { name: /Revision 1/i }));

    expect(screen.getByText("Apply or discard the unapplied canvas changes first.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Restore this revision/i })).toBeDisabled();
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
  });
});
