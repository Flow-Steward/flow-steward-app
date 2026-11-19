/* eslint-disable testing-library/no-node-access, testing-library/no-container -- fields are addressed by their data-field frame, as the dialog names them. */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";

import type { FormFieldView, FormFiles, FormValues } from "./form-field";
import { WorkflowInputFields } from "./workflow-input-fields";

function field(name: string, kind: string, widget: string, extra: Partial<FormFieldView> = {}): FormFieldView {
  return { name, kind, widget, label: name, ...extra };
}

/** The form with its own state, as the dialog holds it; the latest values are exposed for checks. */
function Harness({ fields, initial = {}, onState }: Readonly<{ fields: FormFieldView[]; initial?: FormValues; onState?: (values: FormValues, files: FormFiles) => void }>) {
  const [values, setValues] = useState<FormValues>(initial);
  const [files, setFiles] = useState<FormFiles>({});
  onState?.(values, files);
  return (
    <WorkflowInputFields
      fields={fields}
      files={files}
      onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
      onFiles={(name, next) => setFiles((current) => ({ ...current, [name]: next }))}
      values={values}
    />
  );
}

function frame(container: HTMLElement, name: string): HTMLElement {
  const element = container.querySelector(`[data-field="${name}"]`);
  if (!element) throw new Error(`no field ${name}`);
  return element as HTMLElement;
}

