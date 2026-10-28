import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { fallbackContext, I18nContext, type I18nContextValue } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { changelogRevisionRestoreCopy } from "@/pages/registry/shared/changelog-revision";
import { RegistryEntityChangelogPanel } from "@/pages/registry/shared/entity-changelog-panel";
import { fetchChangelogDiff, restoreChangelogEntry } from "@/pages/registry/changelog/api";

vi.mock("@/pages/registry/changelog/api", () => ({
  fetchChangelogDiff: vi.fn(async () => ({ changes: [], summary: {} })),
  restoreChangelogEntry: vi.fn(async () => ({ ok: true })),
}));

function renderPanel(
  rows: Array<Record<string, unknown>>,
  props: Partial<Parameters<typeof RegistryEntityChangelogPanel>[0]> = {},
  i18n: I18nContextValue = fallbackContext,
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
      <I18nContext.Provider value={i18n}>
        <RegistryEntityChangelogPanel
          entityId="demo_entity"
          entityType="tool"
          loading={false}
          rows={rows}
          {...props}
        />
      </I18nContext.Provider>
    </QueryClientProvider>,
  );
}

function localizedContext(messages: Record<string, string>): I18nContextValue {
  return {
    ...fallbackContext,
    locale: "lt",
    messages,
    t: (key, defaultText, params) => interpolate(messages[key] || defaultText || key, params),
  };
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

    expect(await screen.findByRole("dialog", { name: "Restore saved revision" })).toBeInTheDocument();
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

  // WFB007: these exercise the rendered panel; API doubles only replace I/O.
  it("keeps numeric revision labels scoped to workflow content", () => {
    renderPanel([{
      id: "tool-revision-2", revision: 2, action: "update", revision_kind: "content",
    }], { entityType: "tool" });

    const row = screen.getByRole("button", { name: /Revision 2|Saved revision/u });
    expect(row).toHaveTextContent("Saved revision");
    expect(row).toHaveTextContent("Entry #tool-revision-2");
    expect(row).not.toHaveTextContent(/\bRevision 2\b/u);
  });

  it("compares numbered workflow revisions while retaining the exact entry references", async () => {
    renderPanel([
      { id: "history-new", revision: 2, action: "update", revision_kind: "content" },
      { id: "history-old", revision: 1, action: "create", revision_kind: "content" },
    ], { entityType: "workflow", expectedRevision: 2 });

    fireEvent.click(screen.getByRole("button", { name: /Revision 2/u }));

    expect(await screen.findByText("Comparing Revision 1 to Revision 2")).toBeVisible();
    expect(screen.getByRole("button", { name: /Revision 2/u })).toHaveTextContent("Entry #history-new");
    expect(screen.getByRole("button", { name: /Revision 1/u })).toHaveTextContent("Entry #history-old");
    expect(fetchChangelogDiff).toHaveBeenCalledWith({
      entityType: "workflow", entityId: "demo_entity", fromEntryId: "history-old", toEntryId: "history-new",
    });
  });

  it.each([
    ["update", "Updated", "content"],
    ["create", "Created", "content"],
    ["delete", "Deleted", "lifecycle"],
    ["restore", "Restored", "lifecycle"],
  ] as const)("labels the known %s action without changing its revision kind", (action, label, revisionKind) => {
    renderPanel([{ id: "known-action", action, revision_kind: revisionKind }]);

    const row = screen.getByRole("button", { name: /Entry #known-action/u });
    expect(row).toHaveTextContent(label);
    fireEvent.click(row);
    if (revisionKind === "lifecycle") {
      expect(screen.getByRole("heading", { name: "Lifecycle event" })).toBeVisible();
      expect(screen.queryByRole("button", { name: "Restore this revision" })).not.toBeInTheDocument();
    }
  });

  it.each([
    ["display_name", "Display name"],
    ["settings.callable.expected_outputs[0].label", "Outputs · Output 1 · Label"],
  ] as const)("shows a readable label for the known workflow field %s and retains the raw path", async (path, label) => {
    vi.mocked(fetchChangelogDiff).mockResolvedValue({
      changes: [{ path, change: "changed", from: "Before", to: "After" }],
    });
    renderPanel([
      { id: "field-new", revision: 2, action: "update", revision_kind: "content" },
      { id: "field-old", revision: 1, action: "create", revision_kind: "content" },
    ], { entityType: "workflow" });

    fireEvent.click(screen.getByRole("button", { name: /Revision 2/u }));

    expect(await screen.findByText(label)).toBeVisible();
    expect(screen.getByText(path)).toBeInTheDocument();
    expect(screen.getByText(/from:\s*Before\s*• to:\s*After/u)).toBeVisible();
  });

  it("keeps actor, event time and source legible beside a friendly revision label", () => {
    renderPanel([{
      id: "metadata-entry", revision: 2, action: "update", revision_kind: "content",
      actor: "ana", timestamp: "retained-event-time", source: "manual",
    }], { entityType: "workflow" });

    const row = screen.getByRole("button", { name: /Revision 2/u });
    expect(row).toHaveTextContent("ana");
    expect(row).toHaveTextContent("retained-event-time");
    expect(row).toHaveTextContent("manual");
  });

  it("uses locale messages for known workflow labels and distinct restore title and question keys", async () => {
    const locale = localizedContext({
      "react.registry.revision_value0": "Versija {value0}",
      "react.registry.action_updated": "Atnaujinta",
      "react.registry.display_name": "Pavadinimas",
      "react.registry.workflow_output_label_path": "Išvestys · Rezultatas {index} · Žyma",
      "react.registry.restore_saved_revision_title": "Išsaugotos versijos atkūrimas",
      "react.registry.restore_saved_revision_question": "Atkurti šią išsaugotą versiją?",
      "react.registry.restore_workflow_revision_as_new": "Atkurti {value0} kaip naują versiją?",
      "react.registry.restore_target": "Paskirtis: {value0}/{value1}.",
      "react.registry.restored_workflow_revision_as_new": "Atkurta {value0} kaip nauja versija.",
      "react.registry.restored_saved_revision": "Atkurta ši išsaugota versija.",
    });
    const genericRestoreCopy = changelogRevisionRestoreCopy(
      "tool", "tool-1", { revision_kind: "content" }, "entry-1", locale.t,
    );
    expect(genericRestoreCopy.title).toBe("Išsaugotos versijos atkūrimas");
    expect(genericRestoreCopy.body).toContain("Atkurti šią išsaugotą versiją?");
    vi.mocked(fetchChangelogDiff).mockResolvedValue({ changes: [
      { path: "display_name", change: "changed", from: "Old", to: "New" },
      { path: "settings.callable.expected_outputs[0].label", change: "changed", from: "Old", to: "New" },
    ] });
    vi.mocked(restoreChangelogEntry).mockResolvedValue({ ok: true });
    renderPanel([
      { id: "localized-new", revision: 2, action: "update", revision_kind: "content" },
      { id: "localized-old", revision: 1, action: "create", revision_kind: "content" },
    ], { entityType: "workflow" }, locale);

    const revision = screen.getByRole("button", { name: /Versija 2/u });
    expect(revision).toHaveTextContent("Atnaujinta");
    fireEvent.click(revision);
    expect(await screen.findByText("Pavadinimas")).toBeVisible();
    expect(screen.getByText("Išvestys · Rezultatas 1 · Žyma")).toBeVisible();
    expect(screen.getByText("settings.callable.expected_outputs[0].label")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Restore this revision" }));
    const confirmation = await screen.findByRole("dialog", { name: "Išsaugotos versijos atkūrimas" });
    expect(confirmation).toHaveTextContent("Atkurti Versija 2 kaip naują versiją?");
    expect(confirmation).toHaveTextContent("Paskirtis: workflow/demo_entity.");
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(await screen.findByText("Atkurta Versija 2 kaip nauja versija.")).toBeVisible();
  });

  it("confirms the selected workflow revision as a new revision and reports the exact target", async () => {
    vi.mocked(restoreChangelogEntry).mockResolvedValue({ ok: true });
    renderPanel([{ id: "restore-entry", revision: 2, action: "update", revision_kind: "content" }],
      { entityType: "workflow", expectedRevision: 7 });
    fireEvent.click(screen.getByRole("button", { name: /Revision 2/u }));
    fireEvent.click(screen.getByRole("button", { name: "Restore this revision" }));

    const confirmation = await screen.findByRole("dialog", { name: "Restore saved revision" });
    expect(confirmation).toHaveTextContent("Restore Revision 2 as a new revision?");
    expect(confirmation).toHaveTextContent("workflow/demo_entity");
    expect(confirmation).toHaveTextContent("Entry #restore-entry");
    expect(restoreChangelogEntry).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));

    expect(await screen.findByText("Restored Revision 2 as a new revision.")).toBeVisible();
    expect(restoreChangelogEntry).toHaveBeenCalledTimes(1);
    expect(restoreChangelogEntry).toHaveBeenCalledWith({
      entityType: "workflow", entityId: "demo_entity", entryId: "restore-entry", expectedRevision: 7,
    });
  });

  it("uses a saved-revision fallback without guessing an entity name or a revision number", async () => {
    vi.mocked(restoreChangelogEntry).mockResolvedValue({ ok: true });
    renderPanel([{ id: "legacy-ref", action: "custom.audit/v2", revision_kind: "content",
      snapshot: { display_name: "Historical title only" } }]);

    const row = screen.getByRole("button", { name: /Entry #legacy-ref/u });
    expect(row).toHaveTextContent("Saved revision");
    expect(row).toHaveTextContent("custom.audit/v2");
    fireEvent.click(row);
    fireEvent.click(screen.getByRole("button", { name: "Restore this revision" }));
    const confirmation = await screen.findByRole("dialog", { name: "Restore saved revision" });
    expect(confirmation).toHaveTextContent("Restore this saved revision?");
    expect(confirmation).toHaveTextContent("tool/demo_entity");
    expect(confirmation).toHaveTextContent("Entry #legacy-ref");
    expect(confirmation).not.toHaveTextContent("Historical title only");
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(await screen.findByText("Restored this saved revision.")).toBeVisible();
    expect(restoreChangelogEntry).toHaveBeenCalledWith({
      entityType: "tool", entityId: "demo_entity", entryId: "legacy-ref", expectedRevision: undefined,
    });
  });

  it.each(["settings.vendor_x.custom[3].token", "settings.callable.expected_outputs[0].label"])("retains raw tool field %s and unknown change kind without assuming workflow vocabulary", async (path) => {
    vi.mocked(fetchChangelogDiff).mockResolvedValue({
      changes: [{ path, change: "provider_delta", from: "old", to: "new" }],
    });
    renderPanel([
      { id: "fallback-new", action: "custom.audit/v2", revision_kind: "content" },
      { id: "fallback-old", action: "create", revision_kind: "content" },
    ]);
    fireEvent.click(screen.getByRole("button", { name: /Entry #fallback-new/u }));

    expect(await screen.findByText(path)).toBeVisible();
    expect(screen.getByText("provider_delta")).toBeVisible();
    expect(screen.getByText(/from:\s*old\s*• to:\s*new/u)).toBeVisible();
    expect(fetchChangelogDiff).toHaveBeenCalledWith({
      entityType: "tool", entityId: "demo_entity", fromEntryId: "fallback-old", toEntryId: "fallback-new",
    });
  });

  it.each([
    [{ from: 0, before: 91, to: false, after: "alias" }, /from:\s*0\s*• to:\s*false/u],
    [{ from: false, before: "alias", to: 0, after: 91 }, /from:\s*false\s*• to:\s*0/u],
    [{ from: null, before: "alias before", to: "", after: "alias after" }, /from:\s*—\s*• to:\s*—/u],
    [{ from: "", before: "alias before", to: null, after: "alias after" }, /from:\s*—\s*• to:\s*—/u],
    [{ before: "legacy before", after: "legacy after" }, /from:\s*legacy before\s*• to:\s*legacy after/u],
    [{ from: { title: "structured" }, to: ["one", "two"] }, /from:\s*—\s*• to:\s*2 items/u],
  ] as const)("renders own primitive and empty values before considering absent-key aliases: %j", async (values, expected) => {
    vi.mocked(fetchChangelogDiff).mockResolvedValue({ changes: [{ path: "settings.custom_value", ...values }] });
    renderPanel([
      { id: "value-new", action: "update", revision_kind: "content" },
      { id: "value-old", action: "create", revision_kind: "content" },
    ]);
    fireEvent.click(screen.getByRole("button", { name: /Entry #value-new/u }));

    expect(await screen.findByText(expected)).toBeVisible();
  });

});
