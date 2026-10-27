import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { definitionFromWorkflow } from "../builder/change-set";
import { useWorkflowChangeSet } from "./detail-change-set";
import type { GroupDraft } from "./detail-drawer-controller";
import { useWorkflowGroupActions } from "./detail-group-actions";
import { GroupEditorSurface } from "./detail-group-editor-surface";

const label = "Lanes running at once (optional)";
const definition = definitionFromWorkflow({ workflow_id: "workflow_a", revision: 7, steps: [], groups: [] });
function Harness({ kind = "group", value = "0", over = "state.items", lanes = "first, second", editing = false }: { kind?: GroupDraft["containerKind"]; value?: string; over?: string; lanes?: string; editing?: boolean }) {
  const [groupDraft, setGroupDraft] = useState<GroupDraft>({ containerKind: kind, bodyKind: "lanes", concurrencyLimit: value, displayName: "Workers", description: "", groupId: "workers", lanes, over, itemName: "item", indexName: "", parentGroupId: "" });
  const [groupCreateMode, setGroupCreateMode] = useState(!editing);
  const [error, setSaveError] = useState("");
  const selectedGroup = editing ? { local: true, containerKind: kind, groupId: "workers", bodyKind: "lanes", lanes: [], steps: [], raw: { body: { kind: "lanes", lanes: [{ lane_id: "first" }, { lane_id: "second" }] } } } : null;
  const changeSet = useWorkflowChangeSet({ workflowId: "workflow_a", serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces: async () => ({}) });
  const actions = useWorkflowGroupActions({ groupDraft, setGroupDraft, groupCreateMode, setGroupCreateMode, changeSet, selectedGroup, setSelectedGroupId: vi.fn(), setSaveError, setSaveNotice: vi.fn() });
  return <>
    <GroupEditorSurface ctx={{ groupDraft, setGroupDraft, groupCreateMode, selectedGroup, drawerMode: "group", drawerTab: "settings", preview: changeSet.preview, ...actions,
      // An edited group: Save group rests while nothing differs, and this harness starts from a changed draft.
      groupFormDirty: editing, renderWorkflowVariableInput: ({ ariaLabel, ariaRequired, ariaInvalid, ariaDescribedBy, name, value: inputValue, onChange }: { ariaLabel: string; ariaRequired?: boolean; ariaInvalid?: boolean; ariaDescribedBy?: string; name: string; value: string; onChange: (value: string) => void }) => <input aria-label={ariaLabel} aria-required={ariaRequired || undefined} aria-invalid={ariaInvalid || undefined} aria-describedby={ariaDescribedBy} name={name} value={inputValue} onChange={(event) => onChange(event.target.value)} /> }} />
    <output aria-label="Pending operations">{JSON.stringify(changeSet.operations)}</output>
    <output aria-label="Save error">{error}</output>
  </>;
}

beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected request"));
});
afterEach(() => vi.restoreAllMocks());

describe.each(["group", "for_each"] as const)("%s concurrency field", (kind) => {
  it.each([false, true])("keeps invalid raw input, associates the error, and focuses it on %s edit submit", (editing) => {
    render(<Harness kind={kind} editing={editing} />);
    const input = screen.getByRole("textbox", { name: label });
    const saveName = editing ? kind === "group" ? "Save group" : "Save For each" : kind === "group" ? "Add group to changes" : "Add For each to changes";
    expect(input).not.toBeRequired();
    expect(input).not.toHaveAttribute("aria-invalid", "true");
    fireEvent.click(screen.getByRole("button", { name: saveName }));
    expect(input).toHaveValue("0");
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("aria-invalid", "true");
    const descriptionId = input.getAttribute("aria-describedby");
    expect(descriptionId).toBeTruthy();
    const error = screen.getByRole("alert");
    expect(error).toHaveAttribute("id", descriptionId);
    expect(error).toHaveTextContent(/whole number greater than zero/i);
    expect(screen.getByLabelText("Pending operations")).toHaveTextContent("[]");
    expect(globalThis.fetch).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "2" } });
    expect(input).toHaveValue("2");
    expect(input).not.toHaveAttribute("aria-invalid", "true");
    expect(input).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByText(/whole number greater than zero/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: saveName }));
    const operations = JSON.parse(screen.getByLabelText("Pending operations").textContent!);
    const operation = operations.at(-1);
    const container = editing ? operation.patch : kind === "group" ? operation.group : operation.for_each;
    expect(container.body.concurrency_limit).toBe(2);
    expect(screen.getByLabelText("Save error")).toHaveTextContent(/^$/);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it.each(["1.5", "2x"])("rejects %s through the real Add button and allows a blank correction", (value) => {
    render(<Harness kind={kind} value={value} />);
    const input = screen.getByRole("textbox", { name: label });
    const saveName = kind === "group" ? "Add group to changes" : "Add For each to changes";
    fireEvent.click(screen.getByRole("button", { name: saveName }));
    expect(input).toHaveFocus();
    expect(input).toHaveValue(value);
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Pending operations")).toHaveTextContent("[]");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: saveName }));
    const operation = JSON.parse(screen.getByLabelText("Pending operations").textContent!)[0];
    expect((kind === "group" ? operation.group : operation.for_each).body).not.toHaveProperty("concurrency_limit");
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});