describe("WorkflowInputFields widgets", () => {
  it("renders each widget as its own control", () => {
    const fields = [
      field("title", "string", "text", { prepend: "№", append: "pcs", placeholder: "Title", instructions: "As printed" }),
      field("notes", "string", "textarea"),
      field("body", "string", "wysiwyg"),
      field("email", "string", "email"),
      field("site", "string", "url"),
      field("amount", "number", "number"),
      field("day", "string", "date"),
      field("at", "string", "date_time"),
      field("clock", "string", "time"),
      field("kind", "select", "select", { options: ["a", "b"] }),
      field("size", "select", "radio", { options: ["s", "m"], option_labels: { s: "Small" } }),
      field("tone", "select", "button_group", { options: ["calm", "loud"] }),
      field("tags", "select_list", "checkbox", { options: ["x", "y"] }),
      field("labels", "select_list", "multiselect", { options: ["p", "q"] }),
      field("urgent", "boolean", "true_false"),
      field("scan", "file", "file"),
      field("pages", "file_list", "files"),
      field("address", "object", "group", { sub_fields: [field("city", "string", "text")] }),
    ];
    const { container } = render(<Harness fields={fields} />);
    const input = (name: string, selector = "input") => frame(container, name).querySelector(selector) as HTMLInputElement;

    expect(input("title").type).toBe("text");
    expect(input("title").placeholder).toBe("Title");
    expect(within(frame(container, "title")).getByText("№")).toBeInTheDocument();
    expect(within(frame(container, "title")).getByText("pcs")).toBeInTheDocument();
    expect(within(frame(container, "title")).getByText("As printed")).toBeInTheDocument();
    expect(input("notes", "textarea")).toBeInTheDocument();
    expect(input("body", "textarea[data-markdown]")).toBeInTheDocument();
    expect(within(frame(container, "body")).getByRole("button", { name: "Preview" })).toBeInTheDocument();
    expect(input("email").type).toBe("email");
    expect(input("site").type).toBe("url");
    expect(input("amount").type).toBe("number");
    expect(input("day").type).toBe("date");
    expect(input("at").type).toBe("datetime-local");
    expect(input("clock").type).toBe("time");
    expect(within(frame(container, "kind")).getByRole("combobox")).toBeInTheDocument();
    expect(within(frame(container, "size")).getAllByRole("radio")).toHaveLength(2);
    expect(within(frame(container, "size")).getByText("Small")).toBeInTheDocument();
    expect(within(frame(container, "tone")).getByRole("button", { name: "loud" })).toHaveAttribute("aria-pressed", "false");
    expect(within(frame(container, "tags")).getAllByRole("checkbox")).toHaveLength(2);
    expect(within(frame(container, "urgent")).getByRole("switch")).toBeInTheDocument();
    expect(input("scan").type).toBe("file");
    expect(input("scan").multiple).toBe(false);
    // Several files: a real file picker that takes many, never a text box.
    expect(input("pages").type).toBe("file");
    expect(input("pages").multiple).toBe(true);
    expect(frame(container, "pages").querySelector('input[type="text"], textarea')).toBeNull();
    expect(within(frame(container, "address")).getByRole("group")).toBeInTheDocument();
    expect(frame(container, "address").querySelector('[data-field="city"] input')).toBeInTheDocument();
  });

  it("keeps the chosen files listed and adds later picks", () => {
    let latest: FormFiles = {};
    const { container } = render(<Harness fields={[field("pages", "file_list", "files")]} onState={(_, files) => { latest = files; }} />);
    const picker = frame(container, "pages").querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(picker, { target: { files: [new File(["1"], "page-1.jpg"), new File(["2"], "page-2.jpg")] } });
    fireEvent.change(picker, { target: { files: [new File(["3"], "page-3.jpg")] } });
    const list = screen.getByRole("list", { name: "Chosen files for pages" });
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringContaining("page-1.jpg"),
      expect.stringContaining("page-2.jpg"),
      expect.stringContaining("page-3.jpg"),
    ]);
    expect(latest.pages?.map((file) => file.name)).toEqual(["page-1.jpg", "page-2.jpg", "page-3.jpg"]);
  });

  it("does not render hidden, fixed or condition-hidden fields", () => {
    const fields = [
      field("urgent", "boolean", "true_false"),
      field("urgent_reason", "string", "text", { conditional_logic: [[{ field: "urgent", operator: "==", value: true }]] }),
      field("ocr_model", "string", "text", { hidden: true }),
      field("tenant", "string", "text", { fixed: true, hidden: true }),
    ];
    const { container } = render(<Harness fields={fields} />);
    expect(container.querySelector('[data-field="ocr_model"]')).toBeNull();
    expect(container.querySelector('[data-field="tenant"]')).toBeNull();
    expect(container.querySelector('[data-field="urgent_reason"]')).toBeNull();
    fireEvent.click(screen.getByRole("switch", { name: "urgent" }));
    expect(container.querySelector('[data-field="urgent_reason"]')).toBeInTheDocument();
  });

  it("adds and removes repeater rows within the contract's bounds", () => {
    let latest: FormValues = {};
    const companies = field("companies", "object_list", "repeater", {
      layout: "table",
      button_label: "Add company",
      min_items: 1,
      max_items: 2,
      sub_fields: [field("name", "string", "text", { required: true }), field("website", "string", "url")],
    });
    render(<Harness fields={[companies]} initial={{ companies: [{}] }} onState={(values) => { latest = values; }} />);
    const add = screen.getByRole("button", { name: "Add company" });
    expect(screen.getByRole("button", { name: "Remove row 1" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "name, companies, row 1" }), { target: { value: "Acme" } });
    fireEvent.click(add);
    expect(add).toBeDisabled();
    expect(screen.getAllByRole("row")).toHaveLength(3);
    fireEvent.change(screen.getByRole("textbox", { name: "name, companies, row 2" }), { target: { value: "Beta" } });
    expect(latest.companies).toEqual([{ name: "Acme" }, { name: "Beta" }]);
    fireEvent.click(screen.getByRole("button", { name: "Remove row 1" }));
    expect(latest.companies).toEqual([{ name: "Beta" }]);
    expect(add).toBeEnabled();
    expect(screen.getByRole("button", { name: "Remove row 1" })).toBeDisabled();
  });

  it("lays out block rows with labelled sub-fields", () => {
    const companies = field("companies", "object_list", "repeater", { layout: "block", sub_fields: [field("name", "string", "text")] });
    const { container } = render(<Harness fields={[companies]} initial={{ companies: [{}, {}] }} />);
    expect(screen.getAllByRole("group", { name: /companies, row/ })).toHaveLength(2);
    expect(container.querySelectorAll('[data-layout="block"] [data-field="name"]')).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Add row" })).toBeEnabled();
  });
});
