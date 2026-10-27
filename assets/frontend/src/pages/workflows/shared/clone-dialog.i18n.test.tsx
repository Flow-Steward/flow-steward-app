import { fireEvent, render, screen } from "@testing-library/react";

import { I18nContext, fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { WorkflowCloneDialog } from "./clone-dialog";
import { ResourceSelectionList } from "@/pages/registry/datasets/resource-selection-list";
import { ExtensionActionResultPanel } from "@/pages/extensions/extension-action-result-panel";

const translations: Record<string, string> = {
  "react.workflows.clone_workflow": "Klonuoti darbo eigą",
  "react.workflows.new_workflow_id": "Naujos darbo eigos ID",
  "extensions.repeater.cancel": "Atšaukti",
  "react.workflows.workflow_id_is_required": "Būtinas darbo eigos ID.",
  "react.registry.all_columns": "Visi stulpeliai",
  "registry.skill_detail.technical_details": "Techninė informacija",
  "react.extensions.succeeded": "pavyko",
};

function localeContext(locale: string): I18nContextValue {
  return { ...fallbackContext, locale, t: (key, text = "", params) => fallbackContext.t(key, locale === "lt" ? translations[key] || text : text, params) };
}

describe("platform workflow, registry and extension controls follow the active language", () => {
  it("changes the clone dialog language without changing the workflow identifier", () => {
    const dialog = (locale: string) => <I18nContext.Provider value={localeContext(locale)}><WorkflowCloneDialog sourceWorkflowId="wf_source" onClose={() => {}} onCloned={() => {}} /></I18nContext.Provider>;
    const view = render(dialog("en"));
    fireEvent.change(screen.getByLabelText("New workflow ID"), { target: { value: "ACME_custom" } });
    view.rerender(dialog("lt"));
    expect(screen.getByRole("button", { name: "Klonuoti darbo eigą" })).toBeVisible();
    expect(screen.getByLabelText("Naujos darbo eigos ID")).toHaveValue("ACME_custom");
    fireEvent.change(screen.getByLabelText("Naujos darbo eigos ID"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Klonuoti darbo eigą" }));
    expect(screen.getByText("Būtinas darbo eigos ID.")).toBeVisible();
    view.rerender(dialog("en"));
    expect(screen.getByRole("button", { name: "Clone workflow" })).toBeVisible();
  });

  it("translates registry selection controls while retaining resource and column names", () => {
    render(<I18nContext.Provider value={localeContext("lt")}><ResourceSelectionList resources={[{ resource_id: "Customer ledger", resource_type: "table", columns: [{ name: "Account number" }] }]} selectedResources={{ "Customer ledger": null }} onToggleResource={() => {}} onSetAllColumns={() => {}} onToggleColumn={() => {}} /></I18nContext.Provider>);
    expect(screen.getByRole("checkbox", { name: "Customer ledger" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Visi stulpeliai" })).toBeInTheDocument();
  });

  it("translates extension result controls while retaining extension result content", () => {
    render(<I18nContext.Provider value={localeContext("lt")}><ExtensionActionResultPanel result={{ label: "Vendor action", ok: true, payload: { value: "Vendor data" } }} /></I18nContext.Provider>);
    expect(screen.getByText("Techninė informacija")).toBeInTheDocument();
    expect(screen.getByText("pavyko")).toBeVisible();
    expect(screen.getByText(/Vendor data/)).toBeInTheDocument();
  });
});
