import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ProjectWorkflowInputFields } from "@/pages/projects/detail/project-workflow-input-fields";
import { parseWorkflowInputs } from "@/pages/projects/detail/project-detail-helpers";

const fields = [
  { name: "record", label: "Record", type: "object" },
  { name: "rows", label: "Rows", type: "array" },
];
const original = {
  record: { count: 0, active: false, missing: null, nested: { list: [1, true] }, text: "001" },
  rows: [0, false, null, { name: "001" }, [2], "before"],
};

function Harness() {
  const [stored, setStored] = useState<Record<string, unknown>>(original);
  const [open, setOpen] = useState(true);
  const [values, setValues] = useState<Record<string, string>>({ record: JSON.stringify(original.record), rows: JSON.stringify(original.rows) });
  const [errors, setErrors] = useState<Record<string, string>>({});
  return <>
    {open ? <form onSubmit={(event) => {
      event.preventDefault();
      if (Object.values(errors).some(Boolean)) return;
      const result = parseWorkflowInputs(fields, values);
      if (!result.error) { setStored(result.inputs); setOpen(false); }
    }}>
      <ProjectWorkflowInputFields workflowInputFields={fields} effectiveWorkflowInputValues={values}
        setWorkflowInputValues={setValues} onValidationChange={(name, error) => setErrors((current) => ({ ...current, [name]: error }))} />
      <button type="submit">Save inputs</button>
    </form> : <button onClick={() => {
      setValues({ record: JSON.stringify(stored.record), rows: JSON.stringify(stored.rows) });
      setOpen(true);
    }}>Reopen saved inputs</button>}
    <output aria-label="Stored inputs">{JSON.stringify(stored)}</output>
  </>;
}

