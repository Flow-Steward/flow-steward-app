import { workflowCanvasNodeIdForExecutedLocation, workflowCanvasNodeIdForRuntimeLocation } from "@/pages/workflows/detail/workflow-runtime-location";

const canvasGraph = {
  groups: [{ id: "process_items", group_id: "process_items", display_name: "Process items" }],
  steps: [
    {
      id: "fetch_products",
      group_id: "process_items",
      step_id: "fetch_products",
      display_name: "Fetch products",
      kind: "connector",
      config_summary: "",
    },
  ],
  edges: [],
} as never;

describe("workflow runtime location", () => {
  it("maps an authored runtime step ID to the exact canvas node ID", () => {
    expect(
      workflowCanvasNodeIdForRuntimeLocation(canvasGraph, {
        groupId: "process_items",
        stepId: "fetch_products",
      }),
    ).toBe("fetch_products");
  });

  it("falls back to the real group node while its lanes run with no single current step", () => {
    expect(
      workflowCanvasNodeIdForRuntimeLocation(canvasGraph, {
        groupId: "process_items",
        stepId: "",
      }),
    ).toBe("group:process_items");
  });

  it("shows nothing for a step the definition does not have", () => {
    expect(
      workflowCanvasNodeIdForRuntimeLocation(canvasGraph, { stepId: "removed_step" }),
    ).toBe("");
  });
});

it("highlights an executed group only when the workflow and revision match the displayed definition", () => {
  const location = { stepId: "", groupId: "process_items", definitionWorkflowId: "wf", definitionRevision: 4 };
  expect(workflowCanvasNodeIdForExecutedLocation(canvasGraph, location, "wf", 4)).toBe("group:process_items");
  expect(workflowCanvasNodeIdForExecutedLocation(canvasGraph, location, "wf", 5)).toBe("");
  expect(workflowCanvasNodeIdForExecutedLocation(canvasGraph, location, "other", 4)).toBe("");
  expect(workflowCanvasNodeIdForExecutedLocation(canvasGraph, { stepId: "fetch_products" }, "wf", 4)).toBe("");
});


describe("executed locations inside connected hook workflows", () => {
  const frames = new Map([
    ["before_step", { hookId: "before", stepId: "before_step", targetWorkflowId: "client", targetRevision: 3 }],
  ]);
  const projected = {
    groups: [{ id: "hook:before:process_items", display_name: "Client group" }],
    steps: [
      { id: "fetch_products", step_id: "fetch_products", kind: "connector", display_name: "Author step", config_summary: "" },
      { id: "hook:before:fetch_products", step_id: "hook:before:fetch_products", kind: "connector", display_name: "Client step", config_summary: "" },
    ],
    for_each: [{ id: "hook:before:items", display_name: "Client items" }],
    edges: [],
  };
  function executed(location: Record<string, unknown>, hooks = frames) {
    return Reflect.apply(workflowCanvasNodeIdForExecutedLocation, undefined, [projected, location, "author", 4, hooks]);
  }

  it("maps a client step to its projected member instead of the same-named author step", () => {
    expect(executed({ stepId: "fetch_products", definitionWorkflowId: "client", definitionRevision: 3 }))
      .toBe("hook:before:fetch_products");
  });

  it("maps the client group while its parallel lanes execute", () => {
    expect(executed({ stepId: "", groupId: "process_items", definitionWorkflowId: "client", definitionRevision: 3 }))
      .toBe("group:hook:before:process_items");
  });

  it("maps the owning client For each when its container is executing", () => {
    expect(executed({ stepId: "", forEachId: "items", definitionWorkflowId: "client", definitionRevision: 3 }))
      .toBe("for_each:hook:before:items");
  });

  it("does not paint stale client revisions or another workflow onto the frame", () => {
    expect(executed({ stepId: "fetch_products", definitionWorkflowId: "client", definitionRevision: 2 })).toBe("");
    expect(executed({ stepId: "fetch_products", definitionWorkflowId: "other", definitionRevision: 3 })).toBe("");
  });

  it("does not guess between hooks that call the same client workflow", () => {
    const twoFrames = new Map(frames);
    twoFrames.set("after_step", { hookId: "after", stepId: "after_step", targetWorkflowId: "client", targetRevision: 3 });
    expect(executed({ stepId: "fetch_products", definitionWorkflowId: "client", definitionRevision: 3 }, twoFrames)).toBe("");
  });
});
