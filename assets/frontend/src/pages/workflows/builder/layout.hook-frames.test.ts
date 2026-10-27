import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { buildCanvasLayout } from "./layout";
import { hookFrame, withHookFrames } from "./hook-frames";

function graph(): WorkflowCanvasGraph {
  const hook: WorkflowCanvasGraph["groups"][number] = { id: "hook:before", display_name: "Hook", body: { entry: { kind: "group", id: "hook:before:sequence" } }, hook: { hookId: "before", stepId: "before", displayName: "Hook", connected: true, targetWorkflowId: "client", targetName: "Client", usedIn: 1 } };
  const sequence: WorkflowCanvasGraph["groups"][number] = { id: "hook:before:sequence", group_id: "hook:before:sequence", display_name: "Sequence", parent: { kind: "group", id: "hook:before" }, body: { entry: { kind: "for_each", id: "hook:before:items" } } };
  return {
    workflow: { id: "author", enabled: true, display_name: "Author", callable_by_parent: false }, interface_contract: {}, output_contract: {}, entry: { kind: "group", id: "hook:before" },
    // API order is intentionally children first. React Flow requires parents first.
    groups: [sequence, hook],
    for_each: [
      { id: "hook:before:items", group_id: "hook:before:sequence", parent: { kind: "group", id: "hook:before:sequence" }, display_name: "Items", over: "state.items", item_name: "item", body: { kind: "lanes", lanes: [{ lane_id: "left", entry: { kind: "step", id: "hook:before:left" } }, { lane_id: "right", entry: { kind: "step", id: "hook:before:right" } }] } },
    ],
    steps: [
      { id: "hook:before:left", kind: "set_variable", display_name: "Left", config_summary: "", for_each_id: "hook:before:items", lane_id: "left", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "hook:before:right", kind: "set_variable", display_name: "Right", config_summary: "", for_each_id: "hook:before:items", lane_id: "right", ports: [{ outcome: { name: "success" }, label: "Next" }] },
    ],
    edges: [
      { source: { kind: "step", id: "hook:before:left" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "hook:before:items", lane_id: "left" } },
      { source: { kind: "step", id: "hook:before:right" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "hook:before:items", lane_id: "right" } },
      { source: { kind: "for_each", id: "hook:before:items" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "hook:before:sequence", lane_id: null } },
    ], validation_issues: [], automation_rules: [],
  };
}

function layout(canvasGraph: WorkflowCanvasGraph, readOnly = false) {
  return buildCanvasLayout({ canvasGraph, readOnly, structureLocked: true, onOpenGroup: vi.fn(), onOpenGroupAutomation: vi.fn(), onOpenStep: vi.fn(), onOpenStepAutomation: vi.fn() });
}

it.each(["fields", "none"])("uses the connected child's %s return contract on its nested Stop card", (outputMode) => {
  const author = graph();
  author.groups = [];
  author.for_each = [];
  author.entry = { kind: "step", id: "before" };
  author.steps = [
    { id: "before", kind: "invoke_workflow", display_name: "Hook", config_summary: "", ports: [] },
    { id: "author_done", kind: "stop_success", display_name: "Author done", config_summary: "", ports: [] },
  ];
  author.edges = [];
  author.output_contract = { expected_outputs: [{ name: "order_status" }], returned_files: [{ name: "author.csv" }] };
  const child: WorkflowCanvasGraph = {
    ...author,
    workflow: { ...author.workflow, id: "client" },
    entry: { kind: "group", id: "child_group" },
    groups: [{ id: "child_group", display_name: "Child group", body: { entry: { kind: "step", id: "child_done" } } }],
    steps: [{ id: "child_done", kind: "stop_success", group_id: "child_group", display_name: "Child done", config_summary: "", ports: [] }],
    output_contract: { output_mode: outputMode, expected_outputs: [{ name: "quantity", default: "PRIVATE_DEFAULT" }], returned_files: [{ name: "child.csv", description: "PRIVATE_ARTIFACT" }] },
  };
  const frame = hookFrame({ hookId: "before", stepId: "before", displayName: "Hook", description: "", connected: true, targetWorkflowId: "client", targetName: "Client", bindingRevision: 1 }, child);
  const { nodes } = layout(withHookFrames(author, new Map([["before", frame]])));
  const stopContract = (id: string) => {
    const data = nodes.find((node) => node.id === id)?.data;
    return data && "step" in data ? data.contractSummary : undefined;
  };
  expect(stopContract("hook:before:child_done")).toEqual({ outputs: outputMode === "none" ? [] : ["quantity"], artifacts: ["child.csv"] });
  expect(stopContract("author_done")).toEqual({ outputs: ["order_status"], artifacts: ["author.csv"], outputSignatures: [{ name: "order_status" }], artifactSignatures: [{ name: "author.csv" }] });
  expect(JSON.stringify(nodes.map((node) => node.data))).not.toMatch(/PRIVATE_DEFAULT|PRIVATE_ARTIFACT/);
});

it("keeps Groups and For each scopes inside a root hook with every child in its visual parent", () => {
  const result = layout(graph());
  expect(result.nodes.find((node) => node.id === "group:hook:before")!.parentId).toBeUndefined();
  for (const [id, parentId] of [
    ["group:hook:before:sequence", "group:hook:before"], ["for_each:hook:before:items", "group:hook:before:sequence"], ["hook:before:left", "for_each:hook:before:items"], ["hook:before:right", "for_each:hook:before:items"],
  ]) {
    const child = result.nodes.find((node) => node.id === id)!;
    const parent = result.nodes.find((node) => node.id === parentId)!;
    expect(child.parentId, id).toBe(parentId);
    expect(child.extent, id).toBe("parent");
    expect(result.nodes.indexOf(parent), id).toBeLessThan(result.nodes.indexOf(child));
    expect(child.position.x, id).toBeGreaterThanOrEqual(0);
    expect(child.position.y, id).toBeGreaterThan(0);
    expect(child.position.x + child.width!, id).toBeLessThanOrEqual(parent.width!);
    expect(child.position.y + child.height!, id).toBeLessThanOrEqual(parent.height!);
    expect((child.data as { unreachable: boolean }).unreachable, id).toBe(false);
    expect((child.data as { isEntry: boolean }).isEntry, id).toBe(true);
  }
  expect(result.nodes.find((node) => node.id === "hook:before:left")!.position.y).not.toBe(result.nodes.find((node) => node.id === "hook:before:right")!.position.y);
  expect(result.nodes.find((node) => node.id === "group:hook:before")!.data.readOnly).toBe(true);
  for (const id of ["group:hook:before:sequence", "for_each:hook:before:items", "hook:before:left"]) expect(result.nodes.find((node) => node.id === id)!.data.readOnly).toBe(false);
  expect(result.edges.find((edge) => edge.source === "hook:before:left")).toMatchObject({ target: "for_each:hook:before:items", targetHandle: "exit" });
  expect(result.edges.find((edge) => edge.source === "for_each:hook:before:items")).toMatchObject({ target: "group:hook:before:sequence", targetHandle: "exit" });
});

it("keeps projected child containers read-only in archived runs", () => {
  const { nodes } = layout(graph(), true);
  for (const id of ["group:hook:before:sequence", "for_each:hook:before:items", "hook:before:left"]) expect(nodes.find((node) => node.id === id)!.data.readOnly).toBe(true);
});
