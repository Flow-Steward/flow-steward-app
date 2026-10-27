import { render, screen } from "@testing-library/react";

import { createWorkflowVariableSuggestionRenderers } from "@/pages/workflows/detail/detail-variable-suggestions";

/**
 * A Template input offered step artifacts and project settings in its source
 * list, and saving one was then refused ("must be a workflow variable"). A
 * field can now limit the list to what its step kind accepts.
 */
describe("workflow variable suggestions limited to what a field accepts", () => {
  const structure = {
    globalGroup: { key: "global", label: "Global", options: [{ value: "project_config.region", label: "region" }] },
    workflowGroup: { key: "workflow", label: "Workflow", options: [{ value: "state.outputs.header", label: "header" }] },
    steps: [
      {
        stepId: "start",
        aliasOptions: [{ value: "state.outputs.body", label: "body", stepId: "start" }],
        artifactOptions: [
          { value: "message.step.start.artifacts.outputs", label: "outputs", stepId: "start" },
        ],
      },
    ],
  };

  function renderField(accept?: (path: string) => boolean, expandedWorkflowSuggestionKeys: string[] = []) {
    const { renderWorkflowVariableInput } = createWorkflowVariableSuggestionRenderers({
      activeWorkflowSuggestionPanelId: "source-panel",
      availableWorkflowVariableSuggestionCount: 4,
      expandedWorkflowSuggestionKeys,
      setActiveWorkflowSuggestionPanelId: vi.fn(),
      setExpandedWorkflowSuggestionKeys: vi.fn(),
      workflowSuggestionStructure: structure,
      workflowSuggestionTyped: true,
    });
    render(
      <>
        {renderWorkflowVariableInput({
          accept,
          ariaLabel: "Input 1 source",
          className: "",
          onChange: vi.fn(),
          panelId: "source-panel",
          placeholder: "state.outputs.text",
          // Typed text ranks every variable whose path contains it.
          value: "o",
        })}
      </>,
    );
    return screen.getAllByRole("option").map((option) => option.textContent || "");
  }

  it("offers ordinary variables while Advanced is closed", () => {
    const offered = renderField().join(" ");
    expect(offered).toContain("state.outputs.header");
    expect(offered).toContain("message.step.start.artifacts.outputs");
    expect(offered).not.toContain("project_config.region");
  });

  it("offers global variables after Advanced is explicitly opened", () => {
    const offered = renderField(undefined, ["group:global"]).join(" ");
    expect(offered).toContain("message.step.start.artifacts.outputs");
    expect(offered).toContain("project_config.region");
  });

  it("offers only workflow variables to a field that reads state even with Advanced open", () => {
    const offered = renderField((path) => path.startsWith("state."), ["group:global"]).join(" ");
    expect(offered).toContain("state.outputs.header");
    expect(offered).toContain("state.outputs.body");
    expect(offered).not.toContain("message.step.start.artifacts.outputs");
    expect(offered).not.toContain("project_config.region");
  });
});
