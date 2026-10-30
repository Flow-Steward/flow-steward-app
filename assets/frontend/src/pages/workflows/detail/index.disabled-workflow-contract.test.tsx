/**
 * The builder for a disabled workflow.
 *
 * The contract route reused the run-gated projection, so a disabled draft
 * answered HTTP 500 twice per page load. The backend fix is what stops the 500;
 * what this pins on the client is the property that made it visible and would
 * make any future failure of that request visible again: the page stays usable
 * and the author can still reach their steps and groups.
 */
import { screen } from "@testing-library/react";

import {
  fetchSpy,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowValidation,
  mockedFindWorkflowLinkedProjectId,
  renderPage,
} from "./index.test-helpers";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

function disabledWorkflow() {
  return {
    workflow_id: "draft_workflow",
    display_name: "Draft in progress",
    enabled: false,
    steps: [{
            id: "11111111-1111-1111-1111-1111111110bb",
            step_id: "draft_step",
            display_name: "Draft step",
            step_kind: "agent",
            agent_id: "reviewer",
            override: {},
          }], groups: [],
  };
}

function mockStack(contractResponse: () => Response) {
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  fetchSpy.mockImplementation(async (input) => {
    const url = stringFromUnknownValue(input);
    if (url.includes("/contract")) {
      return contractResponse();
    }
    return new Response(JSON.stringify({ sections: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  mockedFetchWorkflowDetail.mockResolvedValue(disabledWorkflow() as never);
  mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
  mockedFetchWorkflowValidation.mockResolvedValue({
    validation: { valid: true, errors: [], warnings: [] },
  } as never);
}

describe("the builder for a disabled workflow", () => {
  it("loads and stays usable when the contract is served", async () => {
    mockStack(
      () =>
        new Response(
          JSON.stringify({ workflow_id: "draft_workflow", interface: {} }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
    );

    renderPage();

    expect(await screen.findByRole("button", { name: /Draft step/i })).toBeInTheDocument();
  });

  it("stays usable if the contract request fails", async () => {
    // A failing contract must degrade the contract-derived surfaces, not the
    // page: the author still has to be able to reach their steps.
    mockStack(
      () =>
        new Response(JSON.stringify({ detail: "boom" }), {
          status: 500,
          headers: { "content-type": "application/json" },
        }),
    );

    renderPage();

    expect(await screen.findByRole("button", { name: /Draft step/i })).toBeInTheDocument();
  });
});
