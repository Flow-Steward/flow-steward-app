import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import type { WorkflowStep } from "@/pages/workflows/shared/api";
import {
  canvasGraphFixture,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  postedChangeSets,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

describe("canvas diagnostic repair navigation", () => {
  it("opens the exact affected step and saves its IF repair as one config change", async () => {
    let revision = 3;
    let step: WorkflowStep = {
      step_id: "check_city",
      display_name: "Check city",
      step_kind: "if",
      override: { if: { condition: true } },
    };
    let repaired = false;
    mockedFetchWorkflowDetail.mockImplementation(async () =>
      structuredClone(
        workflowDetailFixture({
          enabled: false,
          revision,
          steps: [
            { step_id: "prepare", display_name: "Prepare city", step_kind: "set_variable", override: { set_variable: { assignments: { "state.prepared": true } } } },
            step,
          ],
          edges: [
            { source: { kind: "step", id: "prepare" }, outcome: { name: "success" }, target: { kind: "step", id: "check_city" } },
          ],
        }),
      ),
    );
    mockedFetchWorkflowCanvasGraph.mockImplementation(async () =>
      canvasGraphFixture({
        enabled: false,
        entry: { kind: "step", id: "prepare" },
        steps: [
          { id: "prepare", step_id: "prepare", display_name: "Prepare city", kind: "set_variable", config_summary: "state.prepared = true", ports: [{ outcome: { name: "success" }, label: "Next" }] },
          { id: "check_city", step_id: "check_city", display_name: "Check city", kind: "if", config_summary: "Constant condition", ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }] },
        ],
        edges: [
          { source: { kind: "step", id: "prepare" }, outcome: { name: "success" }, target: { kind: "step", id: "check_city" } },
        ],
        validation_issues: repaired
          ? []
          : [{
              scope: "step", scope_id: "check_city", authored_id: "check_city", step_id: "check_city",
              severity: "warning", code: "workflow.if.condition_not_object", title: "Normalize IF condition",
              message: "The constant condition can use its supported operator.",
              field_path: "workflow.steps.check_city.override.if.condition",
            }],
      }),
    );
    const defaultFetch = fetchSpy.getMockImplementation();
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input) === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { operations: Array<{ patch: Partial<WorkflowStep> }> };
        step = { ...step, ...body.operations[0].patch };
        repaired = true;
        revision += 1;
        return new Response(JSON.stringify({ workflow_id: "weekly_report", revision, valid: true, issues: [], workflow: {} }), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      return defaultFetch ? defaultFetch(input, init) : new Response("{}");
    });

    const view = renderPage({ surface: "builder" });
    fireEvent.click(await screen.findByTitle("Open canvas validation issues"));
    fireEvent.click(await screen.findByRole("button", { name: /Normalize IF condition/ }));
    expect(await screen.findByRole("heading", { name: "Step editor" })).toBeInTheDocument();
    const drawer = screen.getByRole("dialog", { name: "Step editor" });
    expect(within(drawer).getByDisplayValue("Check city")).toBeInTheDocument();
    expect(within(drawer).getByText("Unsupported raw IF condition")).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "Apply fix" }));

    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0]).toEqual({
      expected_revision: 3,
      operations: [
        {
          op: "update_step_config",
          step_id: "check_city",
          patch: expect.objectContaining({
            display_name: "Check city",
            override: { if: { condition: { operator: "always_true" } } },
          }),
        },
      ],
    });
    // A config save carries no structure: no id, kind or membership.
    const patch = (postedChangeSets()[0].operations as Array<{ patch: Record<string, unknown> }>)[0].patch;
    expect(Object.keys(patch)).not.toEqual(expect.arrayContaining(["step_id"]));
    expect(patch).not.toHaveProperty("step_kind");
    expect(patch).not.toHaveProperty("group_id");
    await waitFor(() => expect(within(drawer).queryByText("Unsupported raw IF condition")).not.toBeInTheDocument());

    view.unmount();
    renderPage({ initialEntry: "/registry/workflows/weekly_report/builder?drawer=step&step_id=check_city" });
    expect(await screen.findByRole("heading", { name: "Step editor" })).toBeInTheDocument();
    const operator = screen.getByRole("combobox", { name: "Operator" });
    expect(operator).toHaveTextContent("always true");
    fireEvent.click(operator);
    expect(within(screen.getByRole("listbox", { name: "Operator options" }))
      .getByRole("option", { name: "always true" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(operator, { key: "Escape" });
    expect(screen.queryByText("Unsupported raw IF condition")).not.toBeInTheDocument();
    expect(postedChangeSets()).toHaveLength(1);
  }, 15_000);
});
