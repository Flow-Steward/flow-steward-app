import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { ModelSpend, QuickLinks } from "@/pages/dashboard/dashboard-panels";

describe("dashboard model spend", () => {
  it("renders unavailable and partial spend without inventing zero-dollar values", () => {
    render(
      <ModelSpend
        breakdown={{
          agent: [],
          project: [
            {
              availability: "unavailable",
              name: "Unknown project",
              spend_usd: null,
            },
            {
              availability: "partial",
              name: "Partial project",
              spend_usd: 1.25,
            },
          ],
          workflow: [],
        }}
        rangeLabel="Last 24 hours"
        total="Partial"
      />,
    );

    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/\$1\.25 \(partial\)/)).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("hides registry and administration destinations without matching capabilities", () => {
    const { rerender } = render(
      <MemoryRouter>
        <QuickLinks canAdministerPlatform={false} canReadRegistry={false} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: /create a project/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /build a workflow/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /review guardrails/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /system monitoring/i })).not.toBeInTheDocument();

    rerender(
      <MemoryRouter>
        <QuickLinks canAdministerPlatform canReadRegistry />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: /build a workflow/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /review guardrails/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /system monitoring/i })).toBeInTheDocument();
  });
});
