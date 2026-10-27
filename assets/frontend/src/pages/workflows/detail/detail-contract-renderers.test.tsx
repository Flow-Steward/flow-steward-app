/* Item 11: Set variable -> Assignments. "+ Add" filled the new row's path with the literal
   text "target_path", and clearing a path removed its whole row: the rows were saved
   straight into the assignments object, which cannot hold an empty (or a repeated) key. */
import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import { createWorkflowContractRenderers } from "@/pages/workflows/detail/detail-contract-renderers";
import { objectFromEntries } from "@/pages/workflows/detail/detail-helpers";

type VariableInputProps = {
  ariaDescribedBy?: string;
  ariaInvalid?: boolean;
  ariaLabel: string;
  onChange: (value: string) => void;
  placeholder: string;
  value: string;
};

function AssignmentsHarness({ initial = {} }: Readonly<{ initial?: Record<string, unknown> }>) {
  const [config, setConfig] = useState<Record<string, unknown>>({ assignments: initial });
  const { renderConfigMapEditor } = createWorkflowContractRenderers({
    renderWorkflowVariableInput: ({ ariaDescribedBy, ariaInvalid, ariaLabel, onChange, placeholder, value }: VariableInputProps) => (
      <input
        aria-describedby={ariaDescribedBy}
        aria-invalid={ariaInvalid || undefined}
        aria-label={ariaLabel}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        value={value}
      />
    ),
    replaceStepKindConfigField: vi.fn(),
    setWorkflowContractSectionsExpanded: vi.fn(),
    stepKindConfig: config,
    updateConfigMapField: (_stepKind: string, field: string, rows: Array<{ key: string; value: string }>) =>
      setConfig((current) => ({ ...current, [field]: objectFromEntries(rows) })),
    workflowContractSectionsExpanded: {},
  });
  return (
    <>
      {renderConfigMapEditor("set_variable", "assignments", "Assignments", "state.my_variable", { key: "path" })}
      <output aria-label="Saved assignments">{JSON.stringify(config.assignments)}</output>
    </>
  );
}

function saved(): unknown {
  return JSON.parse(screen.getByLabelText("Saved assignments").textContent || "null");
}

const path = (row: number) => screen.getByRole("textbox", { name: `Assignments key ${row}` });
const value = (row: number) => screen.getByRole("textbox", { name: `Assignments value ${row}` });

describe("the Assignments editor keeps its rows as typed", () => {
  it("adds a row with an empty path and a real placeholder", () => {
    render(<AssignmentsHarness initial={{ "state.plan": true }} />);

    fireEvent.click(screen.getByRole("button", { name: "Add Assignments" }));

    expect(path(2)).toHaveValue("");
    expect(path(2)).toHaveAttribute("placeholder", "state.my_variable");
    expect(screen.queryByDisplayValue(/target_path/)).not.toBeInTheDocument();
    expect(saved()).toEqual({ "state.plan": true });
  });

  it("keeps a row whose path is cleared, says why it is not saved, and saves it again once named", () => {
    render(<AssignmentsHarness initial={{ "state.plan": "weekly" }} />);

    fireEvent.change(path(1), { target: { value: "" } });

    expect(path(1)).toHaveValue("");
    expect(value(1)).toHaveValue("weekly");
    expect(saved()).toEqual({});
    expect(path(1)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Enter a path for this row. A row without one is not saved.")).toBeInTheDocument();

    fireEvent.change(path(1), { target: { value: "state.schedule" } });

    expect(saved()).toEqual({ "state.schedule": "weekly" });
    expect(screen.queryByText(/A row without one is not saved/)).not.toBeInTheDocument();
  });

  it("saves typed paths and values as the assignments object", () => {
    render(<AssignmentsHarness />);

    expect(path(1)).toHaveValue("");
    fireEvent.change(path(1), { target: { value: "state.count" } });
    fireEvent.change(value(1), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Add Assignments" }));
    fireEvent.change(path(2), { target: { value: "state.copy" } });
    fireEvent.change(value(2), { target: { value: '{"source":"state.inputs.products"}' } });

    expect(saved()).toEqual({ "state.count": 3, "state.copy": { source: "state.inputs.products" } });
    // What was typed stays as typed; it is not re-formatted from the saved object.
    expect(value(2)).toHaveValue('{"source":"state.inputs.products"}');
  });

  it("flags a repeated path and saves only the first row that sets it", () => {
    render(<AssignmentsHarness initial={{ "state.plan": "a" }} />);

    fireEvent.click(screen.getByRole("button", { name: "Add Assignments" }));
    fireEvent.change(path(2), { target: { value: "state.plan" } });
    fireEvent.change(value(2), { target: { value: "b" } });

    expect(saved()).toEqual({ "state.plan": "a" });
    expect(path(2)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("An earlier row already sets this path. Only the first one is saved.")).toBeInTheDocument();
    expect(path(1)).not.toHaveAttribute("aria-invalid");

    fireEvent.click(screen.getByRole("button", { name: "Remove Assignments 1" }));

    expect(path(1)).toHaveValue("state.plan");
    expect(saved()).toEqual({ "state.plan": "b" });
    expect(screen.queryByText(/already sets this path/)).not.toBeInTheDocument();
  });

  it("removes a row with the quiet danger button at its end, and keeps one empty row", () => {
    render(<AssignmentsHarness initial={{ "state.plan": true }} />);

    const remove = screen.getByRole("button", { name: "Remove Assignments 1" });
    expect(remove).toHaveClass("fs-button", "ghost-danger", "sm");
    // At the end of its row: after the row's value, before the next row.
    fireEvent.click(screen.getByRole("button", { name: "Add Assignments" }));
    const order = [value(1), remove, path(2)];
    expect(order.map((element) => order[0].compareDocumentPosition(element))).toEqual([0, 4, 4]);
    expect(remove.compareDocumentPosition(path(2))).toBe(4);
    fireEvent.click(screen.getByRole("button", { name: "Remove Assignments 2" }));

    fireEvent.click(remove);

    expect(saved()).toEqual({});
    expect(path(1)).toHaveValue("");
    expect(screen.getByRole("button", { name: "Remove Assignments 1" })).toBeDisabled();
  });
});
