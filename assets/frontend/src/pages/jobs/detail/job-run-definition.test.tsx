import { screen, waitFor, within } from "@testing-library/react";

import {
  jobPayload,
  mockedFetchJobDefinition,
  primeApi,
  renderPage,
} from "./index.test-helpers";

describe("JobsDetailPage executed definition", () => {
  it("names the pinned revision and links the current workflow explicitly", async () => {
    primeApi(jobPayload({ status: "completed" }));
    renderPage();

    const facts = await screen.findByTestId("job-run-definition-revision");

    expect(facts).toHaveTextContent("Executed definition: revision 3 · sha256-revis");
    expect(within(facts).getByTestId("job-current-workflow-link")).toHaveAttribute(
      "href",
      "/registry/workflows/wf_demo",
    );
  });

  it("says the snapshot is unavailable instead of showing the current workflow as executed", async () => {
    primeApi(jobPayload({ status: "completed" }));
    mockedFetchJobDefinition.mockResolvedValue({
      job_id: "42",
      available: false,
      unavailable_reason: "definition_snapshot_unavailable",
      steps: [],
      groups: [],
    } as never);
    renderPage();

    const notice = await screen.findByTestId("job-run-definition-unavailable");
    expect(notice).toHaveTextContent("Definition snapshot unavailable");
    expect(notice).toHaveTextContent("Only its surviving logs are shown");
    expect(screen.queryByTestId("job-run-definition-revision")).not.toBeInTheDocument();
  });

  it("shows when a Wait step resumes and which child a parent waits for", async () => {
    primeApi(
      jobPayload({
        status: "awaiting_timer",
        runtime: {
          workflow_wait: { kind: "timer", wait_id: "w-1", step_id: "pause", due_at: "2026-10-13T08:00:00Z" },
          active_steps: [],
        },
      }),
    );
    const view = renderPage();
    expect(await screen.findByTestId("job-wait-due-at")).toHaveTextContent("Waiting until");
    view.unmount();

    primeApi(
      jobPayload({
        status: "awaiting_child",
        runtime: { workflow_wait: { kind: "child", wait_id: "w-2", child_job_id: "77" }, active_steps: [] },
      }),
    );
    renderPage();
    expect(await screen.findByTestId("job-wait-child-link")).toHaveAttribute("href", "/jobs/77");
  });

  it("names the For each and the item a paused step is at", async () => {
    primeApi(
      jobPayload({
        status: "awaiting_child",
        runtime: {
          workflow_wait: { kind: "child", wait_id: "w-3", child_job_id: "78" },
          active_steps: [
            {
              step_id: "check_customer",
              step_execution_id: "e-1",
              for_each_id: "each_order",
              status: "awaiting_child",
              for_each_progress: { for_each_id: "each_order", item_index: 2, selected_count: 4, truncated: false },
            },
          ],
        },
      }),
    );
    mockedFetchJobDefinition.mockResolvedValue({
      job_id: "42",
      available: true,
      revision: 3,
      digest: "sha256-revision",
      steps: [],
      groups: [],
      for_each: [{ for_each_id: "each_order", display_name: "Each paid order" }],
    } as never);
    renderPage();

    // The executed definition's name, not the internal id.
    expect(await screen.findByTestId("job-for-each-progress")).toHaveTextContent("For each Each paid order: item 3 of 4");
  });

  it("lists the active lanes of a parallel group", async () => {
    primeApi(
      jobPayload({
        status: "in_progress",
        runtime: {
          active_steps: [
            { step_id: "fetch", step_execution_id: "e1", lane_id: "a", group_id: "par", status: "in_progress" },
            { step_id: "enrich", step_execution_id: "e2", lane_id: "b", group_id: "par", status: "pending" },
          ],
        },
      }),
    );
    renderPage();

    const lanes = await screen.findByTestId("job-active-lanes");
    expect(lanes).toHaveTextContent("Lane a: fetch");
    expect(lanes).toHaveTextContent("Lane b: enrich");
  });
});

it("emits the actual runtime group and pinned revision for the embedding builder", async () => {
  primeApi(jobPayload({ status: "in_progress", current_group_id: "archived-group", execution_steps: [{ id: "attempt-1", step_id: "archived-step", group_id: "archived-group", status: "in_progress" }] }));
  const onRuntimeLocationChange = vi.fn();
  renderPage({ onRuntimeLocationChange });
  await waitFor(() => expect(onRuntimeLocationChange).toHaveBeenLastCalledWith({
    stepId: "archived-step", groupId: "archived-group", definitionWorkflowId: "wf_demo", definitionRevision: 3,
  }));
});
