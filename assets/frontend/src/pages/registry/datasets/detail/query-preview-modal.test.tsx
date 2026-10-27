import { render, screen } from "@testing-library/react";

import { QueryPreviewModal } from "./query-preview-modal";

describe("QueryPreviewModal", () => {
  it("keeps wide result columns scrollable and collapses the output schema", () => {
    render(
      <QueryPreviewModal
        onClose={vi.fn()}
        previewState={{
          queryName: "Events by type",
          outputSchema: { type: "object", properties: { event: { type: "string" } } },
          rowCount: 1,
          rows: [
            {
              event: "purchase",
              eventStart: "2026-07-15T05:00:00Z",
              extraLongColumn: "A value that must remain readable without squeezing every other column.",
            },
          ],
          truncated: false,
        }}
      />,
    );

    expect(screen.getByTestId("query-preview-table-scroll")).toHaveClass("overflow-x-auto");
    expect(screen.getByRole("table")).toHaveClass("min-w-max");
    expect(screen.getByTestId("query-preview-output-schema")).not.toHaveAttribute("open");
  });
});
