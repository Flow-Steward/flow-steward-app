/* eslint-disable testing-library/no-node-access -- a large table is counted by its rows. */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { utils, write, type WorkSheet } from "xlsx";

import SpreadsheetPreview from "@/components/file-preview/spreadsheet-preview";

/** A real workbook, written by the same library the preview reads it with. */
function workbookBytes(bookType: "xlsx" | "biff8"): ArrayBuffer {
  const totals: WorkSheet = utils.aoa_to_sheet([
    ["Item", "Amount", "Date"],
    ["Paper", 12.5, 0],
  ]);
  // A saved formula result: the preview shows 42, it never calculates 1+1.
  totals.B2 = { t: "n", v: 42, f: "1+1" };
  totals.C2 = { t: "n", v: 45678, z: "m/d/yy" };
  const rows = Array.from({ length: 250 }, (_, row) => Array.from({ length: 60 }, (_, column) => `r${row + 1}c${column + 1}`));
  const book = utils.book_new();
  utils.book_append_sheet(book, totals, "Totals");
  utils.book_append_sheet(book, utils.aoa_to_sheet(rows), "Long list");
  return write(book, { type: "array", bookType }) as ArrayBuffer;
}

describe("spreadsheet preview", () => {
  it.each(["xlsx", "biff8"] as const)("shows %s sheets as tabs with saved values and a note for what is cut", async (bookType) => {
    render(<SpreadsheetPreview data={workbookBytes(bookType)} name="totals" />);

    const tabs = screen.getByRole("tablist", { name: "Sheets of totals" });
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Totals", "Long list"]);
    const totals = screen.getByRole("table", { name: "Totals" });
    expect(within(totals).getByRole("cell", { name: "42" })).toBeInTheDocument();
    expect(within(totals).getByRole("cell", { name: "2025-01-21" })).toBeInTheDocument();
    expect(screen.queryByText(/Showing/)).not.toBeInTheDocument();

    fireEvent.click(within(tabs).getByRole("tab", { name: "Long list" }));

    await waitFor(() => expect(screen.getByText("Showing 200 of 250 rows. Showing 50 of 60 columns.")).toBeInTheDocument());
    // A 200 by 50 table: read by text, as role queries over ten thousand cells are slow.
    const list = screen.getByText("r200c50").closest("table") as HTMLTableElement;
    expect(list).toHaveAttribute("aria-label", "Long list");
    expect(list.querySelectorAll("tbody tr")).toHaveLength(200);
    expect(within(list).queryByText("r201c1")).not.toBeInTheDocument();
    expect(within(list).queryByText("r1c51")).not.toBeInTheDocument();
  });

  it("keeps CSV values as written and counts every row", async () => {
    const lines = ["code;name", "007;Ąžuolas <b>bold</b>", ...Array.from({ length: 300 }, (_, row) => `${row};row ${row}`)];
    const data = new TextEncoder().encode(`${lines.join("\n")}\n`).buffer as ArrayBuffer;

    render(<SpreadsheetPreview data={data} name="rows.csv" />);

    await waitFor(() => expect(screen.getByText("Showing 200 of 302 rows.")).toBeInTheDocument());
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.getByText("007").tagName).toBe("TD");
    // Markup in a cell is text.
    expect(screen.getByText("Ąžuolas <b>bold</b>").tagName).toBe("TD");
  });

  it("says a file it cannot read needs its download", () => {
    render(<SpreadsheetPreview data={new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1, 2, 3]).buffer} name="broken.xlsx" />);

    expect(screen.getByRole("alert")).toHaveTextContent("could not be read");
  });
});
