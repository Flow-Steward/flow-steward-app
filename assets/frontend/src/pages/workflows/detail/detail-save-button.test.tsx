import { act, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { SAVE_DONE_VISIBLE_MS, SaveStateButton } from "./detail-save-button";

const labels = {
  label: "Save parameters",
  savingLabel: "Saving parameters…",
  doneLabel: "Saved",
  doneAnnouncement: "Parameters saved",
};

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

it("rests while there is nothing to save and goes live on a change", () => {
  const { rerender } = render(<SaveStateButton {...labels} dirty={false} onClick={() => undefined} saving={false} />);
  expect(screen.getByRole("button", { name: "Save parameters" })).toBeDisabled();
  rerender(<SaveStateButton {...labels} dirty onClick={() => undefined} saving={false} />);
  expect(screen.getByRole("button", { name: "Save parameters" })).toBeEnabled();
});

it("says Saving … while the request runs, then ✓ Saved for a moment, then rests", () => {
  const { rerender } = render(<SaveStateButton {...labels} dirty onClick={() => undefined} saving={false} />);
  rerender(<SaveStateButton {...labels} dirty onClick={() => undefined} saving />);
  const busy = screen.getByRole("button", { name: "Saving parameters…" });
  expect(busy).toHaveAttribute("aria-busy", "true");
  expect(busy).toBeDisabled();
  rerender(<SaveStateButton {...labels} dirty={false} onClick={() => undefined} saving={false} />);
  expect(screen.getByRole("button", { name: "Saved" })).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("Parameters saved");
  act(() => { vi.advanceTimersByTime(SAVE_DONE_VISIBLE_MS); });
  expect(screen.getByRole("button", { name: "Save parameters" })).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("");
});

it("shows no ✓ when the save failed", () => {
  const { rerender } = render(<SaveStateButton {...labels} dirty onClick={() => undefined} saving />);
  rerender(<SaveStateButton {...labels} dirty failed onClick={() => undefined} saving={false} />);
  expect(screen.getByRole("button", { name: "Save parameters" })).toBeEnabled();
});

it("says ✓ Updated when the owner signals a save kept with the unapplied structure", () => {
  const { rerender } = render(<SaveStateButton {...labels} doneLabel="Updated" dirty doneSignal={0} onClick={() => undefined} saving={false} />);
  rerender(<SaveStateButton {...labels} doneLabel="Updated" dirty={false} doneSignal={1} onClick={() => undefined} saving={false} />);
  expect(screen.getByRole("button", { name: "Updated" })).toBeInTheDocument();
});

it("keeps one width: every label is rendered, only the current one is named", () => {
  render(<SaveStateButton {...labels} dirty onClick={() => undefined} saving={false} />);
  const button = screen.getByRole("button", { name: "Save parameters" });
  expect(within(button).getByText("Saving parameters…")).toHaveAttribute("aria-hidden", "true");
  expect(within(button).getByText("Saved")).toHaveAttribute("aria-hidden", "true");
});
