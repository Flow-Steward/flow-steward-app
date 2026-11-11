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
