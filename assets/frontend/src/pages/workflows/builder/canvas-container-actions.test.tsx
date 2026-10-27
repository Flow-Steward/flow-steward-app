import { fireEvent, render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import type * as ContextMenusModule from "./canvas-context-menus";
import { ContainerCard } from "./canvas-container-card";

const commands = vi.hoisted(() => ({
  convert: vi.fn(),
  remove: vi.fn(),
}));
vi.mock("./canvas-context-menus", async (importOriginal) => ({
  ...await importOriginal<typeof ContextMenusModule>(),
  useCanvasCardCommands: () => ({
    moreItems: () => [],
    containerItems: (nodeId: string) => nodeId === "group:prepare" ? [
      { key: "convert", label: "Convert to child workflow…", onSelect: commands.convert },
      { kind: "separator", key: "sep" },
      { key: "delete", label: "Delete group", danger: true, onSelect: commands.remove },
    ] : [],
  }),
}));

const data = { automationRules: [], isEntry: false, lanes: [], local: false, ports: [], unreachable: false, validationIssues: [] };
function renderCard(selected: boolean) {
  return render(<ReactFlowProvider><ContainerCard nodeId="group:prepare" selected={selected} containerId="prepare" containerKind="group" displayName="Prepare order" badges={null} data={data} onOpen={vi.fn()} /></ReactFlowProvider>);
}

// R2-4: a Group had its commands only on right-click; a step shows More above it.
it("shows More above a selected Group with the group's own commands", () => {
  renderCard(true);
  fireEvent.click(screen.getByRole("button", { name: "More actions for Prepare order" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Convert to child workflow…" }));
  expect(commands.convert).toHaveBeenCalledOnce();
});

it("shows More while the group's header is pointed at, and not otherwise", () => {
  renderCard(false);
  expect(screen.queryByRole("button", { name: "More actions for Prepare order" })).not.toBeInTheDocument();
  fireEvent.mouseEnter(screen.getByTitle("Click to open; drag to move the group on the canvas"));
  expect(screen.getByRole("button", { name: "More actions for Prepare order" })).toBeVisible();
});
