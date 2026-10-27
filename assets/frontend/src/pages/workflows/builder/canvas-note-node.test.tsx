import { fireEvent, render, screen, within } from "@testing-library/react";
import type { Node, NodeProps } from "@xyflow/react";

import { CanvasNoteNode, type CanvasNoteData } from "./canvas-note-node";
import type { CanvasNoteActions } from "./canvas-notes";

vi.mock("@xyflow/react", async (importOriginal) => ({ ...await importOriginal<object>(), NodeResizer: () => null }));

const note = { id: "note-1", text: "# Carrier cut-off\nDHL takes orders until **16:00**.", x: 0, y: 0, width: 256, height: 160 };

function actions(): CanvasNoteActions {
  return {
    edit: vi.fn(), finish: vi.fn(async () => true), removeWithUndo: vi.fn(), duplicate: vi.fn(async () => true),
    attachImage: vi.fn(async () => true), clearImage: vi.fn(async () => true), save: vi.fn(async () => true),
  };
}

function renderNote(data: Partial<CanvasNoteData> & { actions: CanvasNoteActions }, selected = false) {
  const props = { id: "annotation:note-1", data: { note, workflowId: "wf", busy: false, editing: false, ...data }, selected } as unknown as NodeProps<Node<CanvasNoteData>>;
  return render(<CanvasNoteNode {...props} />);
}

describe("canvas note", () => {
  it("reads as paper with a Note label, a title and Markdown text", () => {
    renderNote({ actions: actions() });
    const paper = screen.getByRole("group", { name: "Note: Carrier cut-off" });
    expect(paper).toHaveClass("workflow-note");
    expect(within(paper).getByText("Note")).toBeInTheDocument();
    expect(within(paper).getByRole("heading", { name: "Carrier cut-off" })).toBeInTheDocument();
    expect(within(paper).queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("is written in place with Image, Delete and Done under the text", () => {
    const handlers = actions();
    renderNote({ actions: handlers, editing: true }, true);
    const field = screen.getByRole("textbox", { name: "Note text" });
    expect(field).toHaveValue(note.text);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.change(field, { target: { value: "Changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(handlers.finish).toHaveBeenCalledWith("Changed");
    expect(screen.queryByRole("toolbar", { name: "Note actions" })).not.toBeInTheDocument();
  });

  it("keeps the text on Escape and on Cmd/Ctrl+Enter", () => {
    const handlers = actions();
    renderNote({ actions: handlers, editing: true });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Note text" }), { key: "Enter", metaKey: true });
    expect(handlers.finish).toHaveBeenCalledWith(note.text);
  });

  it("deletes from the writing bar and attaches an image with the current text", () => {
    const handlers = actions();
    renderNote({ actions: handlers, editing: true });
    fireEvent.change(screen.getByRole("textbox", { name: "Note text" }), { target: { value: "With a picture" } });
    const file = new File(["png"], "diagram.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Add image"), { target: { files: [file] } });
    expect(handlers.attachImage).toHaveBeenCalledWith(file, "With a picture");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(handlers.removeWithUndo).toHaveBeenCalledWith(note.id);
  });

  it("shows Edit and More when selected, with Duplicate and Delete in More", () => {
    const handlers = actions();
    renderNote({ actions: handlers }, true);
    const toolbar = screen.getByRole("toolbar", { name: "Note actions" });
    fireEvent.click(within(toolbar).getByRole("button", { name: "Edit note (Enter)" }));
    expect(handlers.edit).toHaveBeenCalledWith(note);
    fireEvent.click(within(toolbar).getByRole("button", { name: "More note actions" }));
    const menu = screen.getByRole("menu", { name: "More note actions" });
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Duplicate", "Delete"]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Delete" }));
    expect(handlers.removeWithUndo).toHaveBeenCalledWith(note.id);
  });

  it("opens Edit, Duplicate and Delete on a right-click", () => {
    const handlers = actions();
    renderNote({ actions: handlers });
    fireEvent.contextMenu(screen.getByRole("group", { name: "Note: Carrier cut-off" }));
    const menu = screen.getByRole("menu", { name: "Note actions" });
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Edit", "Duplicate", "Delete"]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Duplicate" }));
    expect(handlers.duplicate).toHaveBeenCalledWith(note);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("opens writing on a double-click", () => {
    const handlers = actions();
    renderNote({ actions: handlers });
    fireEvent.doubleClick(screen.getByRole("group", { name: "Note: Carrier cut-off" }));
    expect(handlers.edit).toHaveBeenCalledWith(note);
  });
});
