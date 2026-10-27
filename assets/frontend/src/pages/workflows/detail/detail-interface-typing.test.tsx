import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import {
  workflowFieldPayload,
  workflowOutputFieldPayload,
  type WorkflowInterfaceFieldDraft,
} from "./detail-interface";
import { createWorkflowInterfaceRenderers } from "./detail-interface-renderers";

type Editor = "input" | "output";

function EditorHarness({ editor, onSave, revision = 0 }: Readonly<{
  editor: Editor;
  onSave: (value: unknown) => void;
  revision?: number;
}>) {
  const [fields, setFields] = useState<WorkflowInterfaceFieldDraft[]>([]);
  const view = createWorkflowInterfaceRenderers({
    workflowInterfaceControlIdPrefix: "typing-regression",
  });
  const save = () => {
    onSave(editor === "input" ? workflowFieldPayload(fields) : workflowOutputFieldPayload(fields));
  };
  return <div data-revision={revision}>
    {view.renderWorkflowInterfaceFieldEditor("Fields", fields, setFields, editor)}
    <button onClick={save} type="button">Save contract</button>
  </div>;
}

describe("workflow Interface typing preserves editable row identity", () => {
  it.each(["input", "output"] as const)("types the entire first %s field without remounting its controls", async (editor) => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditorHarness editor={editor} onSave={onSave} />);
    // An empty contract shows no phantom row: Add opens the row it adds.
    expect(screen.queryByRole("textbox", { name: "Name" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: editor === "input" ? "Add input" : "Add output" }));
    const name = screen.getByRole("textbox", { name: "Name" });
    const label = screen.getByRole("textbox", { name: "Label" });
    const description = screen.getByRole("textbox", { name: "Field description" });
    await user.type(name, "order_id");
    expect(name).toHaveValue("order_id");
    expect(name).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Name" })).toBe(name);
    expect(screen.getByRole("textbox", { name: "Label" })).toBe(label);
    await user.type(label, "Order ID");
    expect(label).toHaveValue("Order ID");
    expect(label).toHaveFocus();
    await user.type(description, "Customer order identifier");
    expect(description).toHaveValue("Customer order identifier");
    expect(description).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Save contract" }));
    expect(onSave).toHaveBeenCalledWith([{
      name: "order_id", type: "string", required: false,
      label: "Order ID", description: "Customer order identifier",
    }]);
  });

  it("keeps the empty input focused across an unrelated parent render", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    const view = render(<EditorHarness editor="input" onSave={onSave} />);
    await user.click(screen.getByRole("button", { name: "Add input" }));
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.click(name);
    view.rerender(<EditorHarness editor="input" onSave={onSave} revision={1} />);
    expect(screen.getByRole("textbox", { name: "Name" })).toBe(name);
    expect(name).toHaveFocus();
    await user.keyboard("order_id");
    expect(name).toHaveValue("order_id");
  });
});

describe("returned files in the workflow interface", () => {
  it("lists each returned file with its step source, read only and without Add", () => {
    const view = createWorkflowInterfaceRenderers({
      workflowInterfaceControlIdPrefix: "returned",
      workflowInterfaceReturnedFiles: [{
        name: "invoice_pdf", type: "file", required: false,
        source: { step_id: "render", step_name: "Render invoice", result: "file", target: "state.invoice_pdf" },
      }],
    });
    render(<>{view.renderWorkflowReturnedFiles()}</>);
    const block = screen.getByTestId("workflow-contract-returned-files");
    expect(block).toHaveTextContent("Returned files");
    expect(block).toHaveTextContent("1 file");
    expect(block).toHaveTextContent("invoice_pdf");
    expect(block).toHaveTextContent("Render invoice → file");
    expect(screen.queryByRole("button", { name: /Add/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("says how to return a file when none is ticked", () => {
    const view = createWorkflowInterfaceRenderers({ workflowInterfaceControlIdPrefix: "returned" });
    render(<>{view.renderWorkflowReturnedFiles()}</>);
    expect(screen.getByText("No returned files.")).toBeVisible();
    expect(screen.getByText(/tick “Return from workflow” on the step result/)).toBeVisible();
  });
});
