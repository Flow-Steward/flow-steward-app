import { fireEvent, render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";

import { ForEachNode } from "@/pages/workflows/builder/canvas-for-each-node";
import type { ForEachNodeData } from "@/pages/workflows/builder/layout";

function data(overrides: Partial<ForEachNodeData> = {}): ForEachNodeData {
  return {
    automationRules: [],
    forEach: {
      id: "each_order",
      for_each_id: "each_order",
      group_id: null,
      display_name: "Each order",
      over: "state.orders",
      item_name: "order",
      body: { kind: "sequence" },
    },
    isEntry: false,
    lanes: [],
    local: false,
    ports: [{ outcome: { name: "success" }, label: "Next" }],
    unreachable: false,
    validationIssues: [],
    onOpenForEach: vi.fn(),
    onOpenNewStep: vi.fn(),
    ...overrides,
  };
}

function mount(nodeData: ForEachNodeData) {
  render(
    <ReactFlowProvider>
      <ForEachNode
        data={nodeData}
        deletable
        draggable
        dragging={false}
        id="for_each:each_order"
        isConnectable={false}
        positionAbsoluteX={0}
        positionAbsoluteY={0}
        selectable
        selected={false}
        type="forEachContainer"
        zIndex={1}
      />
    </ReactFlowProvider>,
  );
}

describe("For each on the canvas", () => {
  it("shows that its body runs per item, and where the items come from", () => {
    mount(data());

    expect(screen.getByText("Each order")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "For each item" })).toBeInTheDocument();
    expect(screen.getByText("per order of state.orders · one at a time")).toBeInTheDocument();
    // The group goes on by its own outcome once every item is done; no exit circle and no Move chip.
    expect(screen.getByText("After all items")).toBeInTheDocument();
    expect(screen.queryByText("Move")).not.toBeInTheDocument();
    expect(screen.queryByText("Source not set")).not.toBeInTheDocument();
    // A For each has no reactions of its own, so its card offers no automation.
    expect(screen.queryByTitle(/automation/i)).not.toBeInTheDocument();
  });

  it("flags a For each without a source instead of guessing one", () => {
    mount(data({ forEach: { ...data().forEach, over: "" } }));

    expect(screen.getByText("Source not set")).toBeInTheDocument();
  });

  it("opens its editor and adds a step into its own body, never into a Group", () => {
    const nodeData = data();
    mount(nodeData);

    fireEvent.click(screen.getByText("Each order"));
    expect(nodeData.onOpenForEach).toHaveBeenCalledWith("each_order");

    fireEvent.click(screen.getByRole("button", { name: "Add step to Each order" }));
    expect(nodeData.onOpenNewStep).toHaveBeenCalledWith({ groupId: "", laneId: "", forEachId: "each_order" });
  });

  it("offers one add button per lane for a lanes body", () => {
    const nodeData = data({ lanes: ["email", "sms"] });
    mount(nodeData);

    expect(screen.getByText("Parallel lanes")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add step to lane sms" }));
    expect(nodeData.onOpenNewStep).toHaveBeenCalledWith({ groupId: "", laneId: "sms", forEachId: "each_order" });
  });

  it("keeps a lane Add control bound to its actual For each lane", () => {
    const nodeData = data({ lanes: ["email", "sms"], laneFrames: [
      { id: "email", x: 26, y: 122, width: 236, height: 180, addControl: { x: 32, y: 264, width: 224, height: 28 } },
      { id: "sms", x: 26, y: 322, width: 236, height: 180, addControl: { x: 32, y: 464, width: 224, height: 28 } },
    ] });
    mount(nodeData);
    expect(screen.getAllByRole("button", { name: /Add step to lane/ })).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Add step to lane sms" }));
    expect(nodeData.onOpenNewStep).toHaveBeenCalledExactlyOnceWith({ groupId: "", laneId: "sms", forEachId: "each_order" });
    expect(nodeData.onOpenForEach).not.toHaveBeenCalled();
  });

  it("shows no authoring controls when read-only", () => {
    mount(data({ readOnly: true }));

    expect(screen.queryByRole("button", { name: /Add step/ })).not.toBeInTheDocument();
  });
});
