import { fireEvent, render, screen } from "@testing-library/react";
import { Search } from "lucide-react";

import { IconButton } from "@/components/ui/icon-button";

describe("IconButton", () => {
  it("provides the canonical medium control and a transient accessible tooltip", () => {
    render(<IconButton icon={Search} label="Search" />);
    const button = screen.getByRole("button", { name: "Search" });
    expect(button).toHaveAttribute("data-size", "medium");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    fireEvent.mouseEnter(button);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Search");

    fireEvent.mouseLeave(button);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    fireEvent.focus(button);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Search");
  });

  it("passes button state and events without changing the label", () => {
    const onClick = vi.fn();
    render(
      <IconButton disabled icon={Search} label="Search" onClick={onClick} />,
    );
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  });

  it("supports the canonical small control used by the Header trigger", () => {
    render(<IconButton icon={Search} label="Search" size="small" />);
    const button = screen.getByRole("button", { name: "Search" });
    expect(button).toHaveAttribute("data-size", "small");
  });

  it("can preserve the accessible label without rendering a tooltip", () => {
    render(<IconButton icon={Search} label="Search" showTooltip={false} />);
    const button = screen.getByRole("button", { name: "Search" });

    fireEvent.mouseEnter(button);
    fireEvent.focus(button);

    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(button).not.toHaveAttribute("aria-describedby");
  });
});
