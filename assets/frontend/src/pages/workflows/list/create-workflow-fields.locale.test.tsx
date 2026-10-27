import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { CreateWorkflowFields } from "@/pages/workflows/list/create-workflow-fields";
import { useCreateWorkflowForm } from "@/pages/workflows/list/create-workflow-state";

const english = "Workflow ID must start with a lowercase letter and use only lowercase letters, numbers, and underscores.";
const lithuanian = "Darbo eigos ID turi prasidėti mažąja raide; jame gali būti tik mažosios raidės, skaitmenys ir pabraukimo ženklai.";

function Form() {
  const form = useCreateWorkflowForm("");
  return <CreateWorkflowFields form={form} />;
}

describe("workflow ID hint", () => {
  it.each(["lt", "en"])("renders the %s hint and preserves author text", (locale) => {
    render(<I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, text = "", params) => fallbackContext.t(key, locale === "lt" && key === "react.workflows.display_workflow_id_rule" ? lithuanian : text, params) }}><Form /></I18nContext.Provider>);
    expect(screen.getByText(locale === "lt" ? lithuanian : english)).toBeVisible();
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "Owner Ąžuolas" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Workflow ID" }), { target: { value: "owner_custom_id" } });
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Owner Ąžuolas");
    expect(screen.getByRole("textbox", { name: "Workflow ID" })).toHaveValue("owner_custom_id");
  });
});
