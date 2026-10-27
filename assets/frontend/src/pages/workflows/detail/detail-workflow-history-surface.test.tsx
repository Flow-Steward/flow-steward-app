import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { fetchJson } from "@/lib/http/client";
import { restoreChangelogEntry } from "@/pages/registry/changelog/api";
import { WorkflowHistorySurface } from "@/pages/workflows/detail/detail-workflow-history-surface";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));
vi.mock("@/pages/registry/changelog/api", () => ({
  fetchChangelogDiff: vi.fn(async () => ({ changes: [], summary: {} })),
  restoreChangelogEntry: vi.fn(async () => ({ status: "restored" })),
}));

const ROWS = [
  { id: "e2", revision: 2, restorable: true, action: "update", revision_kind: "content", author: "ana" },
  { id: "e1", revision: 1, restorable: true, action: "create", revision_kind: "content", author: "ana" },
];

function renderSurface(ctx: Record<string, unknown>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkflowHistorySurface
        ctx={{
          drawerMode: "workflow-settings",
          drawerTab: "history",
          workflowId: "orders",
          serverRevision: 2,
          changeSet: { hasChanges: false },
          refetchWorkflowSurfaces: vi.fn(async () => undefined),
          ...ctx,
        }}
      />
    </QueryClientProvider>,
  );
}

describe("WorkflowHistorySurface", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchJson).mockResolvedValue(ROWS);
  });

  it("stays out of the drawer until its tab is open", () => {
    const { container } = renderSurface({ drawerTab: "triggers" });

    expect(container).toBeEmptyDOMElement();
    expect(fetchJson).not.toHaveBeenCalled();
  });

  it("lists the workflow's revisions and restores one against the revision on screen", async () => {
    const refetchWorkflowSurfaces = vi.fn(async () => undefined);
    const forgetEditorSelection = vi.fn();
    const { container } = renderSurface({ refetchWorkflowSurfaces, forgetEditorSelection });

    fireEvent.click(await screen.findByRole("button", { name: /Revision 1/i }));
    fireEvent.click(screen.getByRole("button", { name: /Restore this revision/i }));
    // Inside the builder drawer the confirmation must sit on the body, above the revision drawer.
    const confirmation = await screen.findByRole("dialog", { name: "Restore workflow/orders" });
    expect(container).not.toContainElement(confirmation);
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));

    await waitFor(() => expect(refetchWorkflowSurfaces).toHaveBeenCalled());
    // Saved parameters changed underneath the editor: it must reload them on next open.
    expect(forgetEditorSelection).toHaveBeenCalled();
    expect(fetchJson).toHaveBeenCalledWith("/api/workflows/orders/history");
    expect(restoreChangelogEntry).toHaveBeenCalledWith({
      entityType: "workflow",
      entityId: "orders",
      entryId: "e1",
      expectedRevision: 2,
    });
  });

  it("does not restore while the canvas has unapplied changes", async () => {
    renderSurface({ changeSet: { hasChanges: true } });

    fireEvent.click(await screen.findByRole("button", { name: /Revision 1/i }));

    expect(screen.getByRole("button", { name: /Restore this revision/i })).toBeDisabled();
    expect(screen.getByText(/Apply or discard the unapplied canvas changes/i)).toBeInTheDocument();
  });
});