it("keeps a new object row while its key is still being typed", async () => {
  render(<Harness />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Add row" }));
  const key = screen.getByRole("textbox", { name: "Record key 6" });
  await user.type(key, "extra");
  await user.type(screen.getByRole("textbox", { name: "Record value 6" }), "more");
  await user.click(screen.getByRole("button", { name: "Save inputs" }));
  const saved = JSON.parse(screen.getByLabelText("Stored inputs").textContent || "{}");
  expect(saved).toEqual({ ...original, record: { ...original.record, extra: "more" } });
  await user.click(screen.getByRole("button", { name: "Reopen saved inputs" }));
  expect(screen.getByRole("textbox", { name: "Record key 6" })).toHaveValue("extra");
  expect(screen.getByRole("textbox", { name: "Record value 6" })).toHaveValue("more");
});

it("preserves nested member types when editing a string and adding an array item", async () => {
  render(<Harness />);
  const user = userEvent.setup();
  await user.clear(screen.getByRole("textbox", { name: "Record value 5" }));
  await user.type(screen.getByRole("textbox", { name: "Record value 5" }), "true");
  await user.clear(screen.getByRole("textbox", { name: "Rows item 6" }));
  await user.type(screen.getByRole("textbox", { name: "Rows item 6" }), "after");
  await user.click(screen.getByRole("button", { name: "Add item" }));
  await user.type(screen.getByRole("textbox", { name: "Rows item 7" }), "new");
  await user.click(screen.getByRole("button", { name: "Save inputs" }));
  expect(await screen.findByRole("button", { name: "Reopen saved inputs" })).toBeInTheDocument();
  expect(JSON.parse(screen.getByLabelText("Stored inputs").textContent || "{}")).toEqual({
    record: { ...original.record, text: "true" }, rows: [0, false, null, { name: "001" }, [2], "after", "new"],
  });
  await user.click(screen.getByRole("button", { name: "Reopen saved inputs" }));
  expect(screen.getByRole("textbox", { name: "Rows item 4" })).toHaveValue('{"name":"001"}');
  expect(screen.getByRole("textbox", { name: "Rows item 5" })).toHaveValue("[2]");
});

it("keeps invalid typed JSON visible and prevents saving until corrected", async () => {
  render(<Harness />);
  const user = userEvent.setup();
  await user.clear(screen.getByRole("textbox", { name: "Record value 4" }));
  await user.type(screen.getByRole("textbox", { name: "Record value 4" }), "invalid");
  await user.click(screen.getByRole("button", { name: "Save inputs" }));
  expect(screen.queryByRole("button", { name: "Reopen saved inputs" })).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Record value 4" })).toHaveValue("invalid");
  expect(JSON.parse(screen.getByLabelText("Stored inputs").textContent || "{}")).toEqual(original);
  await user.clear(screen.getByRole("textbox", { name: "Record value 4" }));
  await user.paste('{"ok":false}');
  await user.click(screen.getByRole("button", { name: "Save inputs" }));
  expect(JSON.parse(screen.getByLabelText("Stored inputs").textContent || "{}")).toEqual({ ...original, record: { ...original.record, nested: { ok: false } } });
});

function ObjectForm({ initial = { first: "keep" } }: { initial?: Record<string, unknown> }) {
  const [values, setValues] = useState<Record<string, string>>({ payload: JSON.stringify(initial) });
  return <>
    <ProjectWorkflowInputFields
      workflowInputFields={[{ name: "payload", type: "object", required: true }]}
      effectiveWorkflowInputValues={values}
      setWorkflowInputValues={setValues}
    />
    <output aria-label="Serialized inputs">{values.payload}</output>
  </>;
}

it("keeps added object rows and other values through a temporary empty key", async () => {
  const user = userEvent.setup();
  render(<ObjectForm />);
  await user.click(screen.getByRole("button", { name: "Add row" }));
  expect(screen.getByLabelText("payload key 2")).toHaveValue("");
  await user.type(screen.getByLabelText("payload key 2"), "second");
  await user.type(screen.getByLabelText("payload value 2"), "new");
  await user.clear(screen.getByLabelText("payload key 1"));
  expect(screen.getByLabelText("payload value 1")).toHaveValue("keep");
  expect(screen.getByLabelText("payload key 2")).toHaveValue("second");
  expect(screen.getByLabelText("payload value 2")).toHaveValue("new");
  await user.type(screen.getByLabelText("payload key 1"), "renamed");
  expect(JSON.parse(screen.getByLabelText("Serialized inputs").textContent || "{}"))
    .toEqual({ renamed: "keep", second: "new" });
  await user.click(screen.getByRole("button", { name: "Add row" }));
  expect(screen.getByLabelText("payload key 3")).toHaveValue("");
  expect(screen.getByLabelText("payload value 2")).toHaveValue("new");
});

it("preserves existing numeric and boolean object values while another row is edited", async () => {
  const user = userEvent.setup();
  render(<ObjectForm initial={{ first: "keep", count: 7, active: true }} />);
  await user.type(screen.getByLabelText("payload value 1"), "-edited");
  expect(JSON.parse(screen.getByLabelText("Serialized inputs").textContent || "{}"))
    .toEqual({ first: "keep-edited", count: 7, active: true });
});

it("retains an entered value until its key is filled without publishing lost data", async () => {
  const user = userEvent.setup();
  render(<ObjectForm />);
  await user.click(screen.getByRole("button", { name: "Add row" }));
  await user.type(screen.getByLabelText("payload value 2"), "pending");
  expect(screen.getByLabelText("payload value 2")).toHaveValue("pending");
  expect(screen.getByRole("alert")).toHaveTextContent(/key.*non-empty value/i);
  expect(JSON.parse(screen.getByLabelText("Serialized inputs").textContent || "{}"))
    .toEqual({ first: "keep" });
  await user.type(screen.getByLabelText("payload key 2"), "second");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(JSON.parse(screen.getByLabelText("Serialized inputs").textContent || "{}"))
    .toEqual({ first: "keep", second: "pending" });
});
