import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { expect, it } from "vitest";

import { ContractBlock, type ContractBlockRow } from "./detail-contract-block";

function Harness({ initial = [] as Array<{ name: string; invalid?: boolean }> }) {
  const [rows, setRows] = useState(initial);
  const blockRows: ContractBlockRow[] = rows.map((row, index) => ({
    key: `row-${index}`,
    name: row.name || "Unnamed output",
    unnamed: !row.name,
    required: index === 0,
    type: "string",
    summary: "Receipt",
    invalid: row.invalid,
    content: <input aria-label={`Name ${index}`} onChange={(event) => setRows((current) => current.map((item, i) => i === index ? { name: event.target.value } : item))} value={row.name} />,
  }));
  return <ContractBlock addLabel="Add output" countText={`${rows.length} outputs`} emptyText="No expected outputs." onAdd={() => setRows((current) => [...current, { name: "" }])} rows={blockRows} title="Expected outputs" />;
}

it("shows the empty state and opens the row Add creates", () => {
  render(<Harness />);
  const block = screen.getByRole("region", { name: "Expected outputs" });
  expect(within(block).getByText("No expected outputs.")).toBeInTheDocument();
  fireEvent.click(within(block).getByRole("button", { name: "Add output" }));
  expect(within(block).getByText("1 outputs")).toBeInTheDocument();
  expect(within(block).getByRole("button", { name: /Unnamed output/ })).toHaveAttribute("aria-expanded", "true");
  expect(within(block).getByLabelText("Name 0")).toBeInTheDocument();
});

it("folds rows to one summary line and opens one at a time", () => {
  render(<Harness initial={[{ name: "receipt" }, { name: "total" }]} />);
  expect(screen.queryByLabelText("Name 0")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^receipt/ }));
  expect(screen.getByLabelText("Name 0")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^total/ }));
  expect(screen.queryByLabelText("Name 0")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Name 1")).toBeInTheDocument();
});

it("keeps a rejected row open while its value is fixed", () => {
  render(<Harness initial={[{ name: "", invalid: true }]} />);
  const field = screen.getByLabelText("Name 0");
  fireEvent.change(field, { target: { value: "receipt" } });
  expect(screen.getByLabelText("Name 0")).toHaveValue("receipt");
});

it("reads every row open for someone who may not edit the contract, with nothing to add", async () => {
  const { ContractReadOnly } = await import("./detail-contract-block");
  render(<ContractReadOnly value><Harness initial={[{ name: "receipt" }, { name: "total" }]} /></ContractReadOnly>);
  expect(screen.getByLabelText("Name 0")).toBeInTheDocument();
  expect(screen.getByLabelText("Name 1")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Add output" })).not.toBeInTheDocument();
});
