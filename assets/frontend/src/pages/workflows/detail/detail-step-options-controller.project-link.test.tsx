import type { ReactNode } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  fetchProjectWorkflowBindings,
  saveProjectWorkflowBindings,
} from "@/pages/projects/workflows/api";
import { useWorkflowStepOptionsController } from "@/pages/workflows/detail/detail-step-options-controller";

vi.mock("@/pages/projects/workflows/api", () => ({
  fetchProjectWorkflowBindings: vi.fn(),
  saveProjectWorkflowBindings: vi.fn(),
}));

const mockedFetchProjectWorkflowBindings = vi.mocked(fetchProjectWorkflowBindings);
const mockedSaveProjectWorkflowBindings = vi.mocked(saveProjectWorkflowBindings);

describe("workflow builder project linking", () => {
  beforeEach(() => {
    mockedFetchProjectWorkflowBindings.mockReset();
    mockedSaveProjectWorkflowBindings.mockReset();
  });

  it("adds the workflow to existing bindings and leaves the default to the server", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    mockedFetchProjectWorkflowBindings.mockResolvedValue({
      project_id: "7",
      workflow_ids: ["wf_a", "wf_b"],
      operator_default_workflow_id: "wf_b",
      project_workflows: [],
    });
    mockedSaveProjectWorkflowBindings.mockResolvedValue({} as never);
    const linkedProjectQuery = { refetch: vi.fn().mockResolvedValue(undefined) };
    const availableProjectsQuery = {
      data: [{ id: "7", name: "Project Seven" }],
      isLoading: false,
      refetch: vi.fn().mockResolvedValue(undefined),
    };
    function Harness() {
      const controller = useWorkflowStepOptionsController({
          availableProjectsQuery,
          drawerMode: "",
          linkedProjectQuery,
          definitionRows: { stepRows: [] },
          selectedProjectId: "",
          selectedStep: null,
          setSearchParams: vi.fn(),
          stepAgentId: "",
          stepKindDraft: "",
          stepOverrideDraft: {},
          workflow: {},
          workflowId: "wf_new",
        });
      return controller.renderProjectLinkControl();
    }
    render(<Harness />, { wrapper });

    await user.selectOptions(screen.getByLabelText("Choose a project"), "7");
    await user.click(screen.getByRole("button", { name: "Link project for Test Run" }));

    await waitFor(() =>
      expect(mockedSaveProjectWorkflowBindings).toHaveBeenCalledWith("7", {
        workflow_ids: ["wf_a", "wf_b", "wf_new"],
      }),
    );
  });
});
