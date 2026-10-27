import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { ContractEditorCard, type ContractDraftItem } from "@/pages/registry/skills/detail/contract-editor";

it.each([
  ["text", "Tekstas"],
  ["json", "JSON"],
  ["list", "Sąrašas"],
  ["number", "skaičius"],
  ["boolean", "loginė reikšmė"],
])("localizes the contract type %s while saving the canonical draft value", async (valueType, label) => {
  const messages: Record<string, string> = {
    "trash.columns.type": "Tipas",
    "react.registry.text": "Tekstas",
    "react.registry.contract_type_json": "JSON",
    "react.workflows.list": "Sąrašas",
    "react.workflows.number": "skaičius",
    "react.workflows.boolean": "loginė reikšmė",
  };
  const onSave = vi.fn();
  function DraftEditor() {
    const [items, setItems] = useState<ContractDraftItem[]>([{
      contract_id: "",
      description: "Owner description",
      name: "owner_field",
      required: true,
      type: "text",
    }]);
    return <ContractEditorCard
      addLabel="Pridėti"
      contractOptions={[]}
      emptyMessage="Tuščia"
      fieldOptions={[]}
      items={items}
      onAdd={() => undefined}
      onChange={(index, next) => setItems((current) => current.map((item, position) => position === index ? { ...item, ...next } : item))}
      onRemove={() => undefined}
      onSave={() => onSave(items)}
      requiredByDefault
      saveLabel="Išsaugoti"
      saveStatus=""
      saving={false}
      title="Sutartis"
    />;
  }
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><DraftEditor /></I18nContext.Provider>);
  const user = userEvent.setup();

  expect(screen.getByRole("combobox", { name: "Tipas" })).toHaveTextContent("Tekstas");
  await user.click(screen.getByRole("combobox", { name: "Tipas" }));
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
    "Tekstas", "JSON", "Sąrašas", "skaičius", "loginė reikšmė",
  ]);
  await user.click(screen.getByRole("option", { name: label }));
  expect(screen.getByRole("combobox", { name: "Tipas" })).toHaveTextContent(label);
  await user.click(screen.getByRole("button", { name: "Išsaugoti" }));
  expect(onSave).toHaveBeenCalledWith([{
    contract_id: "", description: "Owner description", name: "owner_field", required: true, type: valueType,
  }]);
});

it.each([
  { label: "Catalog Field", search: "Search catalog fields", expected: "Nė vienas katalogo laukas neatitinka paieškos.", english: "No catalog fields match your search." },
  { label: "Data Contract", search: "Search data contracts", expected: "Nė viena duomenų sutartis neatitinka paieškos.", english: "No data contracts match your search." },
])("localizes the $label unmatched-search state without replacing custom field names", async ({ label, search, expected, english }) => {
  const messages: Record<string, string> = { "react.registry.no_catalog_fields_match_search": "Nė vienas katalogo laukas neatitinka paieškos.", "react.registry.no_data_contracts_match_search": "Nė viena duomenų sutartis neatitinka paieškos." };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><ContractEditorCard addLabel="Pridėti" contractOptions={[{ id: "contract-1", label: "Fixture contract" }]} emptyMessage="Tuščia" fieldOptions={[{ contractId: "contract-1", description: "Fixture description", fieldKey: "payload", label: "Fixture payload", valueType: "json" }]} items={[{ contract_id: "", description: "", name: "custom_payload", required: true, type: "json" }]} onAdd={() => undefined} onChange={() => undefined} onRemove={() => undefined} onSave={() => undefined} requiredByDefault saveLabel="Išsaugoti" saveStatus="" saving={false} title="Sutartis" /></I18nContext.Provider>);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: label }));
  await user.type(screen.getByPlaceholderText(search), "no-such-fixture");
  expect(screen.getByText(expected)).toBeInTheDocument();
  expect(screen.queryByText(english)).not.toBeInTheDocument();
  expect(screen.getByDisplayValue("custom_payload")).toBeInTheDocument();
});

it("localizes name and description placeholders while preserving the user's contract values", () => {
  const messages: Record<string, string> = { "settings.mcp.fields.name": "Pavadinimas", "registry.workflows.simulation.description": "Aprašymas" };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><ContractEditorCard addLabel="Pridėti" contractOptions={[]} emptyMessage="Tuščia" fieldOptions={[]} items={[{ contract_id: "", description: "Owner description", name: "owner_field", required: true, type: "text" }]} onAdd={() => undefined} onChange={() => undefined} onRemove={() => undefined} onSave={() => undefined} requiredByDefault saveLabel="Išsaugoti" saveStatus="" saving={false} title="Sutartis" /></I18nContext.Provider>);
  expect(screen.getByLabelText("Pavadinimas")).toHaveAttribute("placeholder", "Pavadinimas");
  expect(screen.getByLabelText("Aprašymas")).toHaveAttribute("placeholder", "Aprašymas");
  expect(screen.getByDisplayValue("owner_field")).toBeInTheDocument();
  expect(screen.getByDisplayValue("Owner description")).toBeInTheDocument();
});
