import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { WorkflowHeaderMutationDialogs } from "@/pages/workflows/shared/header-mutation-dialogs";
import type { WorkflowHeaderMutations } from "@/pages/workflows/shared/header-actions";

function controllerWith(
  overrides: Partial<WorkflowHeaderMutations> = {},
): WorkflowHeaderMutations {
  return {
    cloneProjectId: "",
    cloneSource: null,
    cloneWorkflow: vi.fn(),
    closeClone: vi.fn(),
    finishClone: vi.fn(),
    deleteOpen: false,
    deleteWorkflow: vi.fn(),
    closeDelete: vi.fn(),
    performDelete: vi.fn().mockResolvedValue(undefined),
    deleting: false,
    error: "",
    dismissError: vi.fn(),
    toggleEnabled: vi.fn().mockResolvedValue(undefined),
    togglingEnabled: false,
    ...overrides,
  };
}

function renderDialogs(controller: WorkflowHeaderMutations) {
  return render(
    <MemoryRouter>
      <WorkflowHeaderMutationDialogs controller={controller} workflowId="wf_demo" />
    </MemoryRouter>,
  );
}

describe("WorkflowHeaderMutationDialogs", () => {
  it("renders nothing while both dialogs are closed and there is no error", () => {
    renderDialogs(controllerWith());

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reports a failed mutation on the page instead of in a browser alert", () => {
    renderDialogs(controllerWith({ error: "Workflow is protected." }));

    // The published danger alert carries role=alert; `globalThis.alert` carried none.
    expect(within(screen.getByRole("alert")).getByText("Workflow is protected.")).toBeInTheDocument();
  });

  it("holds the delete until DELETE is typed", () => {
    const performDelete = vi.fn().mockResolvedValue(undefined);
    renderDialogs(controllerWith({ deleteOpen: true, performDelete }));

    const dialog = screen.getByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Delete workflow" });
    expect(confirm).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText("Type DELETE to remove this workflow."), {
      target: { value: "delete please" },
    });
    expect(confirm).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText("Type DELETE to remove this workflow."), {
      target: { value: "delete" },
    });
    // Case is not the safety property here; typing the word is.
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    expect(performDelete).toHaveBeenCalledTimes(1);
  });

  it("clears the typed confirmation on the way out", () => {
    const closeDelete = vi.fn();
    renderDialogs(controllerWith({ deleteOpen: true, closeDelete }));

    const dialog = screen.getByRole("alertdialog");
    fireEvent.change(within(dialog).getByLabelText("Type DELETE to remove this workflow."), {
      target: { value: "DELETE" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(closeDelete).toHaveBeenCalledTimes(1);
    // Reopened, the dialog asks again rather than arriving pre-confirmed.
    expect(
      within(screen.getByRole("alertdialog")).getByLabelText(
        "Type DELETE to remove this workflow.",
      ),
    ).toHaveValue("");
  });

  it("opens the clone dialog for the source the controller names", () => {
    renderDialogs(controllerWith({ cloneSource: "wf_demo" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Clone workflow" })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("New workflow ID")).toHaveValue("wf_demo_copy");
  });
});
