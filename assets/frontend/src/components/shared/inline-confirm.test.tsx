import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { BulkActionBar, DsTable } from "@/components/shared/design-system";
import { InlineConfirm } from "@/components/shared/inline-confirm";

describe("InlineConfirm", () => {
  it("puts No first with focus, and names the confirming action in its variant", () => {
    render(
      <InlineConfirm
        confirmLabel="Restore skill"
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        pendingLabel="Restoring…"
        question="Restore skill “Review”?"
        tone="primary"
      />,
    );
    const group = screen.getByRole("group", { name: "Restore skill “Review”?" });
    const [no, confirm] = within(group).getAllByRole("button");
    expect(no).toHaveTextContent("No");
    expect(no).toHaveFocus();
    expect(confirm).toHaveTextContent("Restore skill");
    expect(confirm).toHaveClass("primary");
  });

  it("cancels on Escape unless the request is in flight", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const { rerender } = render(
      <InlineConfirm confirmLabel="Purge tool" onCancel={onCancel} onConfirm={vi.fn()} pendingLabel="Purging…" question="Purge tool?" tone="danger" />,
    );
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);

    rerender(
      <InlineConfirm confirmLabel="Purge tool" onCancel={onCancel} onConfirm={vi.fn()} pending pendingLabel="Purging…" question="Purge tool?" tone="danger" />,
    );
    expect(screen.getByRole("button", { name: "Purging…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "No" })).toBeDisabled();
    screen.getByRole("group").focus();
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("shows the reason beside restored actions after a failure", () => {
    render(
      <InlineConfirm confirmLabel="Purge tool" error="Blocked by a reference." onCancel={vi.fn()} onConfirm={vi.fn()} pendingLabel="Purging…" question="Purge tool?" tone="danger" />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Blocked by a reference.");
    expect(screen.getByRole("button", { name: "Purge tool" })).toBeEnabled();
  });
});

describe("DsTable row replacement", () => {
  it("replaces one row in place with a strip spanning every column", () => {
    const rows = [{ id: "a", name: "Alpha" }, { id: "b", name: "Beta" }];
    render(
      <DsTable
        ariaLabel="Things"
        columns={[
          { key: "name", header: "Name", cell: (row: (typeof rows)[number]) => row.name },
          { key: "id", header: "ID", cell: (row: (typeof rows)[number]) => row.id },
        ]}
        rowKey={(row) => row.id}
        rowReplacement={(row) => (row.id === "b" ? <span>Confirm Beta</span> : null)}
        rows={rows}
        selectable
        selectedKeys={new Set()}
      />,
    );
    const table = screen.getByRole("table", { name: "Things" });
    expect(within(table).getByText("Alpha")).toBeInTheDocument();
    expect(within(table).queryByText("Beta")).not.toBeInTheDocument();
    // Two columns plus the selection column.
    expect(within(table).getByRole("cell", { name: "Confirm Beta" })).toHaveAttribute("colspan", "3");
  });
});

describe("BulkActionBar confirmation", () => {
  function Harness({ onClear }: Readonly<{ onClear: () => void }>) {
    const [confirming, setConfirming] = useState(false);
    return (
      <BulkActionBar
        actions={[{ id: "restore", label: "Restore Selected" }]}
        confirmation={
          confirming ? (
            <InlineConfirm
              confirmLabel="Restore 3 items"
              onCancel={() => setConfirming(false)}
              onConfirm={vi.fn()}
              pendingLabel="Restoring…"
              question="Restore 3 items?"
              tone="primary"
            />
          ) : undefined
        }
        count={3}
        onAction={() => setConfirming(true)}
        onClear={onClear}
      />
    );
  }

  it("swaps the buttons for the question, keeps the count, and gives Escape to the question", async () => {
    const user = userEvent.setup();
    const onClear = vi.fn();
    render(<Harness onClear={onClear} />);
    const bar = screen.getByRole("region", { name: "Bulk actions" });

    await user.click(within(bar).getByRole("button", { name: "Restore Selected" }));
    expect(within(bar).getByText("3 selected")).toBeInTheDocument();
    expect(within(bar).queryByRole("button", { name: "Restore Selected" })).not.toBeInTheDocument();
    expect(within(bar).queryByRole("button", { name: "Clear selection" })).not.toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "No" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(onClear).not.toHaveBeenCalled();
    expect(within(bar).getByRole("button", { name: "Restore Selected" })).toBeInTheDocument();

    within(bar).getByRole("button", { name: "Restore Selected" }).focus();
    await user.keyboard("{Escape}");
    expect(onClear).toHaveBeenCalledTimes(1);
  });
});
