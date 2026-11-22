import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";
import styles from "@/components/shared/design-system.css?raw";

/** The declarations of the first rule whose selector list is exactly *selector*. */
function rule(css: string, selector: string, from = 0): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s*");
  const match = new RegExp(`(?:^|[}\\n])\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(css.slice(from));
  expect(match, `${selector} must be declared`).not.toBeNull();
  return match ? match[1] : "";
}

function mediaBlock(css: string, query: string): string {
  const start = css.indexOf(`@media (${query})`);
  expect(start, `@media (${query}) must exist`).toBeGreaterThanOrEqual(0);
  return css.slice(start);
}

const LONG_LABEL = "Delete configuration and connections";

describe("confirmation dialog width", () => {
  it("grows to fit its actions instead of cutting a long label off", () => {
    const confirm = rule(styles, ".fs-dialog.confirm");
    expect(confirm).toContain("width: fit-content");
    expect(confirm).toContain("min-width: min(var(--fs-confirmation-dialog-width), var(--fs-modal-max-width))");
    expect(confirm).toContain("max-width: var(--fs-modal-max-width)");
    // Only the footer sets the width: the text above wraps to it.
    expect(rule(styles, ".fs-dialog.confirm > .fs-dialog-header,\n.fs-dialog.confirm > .fs-dialog-body")).toContain(
      "width: 0; min-width: 100%",
    );
    expect(rule(styles, ".fs-dialog.confirm > .fs-dialog-footer .fs-button")).toContain("white-space: nowrap");
  });

  it("keeps the narrow-screen rules: a narrow ceiling, full-width actions that stack", () => {
    const narrow = mediaBlock(styles, "width <= 720px");
    const confirm = rule(narrow, ".fs-dialog.confirm");
    expect(confirm).toContain("width: fit-content");
    expect(confirm).toContain("max-width: var(--fs-modal-max-width-narrow)");
    const phone = mediaBlock(styles, "width <= 430px");
    expect(rule(phone, ".fs-dialog-footer .fs-button")).toContain("flex: 1");
    expect(rule(phone, ".fs-dialog.confirm > .fs-dialog-footer")).toContain("flex-wrap: wrap");
  });

  it("renders the whole confirm label in the footer the width rules size", () => {
    render(
      <ConfirmActionDialog
        confirmLabel={LONG_LABEL}
        consequence="Every connection that signs in with this configuration is deleted too."
        eyebrow="OAuth configuration"
        objects={["Primary", "Backup"]}
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        open
        pending={false}
        pendingLabel="Deleting"
        title="Delete this configuration?"
      />,
    );

    const button = screen.getByRole("button", { name: LONG_LABEL });
    expect(button).toHaveTextContent(LONG_LABEL);
    // eslint-disable-next-line testing-library/no-node-access -- the width rule is a layout fact of the footer inside the confirm dialog, which has no role of its own.
    const footer = button.closest(".fs-dialog-footer");
    // eslint-disable-next-line testing-library/no-node-access -- see above: the dialog box that must grow is the footer's parent.
    expect(footer?.parentElement).toHaveClass("fs-dialog", "confirm");
  });
});
