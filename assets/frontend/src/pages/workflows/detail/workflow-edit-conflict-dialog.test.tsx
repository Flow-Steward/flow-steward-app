import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { WorkflowChangeSetController } from "./detail-change-set";
import { WorkflowEditConflictDialog, WorkflowEditSessionNotice } from "./workflow-edit-conflict-dialog";
import type { WorkflowAuthoringContent } from "./workflow-edit-session";

/**
 * The conflict dialog (Task 6): three comparisons and an explicit choice. Keep
 * mine writes the reviewed copy at the reviewed revision only; a newer revision
 * is a new comparison, never a retry; accepting the server writes nothing.
 */

const base = { description: "base", settings: {}, steps: [], groups: [], for_each: [] } as WorkflowAuthoringContent;
const local = { ...base, description: "mine" } as WorkflowAuthoringContent;
const change = (path: string, from: unknown, to: unknown) => ({ path, change: "changed", from, to });
const comparison = (...changes: ReturnType<typeof change>[]) => ({ changes, summary: { total: changes.length, added: 0, removed: 0, changed: changes.length } });
const compared = (revision: number) => ({
  workflow_id: "wf",
  current_revision: revision,
  current_content: {},
  base_to_local: comparison(change("description", "base", "mine")),
  base_to_server: comparison(change("display_name", "Old", `Theirs ${revision}`)),
  local_to_server: comparison(change("description", "mine", "base"), change("display_name", "Old", `Theirs ${revision}`)),
});
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });

function changeSet(journal: object = { items: [], cursor: 0, pending: [], nextId: 1 }) {
  return {
    workflowId: "wf",
    journal,
    checkpoint: { revision: 12, held: true },
    keepOwnVersionApplied: vi.fn(),
    acceptServerVersion: vi.fn(),
  } as unknown as WorkflowChangeSetController & { keepOwnVersionApplied: ReturnType<typeof vi.fn>; acceptServerVersion: ReturnType<typeof vi.fn> };
}

function server(answers: Array<{ url: string; status?: number; body: unknown }>) {
  const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    requests.push({ url, body: JSON.parse(String(init?.body || "{}")) as Record<string, unknown> });
    const index = answers.findIndex((answer) => url.endsWith(answer.url));
    if (index < 0) throw new Error(`Unexpected request: ${url}`);
    const [answer] = answers.splice(index, 1);
    return response(answer.body, answer.status ?? 200);
  });
  return requests;
}

function show(options: { set?: ReturnType<typeof changeSet>; refetch?: () => Promise<unknown>; sequence?: () => number } = {}) {
  const set = options.set ?? changeSet();
  const onClose = vi.fn();
  const refetch = vi.fn(options.refetch ?? (async () => ({ workflow_id: "wf", revision: 13 })));
  const session = { baseContent: () => base, localContent: () => local, editSequence: options.sequence ?? (() => 4) };
  const view = render(<WorkflowEditConflictDialog changeSet={set} onClose={onClose} open refetchWorkflowSurfaces={refetch} session={session} />);
  return { view, set, onClose, refetch, session };
}

const dialog = () => screen.getByRole("dialog", { name: "Compare your copy with the server" });
const button = (name: string) => within(dialog()).getByRole("button", { name });

afterEach(() => vi.restoreAllMocks());

it("compares the copy three ways from its own base, and Decide later writes nothing", async () => {
  const requests = server([{ url: "/edit-session/compare", body: compared(13) }]);
  const { onClose } = show();
  expect(await screen.findByRole("region", { name: "My changes" })).toHaveTextContent("description");
  expect(screen.getByRole("region", { name: "Changes on the server" })).toHaveTextContent("Theirs 13");
  expect(screen.getByRole("region", { name: "My copy compared with the server" })).toHaveTextContent("display_name");
  // recovery_does_not_require_retained_server_history_entry: the base travels with the session.
  expect(requests).toEqual([{ url: "/api/workflows/wf/edit-session/compare", body: { base_content: base, local_content: local } }]);
  fireEvent.click(button("Decide later"));
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(requests.filter((request) => request.url.endsWith("/apply"))).toEqual([]);
});

it("keeps my version only after confirmation, at the reviewed revision, with the reviewed copy", async () => {
  const requests = server([
    { url: "/edit-session/compare", body: compared(13) },
    { url: "/edit-session/apply", body: { workflow_id: "wf", revision: 14, applied: true, valid: true, issues: [], blocking_issues: [] } },
  ]);
  const { set, onClose, refetch } = show();
  await screen.findByRole("region", { name: "My changes" });
  fireEvent.click(button("Keep my version"));
  expect(requests.filter((request) => request.url.endsWith("/apply"))).toEqual([]);
  expect(within(dialog()).getByText(/after revision 13/)).toBeInTheDocument();
  fireEvent.click(button("Replace with my version"));
  await waitFor(() => expect(set.keepOwnVersionApplied).toHaveBeenCalledWith(14));
  expect(requests.filter((request) => request.url.endsWith("/apply"))).toEqual([
    { url: "/api/workflows/wf/edit-session/apply", body: { content: local, expected_revision: 13 } },
  ]);
  expect(refetch).toHaveBeenCalled();
  expect(onClose).toHaveBeenCalled();
});