it.each([
  ["for_each", "", "", "Enter the list the For each goes through."],
  ["for_each", "state.items", "", "Add at least one lane."],
  ["group", "state.items", "", "Add at least one lane."],
] as const)("preserves earlier %s invalid-field validation", (kind, over, lanes, error) => {
  render(<Harness kind={kind} over={over} lanes={lanes} />);
  fireEvent.click(screen.getByRole("button", { name: kind === "group" ? "Add group to changes" : "Add For each to changes" }));
  expect(screen.getByLabelText("Save error")).toHaveTextContent(error);
  expect(screen.getByRole("textbox", { name: label })).not.toHaveFocus();
  expect(screen.getByLabelText("Pending operations")).toHaveTextContent("[]");
});

it("keeps the optional For each index name editable without changing its staged value", () => {
  render(<Harness kind="for_each" value="" />);
  const index = screen.getByRole("textbox", { name: "Index name (optional)" });
  expect(index).not.toBeRequired();
  expect(index).toHaveValue("");
  fireEvent.change(index, { target: { value: "position" } });
  fireEvent.click(screen.getByRole("button", { name: "Add For each to changes" }));
  const operation = JSON.parse(screen.getByLabelText("Pending operations").textContent!)[0];
  expect(operation.for_each).toMatchObject({ index_name: "position", item_name: "item", over: "state.items" });
  expect(globalThis.fetch).not.toHaveBeenCalled();
});

it("uses the active locale for the associated field error", () => {
  const translated = "Įveskite sveikąjį skaičių, didesnį už nulį, arba palikite lauką tuščią.";
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", t: (key, fallback = "") => key === "react.workflows.concurrency_positive_integer" ? translated : fallback }}><Harness /></I18nContext.Provider>);
  fireEvent.click(screen.getByRole("button", { name: "Add group to changes" }));
  expect(screen.getByText(translated)).toBeInTheDocument();
});


it.each([false, true])("associates the required For each source before concurrency for editing=%s", (editing) => {
  render(<Harness kind="for_each" over="   " value="0" editing={editing} />);
  const source = screen.getByRole("textbox", { name: "Loop source" });
  const save = screen.getByRole("button", { name: editing ? "Save For each" : "Add For each to changes" });
  expect(source).toBeRequired();
  expect(source).toHaveAttribute("aria-invalid", "true");
  expect(source).toHaveAccessibleDescription("Enter the list the For each goes through.");
  fireEvent.click(save);
  expect(source).toHaveFocus();
  expect(screen.getByLabelText("Save error")).toHaveTextContent("Enter the list the For each goes through.");
  expect(screen.getByRole("textbox", { name: label })).not.toHaveFocus();
  expect(screen.getByLabelText("Pending operations")).toHaveTextContent("[]");
  expect(globalThis.fetch).not.toHaveBeenCalled();

  fireEvent.change(source, { target: { value: "state.orders" } });
  expect(source).toBeRequired();
  expect(source).not.toHaveAttribute("aria-invalid", "true");
  expect(source).not.toHaveAttribute("aria-describedby");
  fireEvent.click(save);
  // The next existing invalid field retains its original behavior and priority.
  const concurrency = screen.getByRole("textbox", { name: label });
  expect(concurrency).toHaveFocus();
  expect(concurrency).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByLabelText("Pending operations")).toHaveTextContent("[]");
  fireEvent.change(concurrency, { target: { value: "2" } });
  fireEvent.click(save);
  const operations = JSON.parse(screen.getByLabelText("Pending operations").textContent || "[]");
  const operation = operations.at(-1);
  expect(editing ? operation.patch : operation.for_each).toMatchObject({ over: "state.orders", item_name: "item" });
  expect(globalThis.fetch).not.toHaveBeenCalled();
});

it("keeps a blank Item name optional and stages the existing item default", () => {
  render(<Harness kind="for_each" value="" />);
  const item = screen.getByRole("textbox", { name: "Item name" });
  const description = screen.getByRole("textbox", { name: "Description" });
  expect(item).not.toBeRequired();
  expect(description).not.toBeRequired();
  fireEvent.change(item, { target: { value: "   " } });
  fireEvent.click(screen.getByRole("button", { name: "Add For each to changes" }));
  const operation = JSON.parse(screen.getByLabelText("Pending operations").textContent || "[]")[0];
  expect(operation.for_each).toMatchObject({ item_name: "item", over: "state.items", index_name: null });
  expect(operation.for_each.body).not.toHaveProperty("concurrency_limit");
  expect(globalThis.fetch).not.toHaveBeenCalled();
});
