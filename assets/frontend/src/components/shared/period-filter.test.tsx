import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PeriodFilter } from "@/components/shared/period-filter";
import type { PeriodValue } from "@/lib/time/period";

function Harness({ onChange = () => undefined }: Readonly<{ onChange?: (value: PeriodValue) => void }>) {
  const [value, setValue] = useState<PeriodValue>({ kind: "all" });
  return (
    <PeriodFilter
      ariaLabel="Event period"
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
      value={value}
    />
  );
}

async function openFilter(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /^Event period:/ }));
  return screen.getByRole("dialog", { name: "Event period" });
}

describe("PeriodFilter", () => {
  it("applies a preset on click, closes, and names it on the trigger", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const trigger = screen.getByRole("button", { name: "Event period: All events" });

    const surface = await openFilter(user);
    const group = within(surface).getByRole("radiogroup", { name: "Event period" });
    expect(within(group).getByRole("radio", { name: "All events" })).toBeChecked();
    expect(within(group).getByRole("radio", { name: "All events" })).toHaveFocus();

    await user.click(within(group).getByRole("radio", { name: "Last 7 days" }));
    expect(onChange).toHaveBeenCalledWith({ kind: "preset", preset: "last7" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveAccessibleName("Event period: Last 7 days");
    expect(trigger).toHaveFocus();

    await openFilter(user);
    expect(screen.getByRole("radio", { name: "Last 7 days" })).toHaveFocus();
  });

  it("moves between options with the arrow keys", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await openFilter(user);
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("radio", { name: "Today" })).toHaveFocus();
    await user.keyboard("{ArrowUp}{ArrowUp}");
    expect(screen.getByRole("radio", { name: "Custom range" })).toHaveFocus();
  });

  it("opens the custom range inside the same popover and needs Apply", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const surface = await openFilter(user);

    await user.click(within(surface).getByRole("radio", { name: "Custom range" }));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    const first = within(surface).getByLabelText("Custom range, first day");
    expect(first).toHaveFocus();
    fireEvent.change(first, { target: { value: "2026-06-03" } });
    expect(onChange).not.toHaveBeenCalled();

    // Apply stays enabled and says why it did nothing.
    await user.click(within(surface).getByRole("button", { name: "Apply" }));
    expect(within(surface).getByRole("alert")).toHaveTextContent("Choose both the first and the last day.");
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.change(within(surface).getByLabelText("Custom range, last day"), { target: { value: "2026-06-17" } });
    await user.click(within(surface).getByRole("button", { name: "Apply" }));
    expect(onChange).toHaveBeenCalledWith({ kind: "custom", from: "2026-06-03", to: "2026-06-17" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("reports an inverted range on both bounds", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const surface = await openFilter(user);
    await user.click(within(surface).getByRole("radio", { name: "Custom range" }));
    const first = within(surface).getByLabelText("Custom range, first day");
    const last = within(surface).getByLabelText("Custom range, last day");
    fireEvent.change(first, { target: { value: "2026-06-17" } });
    fireEvent.change(last, { target: { value: "2026-06-03" } });
    await user.click(within(surface).getByRole("button", { name: "Apply" }));

    expect(within(surface).getByRole("alert")).toHaveTextContent("The end of the window may not fall before its start.");
    expect(first).toHaveAttribute("aria-invalid", "true");
    expect(last).toHaveAttribute("aria-invalid", "true");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("leaves the value alone on Cancel", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const surface = await openFilter(user);
    await user.click(within(surface).getByRole("radio", { name: "Custom range" }));
    await user.click(within(surface).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Event period: All events" })).toHaveFocus();
  });
});
