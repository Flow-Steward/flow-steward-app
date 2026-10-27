import { fireEvent, render, screen } from "@testing-library/react";

import { AppShellSearchTrigger } from "@/components/layout/app-shell-search-trigger";

describe("AppShellSearchTrigger", () => {
  it("renders one compact trigger and opens search", () => {
    const onOpenSearch = vi.fn();

    render(<AppShellSearchTrigger onOpenSearch={onOpenSearch} />);

    const trigger = screen.getByRole("button", { name: "Global search" });
    expect(trigger).toHaveAttribute("aria-haspopup", "dialog");
    expect(trigger).toHaveTextContent("Search");
    expect(trigger).toHaveTextContent("⌘K");

    fireEvent.click(trigger);

    expect(onOpenSearch).toHaveBeenCalledOnce();
  });

  it("describes the trigger from hover and focus", () => {
    render(<AppShellSearchTrigger onOpenSearch={vi.fn()} />);

    const trigger = screen.getByRole("button", { name: "Global search" });
    fireEvent.mouseEnter(trigger);
    expect(trigger).toHaveAttribute(
      "aria-describedby",
      screen.getByRole("tooltip").id,
    );
    fireEvent.mouseLeave(trigger);
    fireEvent.focus(trigger);
    expect(trigger).toHaveAttribute(
      "aria-describedby",
      screen.getByRole("tooltip").id,
    );
    fireEvent.blur(trigger);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});
