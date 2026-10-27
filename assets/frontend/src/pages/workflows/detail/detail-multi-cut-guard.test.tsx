import { act, renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import { createWorkflowClipboardCommands, type ClipboardAuthority } from "./detail-clipboard-commands";
import { useWorkflowDeletionGuard } from "./detail-canvas-handlers";
import { useWorkflowChangeSet } from "./detail-change-set";

const definition = definitionFromWorkflow({
  workflow_id: "wf_multi_cut",
  steps: [
    { step_id: "plan", step_kind: "set_variable", display_name: "Plan" },
    { step_id: "review", step_kind: "set_variable", display_name: "Review" },
  ],
  groups: [],
  settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "plan" }, edges: [] } },
});
const reply = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200, headers: { "content-type": "application/json" },
});

it("blocks a disabled saved rule, then makes one combined dry run before a denied OS write", async () => {
  const scopedGets: string[] = [];
  const changeBodies: Array<Record<string, unknown>> = [];
  let protectedRule = true;
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.includes("/reactions")) {
      const scopeId = url.includes("review") ? "review" : "plan";
      scopedGets.push(scopeId);
      return reply({ workflow_id: "wf_multi_cut", project_id: "project_a", scope_kind: "step", scope_id: scopeId,
        reactions: protectedRule && scopeId === "plan"
          ? [{ scope_kind: "step", scope_id: "plan", display_name: "Saved disabled rule", enabled: false }] : [],
        available_trigger_sources: [] });
    }
    if (url.endsWith("/changes")) {
      changeBodies.push(JSON.parse(String(init?.body || "{}")) as Record<string, unknown>);
      return reply({ workflow_id: "wf_multi_cut", revision: 7, valid: true, applied: false,
        issues: [], blocking_issues: [], workflow: {} });
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  const writeText = vi.fn(async () => { throw new DOMException("Denied", "NotAllowedError"); });
  const onCommitted = vi.fn();
  const view = renderHook(() => {
    const changeSet = useWorkflowChangeSet({
      workflowId: "wf_multi_cut", serverDefinition: definition, serverRevision: 7,
      refetchWorkflowSurfaces: async () => ({ workflow_id: "wf_multi_cut", revision: 7 }),
    });
    const guard = useWorkflowDeletionGuard({
      changeSet, owner: { workflowId: "wf_multi_cut", projectId: "project_a", ownerKey: "author/project_a/wf_multi_cut/1",
        selectionId: JSON.stringify(["plan", "review"]), serverRevision: 7, structureLocked: false },
      openProtectedScope: async () => true,
    });
    return { changeSet, guard };
  });
  try {
    const commands = createWorkflowClipboardCommands({
      readCurrent: () => ({
        changeSet: view.result.current.changeSet,
        owner: { workflowId: "wf_multi_cut", projectId: "project_a", ownerKey: "author/project_a/wf_multi_cut/1",
          selectionId: JSON.stringify(["plan", "review"]), serverRevision: 7, structureLocked: false },
        canvasWorkflowId: "wf_multi_cut", viewKey: "author", generation: 1, authoringAllowed: true,
        targets: [
          { canvasId: "plan", ref: { kind: "step", id: "plan" } },
          { canvasId: "review", ref: { kind: "step", id: "review" } },
        ],
        worldPositions: new Map([["plan", { x: 64, y: 64 }], ["review", { x: 384, y: 64 }]]),
        placement: { groupId: "", forEachId: "", laneId: "" },
        pasteOrigin: { x: 64, y: 64 }, pasteMode: "pointer", duplicateOrigin: { x: 64, y: 400 },
      }) satisfies ClipboardAuthority,
      clipboard: { writeText, readText: async () => "" },
      guard: view.result.current.guard,
      openProtectedScope: async () => true,
      onCommitted,
      copyLabel: (label) => label + " copy",
    });
    let outcome: Awaited<ReturnType<typeof commands.cut>> | undefined;
    await act(async () => { outcome = await commands.cut(); });
    expect(outcome).toMatchObject({ ok: false, reason: "guard_refused", clipboardWritten: false, staged: false });
    expect(scopedGets).toEqual(["plan", "review"]);
    expect(changeBodies).toEqual([]);
    expect(writeText).not.toHaveBeenCalled();
    expect(view.result.current.changeSet.operations).toEqual([]);
    protectedRule = false;
    await act(async () => { outcome = await commands.cut(); });
    expect(outcome).toMatchObject({ ok: false, reason: "clipboard_failed", clipboardWritten: false, staged: false });
    expect(scopedGets).toEqual(["plan", "review", "plan", "review"]);
    expect(changeBodies).toEqual([{ expected_revision: 7, dry_run: true, operations: [
      { op: "delete_step", step_id: "plan" }, { op: "delete_step", step_id: "review" },
    ] }]);
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(view.result.current.changeSet.operations).toEqual([]);
    expect(onCommitted).not.toHaveBeenCalled();
  } finally {
    view.unmount();
    fetchSpy.mockRestore();
  }
});
