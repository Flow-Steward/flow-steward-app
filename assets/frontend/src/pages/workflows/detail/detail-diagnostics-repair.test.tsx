import { fireEvent, screen } from "@testing-library/react";

import {
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowValidation,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

const missingSource = "message.step.deleted.artifacts.summary";

function installDefinition() {
  mockedFetchWorkflowDetail.mockResolvedValue(
    workflowDetailFixture({
      enabled: false,
      steps: [
        { step_id: "normalize", display_name: "Normalize", step_kind: "agent", agent_id: "writer" },
        { step_id: "per_item", display_name: "Per item work", step_kind: "agent", group_id: "items" },
      ],
      groups: [
        {
          group_id: "items",
          display_name: "Items",
          body: { kind: "sequence", entry: { kind: "step", id: "per_item" } },
        },
      ],
    }),
  );
  mockedFetchWorkflowValidation.mockResolvedValue({
    validation: {
      valid: false,
      errors: [
        {
          code: "workflow.step_required_input_unavailable",
          input_name: "summary",
          message: "The summary source is unavailable",
          path: "workflow.steps.normalize.input_mapping_jsonb.summary",
          source: missingSource,
          step_id: "normalize",
        },
        {
          code: "workflow.group.body_entry_missing",
          message: "The group body needs an entry",
          path: "workflow.groups.items.body.entry",
        },
      ],
      warnings: [],
    },
  } as never);
}

describe("workflow diagnostics on the step and group model", () => {
  it("names a missing source step and opens the affected step", async () => {
    installDefinition();
    renderPage();

    expect(await screen.findByText("Missing source step: deleted")).toBeInTheDocument();
    expect(screen.getByText(/The referenced step is not in this workflow/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open source step" })).not.toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Open step mapping" })[0]);

    expect(await screen.findByRole("heading", { name: "Step editor" })).toBeInTheDocument();
    // The drawer keeps only failures on screen; the opened editor names the step itself.
    expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Normalize");
  });

  it("opens the affected real group from its issue", async () => {
    installDefinition();
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Open affected group" }));

    expect(await screen.findByRole("heading", { name: "Group editor" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Items");
  });
});
