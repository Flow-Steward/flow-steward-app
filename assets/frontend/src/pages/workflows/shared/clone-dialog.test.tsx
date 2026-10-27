import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { fetchJson } from "@/lib/http/client";
import { WorkflowCloneDialog } from "@/pages/workflows/shared/clone-dialog";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const mockedFetchJson = vi.mocked(fetchJson);

describe("WorkflowCloneDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchJson.mockResolvedValue({ workflow_id: "wf_copy" });
  });

  it("clones into the active project context", async () => {
    const onCloned = vi.fn();
    render(
      <WorkflowCloneDialog
        onClose={() => {}}
        onCloned={onCloned}
        projectId="project-alpha"
        sourceWorkflowId="wf_source"
      />,
    );

    fireEvent.change(screen.getByLabelText("New workflow ID"), {
      target: { value: "wf_copy" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Clone workflow" }));

    await waitFor(() =>
      expect(mockedFetchJson).toHaveBeenCalledWith(
        "/api/workflows/wf_source/clone",
        {
          body: JSON.stringify({
            project_id: "project-alpha",
            workflow_id: "wf_copy",
          }),
          method: "POST",
        },
      ),
    );
    expect(onCloned).toHaveBeenCalledWith("wf_copy");
  });
});
