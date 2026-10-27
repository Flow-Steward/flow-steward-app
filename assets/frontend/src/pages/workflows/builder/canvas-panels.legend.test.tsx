import { render, screen } from "@testing-library/react";

import { LegendPanel } from "@/pages/workflows/builder/canvas-panels";

it("names what Add step offers today: For each in Flow, Group, Parallel lanes and Hook in Groups, no rework path (R3-8)", () => {
  render(<LegendPanel />);

  expect(screen.getByRole("heading", { name: "Flow" })).toBeInTheDocument();
  expect(screen.getByText("For each")).toBeInTheDocument();
  expect(screen.getByText("Parallel lanes")).toBeInTheDocument();
  expect(screen.getByText("Hook")).toBeInTheDocument();
  expect(screen.getByText(/A place where a project adds its own steps\./)).toBeInTheDocument();
  // Groups is a family of Add step; the explanation below it has its own heading.
  expect(screen.getAllByRole("heading", { name: "Groups" })).toHaveLength(1);
  expect(screen.getByRole("heading", { name: "Inside groups" })).toBeInTheDocument();
  expect(screen.queryByText(/loop group/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/rework/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/route_to_step/)).not.toBeInTheDocument();
});
