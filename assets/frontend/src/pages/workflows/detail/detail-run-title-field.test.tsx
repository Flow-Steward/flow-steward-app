import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";

import { WorkflowSettingsSmartSetup } from "./detail-workflow-settings-smart-setup";

function Settings({ initial = "" }: Readonly<{ initial?: string }>) {
  const [runTitle, setRunTitle] = useState(initial);
  return <AuthContext.Provider value={{ session: { user: { roles: ["admin"] } } } as unknown as AuthContextValue}><WorkflowSettingsSmartSetup ctx={{
    drawerMode: "workflow-settings", setDrawerTab: () => undefined, workflowFormDirty: false,
    workflowId: "scrape", selectedProjectId: "project-1", advancedMode: false, drawerTab: "smart-setup",
    workflowDisplayNameDraft: "Scrape a site", workflowDescriptionDraft: "", workflowEnabledDraft: true,
    workflowInterfaceEnabledDraft: true, workflowInterfaceControlIdPrefix: "test",
    workflowInterfaceInputModeDraft: "fields", workflowInterfaceStrictInputsDraft: true,
    workflowInterfaceInputsDraft: [
      { draftId: "1", name: "site_url", type: "string" },
      { draftId: "2", name: "login", type: "secret" },
    ],
    workflowInterfaceOutputsDraft: [], workflowInterfaceReturnedFiles: [],
    workflowInterfaceTriggerChannelsDraft: ["ui"], setWorkflowInterfaceTriggerChannelsDraft: () => undefined,
    workflowRunTitleDraft: runTitle, setWorkflowRunTitleDraft: setRunTitle,
    renderWorkflowContractSection: () => null, renderWorkflowInterfaceFieldEditor: () => null,
    renderWorkflowReturnedFiles: () => null, handleSaveWorkflowSettings: async () => undefined,
  }} /></AuthContext.Provider>;
}

it("edits the run title next to the workflow's name and lists the inputs it may show", async () => {
  const user = userEvent.setup();
  render(<Settings initial="{{ inputs.site_url }}" />);
  const field = screen.getByLabelText("Run title");
  expect(field).toHaveValue("{{ inputs.site_url }}");
  expect(field).not.toHaveAttribute("aria-invalid");
  expect(screen.getByText(/Inputs: \{\{ inputs\.site_url \}\}\./)).toBeInTheDocument();
  expect(screen.queryByText(/inputs\.login/)).not.toBeInTheDocument();
  await user.clear(field);
  await user.type(field, "Scrape");
  expect(field).toHaveValue("Scrape");
});

it("says why a template that reads a secret or an unknown input will not save", () => {
  render(<Settings initial="{{ inputs.login }} {{ inputs.region }}" />);
  expect(screen.getByLabelText("Run title")).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByText("The run title cannot show the secret input login.")).toBeInTheDocument();
  expect(screen.getByText("region is not a declared input.")).toBeInTheDocument();
});
