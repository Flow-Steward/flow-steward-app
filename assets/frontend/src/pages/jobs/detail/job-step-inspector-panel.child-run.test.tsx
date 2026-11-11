import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { StepInspectorPanel } from "./job-step-inspector-panel";

function renderPanel(step: Record<string, unknown>) {
  render(
    <MemoryRouter>
      <StepInspectorPanel selectedStep={{ title: "Notify and check", status: "completed", ...step }} testId="inspector" />
    </MemoryRouter>,
  );
}

it("links a step that ran another workflow, or an extension action behind one, to that run", () => {
  renderPanel({ child_job_id: "job child/1" });

  expect(screen.getByRole("link", { name: "Open the run behind this step" })).toHaveAttribute("href", "/jobs/job%20child%2F1");
});

it("shows no run link for a step that started none", () => {
  renderPanel({});

  expect(screen.queryByRole("link", { name: "Open the run behind this step" })).not.toBeInTheDocument();
});

it("names the extension a step called, and keeps Tool for MCP and system tools", () => {
  renderPanel({ step_type_label: "Extension action", tool_kind: "extension", tool_label: "Example Showcase", operation_label: "Test External Request" });

  expect(screen.getByText("Extension", { selector: "dt" })).toBeInTheDocument();
  expect(screen.getByText("Example Showcase")).toBeInTheDocument();
  expect(screen.getByText("Extension action")).toBeInTheDocument();
  expect(screen.queryByText("Tool", { selector: "dt" })).not.toBeInTheDocument();
});

it("keeps the Tool field for an MCP tool", () => {
  renderPanel({ step_type_label: "MCP tool", tool_kind: "mcp", tool_label: "connector.mcp.sheets" });

  expect(screen.getByText("Tool", { selector: "dt" })).toBeInTheDocument();
  expect(screen.queryByText("Extension", { selector: "dt" })).not.toBeInTheDocument();
});