it("answers a newer revision with a new comparison, never a retry", async () => {
  const requests = server([
    { url: "/edit-session/compare", body: compared(13) },
    { url: "/edit-session/apply", status: 409, body: { detail: { code: "workflow_revision_conflict" } } },
    { url: "/edit-session/compare", body: compared(14) },
  ]);
  const { set } = show();
  await screen.findByRole("region", { name: "My changes" });
  fireEvent.click(button("Keep my version"));
  fireEvent.click(button("Replace with my version"));
  expect(await screen.findByText(/Another newer version was saved meanwhile/)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("region", { name: "Changes on the server" })).toHaveTextContent("Theirs 14"));
  expect(requests.filter((request) => request.url.endsWith("/apply"))).toHaveLength(1);
  expect(set.keepOwnVersionApplied).not.toHaveBeenCalled();
});

it("does not use an old review after the copy changed", async () => {
  const requests = server([
    { url: "/edit-session/compare", body: compared(13) },
    { url: "/edit-session/compare", body: compared(13) },
  ]);
  let sequence = 4;
  const { set } = show({ sequence: () => sequence });
  await screen.findByRole("region", { name: "My changes" });
  fireEvent.click(button("Keep my version"));
  sequence = 5; // A later local action, or another tab's owner change.
  fireEvent.click(button("Replace with my version"));
  expect(await screen.findByText(/Your copy changed after the comparison/)).toBeInTheDocument();
  await waitFor(() => expect(requests.filter((request) => request.url.endsWith("/compare"))).toHaveLength(2));
  expect(requests.filter((request) => request.url.endsWith("/apply"))).toEqual([]);
  expect(set.keepOwnVersionApplied).not.toHaveBeenCalled();
});

it("accepts the server version locally after confirmation, and keeps the copy when it cannot load", async () => {
  const requests = server([{ url: "/edit-session/compare", body: compared(13) }, { url: "/edit-session/compare", body: compared(13) }]);
  const failing = show({ refetch: async () => { throw new Error("offline"); } });
  await screen.findByRole("region", { name: "My changes" });
  fireEvent.click(button("Accept the server version"));
  fireEvent.click(button("Discard my copy"));
  expect(await screen.findByText("The server version could not be loaded. Your copy is kept.")).toBeInTheDocument();
  expect(failing.set.acceptServerVersion).not.toHaveBeenCalled();
  failing.view.unmount();

  const ok = show();
  await screen.findByRole("region", { name: "My changes" });
  fireEvent.click(button("Accept the server version"));
  fireEvent.click(button("Discard my copy"));
  await waitFor(() => expect(ok.set.acceptServerVersion).toHaveBeenCalledTimes(1));
  expect(ok.onClose).toHaveBeenCalled();
  expect(requests.filter((request) => request.url.endsWith("/apply"))).toEqual([]);
});

it("offers no overwrite when the versions cannot be compared", async () => {
  server([{ url: "/edit-session/compare", status: 503, body: { detail: "Service unavailable" } }]);
  show();
  expect(await screen.findByText(/could not be compared/)).toBeInTheDocument();
  expect(button("Keep my version")).toBeDisabled();
});

it("reports an unrecoverable copy and a browser that cannot keep the copy", () => {
  const deleteRecovery = vi.fn(async () => undefined);
  const { rerender } = render(<WorkflowEditSessionNotice session={{ status: "recovery_unavailable", problem: "version", persistence: "unavailable", dirty: false, deleteRecovery }} />);
  expect(screen.getByText(/cannot be restored: it was saved by another version of the builder/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Delete the local copy" }));
  expect(deleteRecovery).toHaveBeenCalledTimes(1);
  rerender(<WorkflowEditSessionNotice session={{ status: "active", problem: null, persistence: "failed", dirty: true, deleteRecovery }} />);
  expect(screen.getByTestId("edit-session-storage-notice")).toHaveTextContent("could not keep a recovery copy");
  rerender(<WorkflowEditSessionNotice session={{ status: "active", problem: null, persistence: "saved", dirty: true, deleteRecovery }} />);
  expect(screen.queryByTestId("edit-session-storage-notice")).not.toBeInTheDocument();
});
