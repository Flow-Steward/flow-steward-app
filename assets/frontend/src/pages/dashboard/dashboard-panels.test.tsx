import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { QuickLinks } from "@/pages/dashboard/dashboard-panels";

describe("dashboard quick links", () => {
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
