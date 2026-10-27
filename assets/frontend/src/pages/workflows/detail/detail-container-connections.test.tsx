import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { fallbackContext } from "@/lib/i18n/context";
import { definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import { errorRoutePolicyOperation } from "@/pages/workflows/detail/detail-canvas-handlers";
import { ContainerConnections } from "@/pages/workflows/detail/detail-step-structure-setup";

// Prepare → Group (For each over orders → item steps) → Done.
const preview = definitionFromWorkflow({
  workflow_id: "wf",
  steps: [
    { step_id: "prepare", step_kind: "set_variable", display_name: "Prepare" },
    { step_id: "done", step_kind: "stop_success", display_name: "Done" },
    { step_id: "tidy", step_kind: "set_variable", display_name: "Tidy", group_id: "process" },
    { step_id: "keep", step_kind: "collect", display_name: "Keep", for_each_id: "each" },
  ],
  groups: [
    { group_id: "process", display_name: "Process", body: { kind: "sequence" } },
    { group_id: "audit", display_name: "Audit", body: { kind: "sequence" } },
  ],
  for_each: [
    {
      for_each_id: "each",
      display_name: "Each order",
      group_id: "process",
      over: "state.orders",
      item_name: "order",
      error_policy: { on_error: "route_to_step" },
      body: { kind: "sequence" },
    },
  ],
  settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "prepare" }, edges: [] } },
});

function choose(name: string, option: string) {
  fireEvent.click(screen.getByRole("combobox", { name }));
  fireEvent.click(screen.getByRole("option", { name: option }));
}

describe("container connections in the drawer", () => {
  it("routes a For each inside its Group: the Group's other nodes and exit, never itself", () => {
    const connectPort = vi.fn();
    render(
      <ContainerConnections
        connectPort={connectPort}
        containerId="each"
        containerKind="for_each"
        disconnectPort={vi.fn()}
        preview={preview}
      />,
    );

    fireEvent.click(screen.getByRole("combobox", { name: "Next step for Next" }));
    const offered = screen.getAllByRole("option").map((option) => option.textContent);
    expect(offered).toEqual(["Not connected", "Tidy", "End the group here"]);

    fireEvent.click(screen.getByRole("option", { name: "End the group here" }));
    expect(connectPort).toHaveBeenCalledWith(
      { kind: "for_each", id: "each" },
      { name: "success" },
      { kind: "group_exit", group_id: "process", lane_id: null },
    );
  });

  it("routes a Group at the root to root steps and other Groups", () => {
    const connectPort = vi.fn();
    render(
      <ContainerConnections
        connectPort={connectPort}
        containerId="process"
        containerKind="group"
        disconnectPort={vi.fn()}
        preview={preview}
      />,
    );

    choose("Next step for Next", "Done");
    expect(connectPort).toHaveBeenCalledWith(
      { kind: "group", id: "process" },
      { name: "success" },
      { kind: "step", id: "done" },
    );
    fireEvent.click(screen.getByRole("combobox", { name: "Next step for Next" }));
    const offered = screen.getAllByRole("option").map((option) => option.textContent);
    expect(offered).toEqual(["Not connected", "Prepare", "Done", "Audit"]);
  });
});

describe("error routes of a For each", () => {
  it("changes the For each's own policy", () => {
    expect(
      errorRoutePolicyOperation(preview, { kind: "for_each", id: "each" }, "route_to_step", fallbackContext.t),
    ).toEqual({
      op: "update_for_each_config",
      for_each_id: "each",
      patch: { error_policy: { on_error: "route_to_step" } },
    });
  });

  it("lets a step inherit the policy of its For each", () => {
    expect(
      errorRoutePolicyOperation(preview, { kind: "step", id: "keep" }, "fail", fallbackContext.t),
    ).toEqual({ op: "update_step_config", step_id: "keep", patch: { error_policy: { on_error: "fail" } } });
  });
});
