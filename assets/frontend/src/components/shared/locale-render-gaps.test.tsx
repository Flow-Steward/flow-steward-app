import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";
import { LocalizedStatusBadge } from "@/components/shared/localized-status-badge";
import { BulkActionBar, MultiCombobox } from "@/components/shared/design-system";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

function Lithuanian({ children }: Readonly<{ children: ReactNode }>) {
  const messages: Record<string, string> = {
    "react.shared.selected_count": "Pasirinkta: {count}",
    "react.shared.additional_objects_count": "ir dar {count}",
  };
  return <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, defaultText = "", params) => interpolate(messages[key] || defaultText, params) }}>{children}</I18nContext.Provider>;
}

describe("Lithuanian dynamic shared interface text", () => {
  it("localizes the live bulk-selection announcement and updates its count", () => {
    const { rerender } = render(<Lithuanian><BulkActionBar actions={[]} count={2} onAction={() => undefined} onClear={() => undefined} /></Lithuanian>);
    expect(screen.getByText("Pasirinkta: 2")).toHaveAttribute("aria-live", "polite");
    rerender(<Lithuanian><BulkActionBar actions={[]} count={7} onAction={() => undefined} onClear={() => undefined} /></Lithuanian>);
    expect(screen.getByText("Pasirinkta: 7")).toBeInTheDocument();
    expect(screen.queryByText("7 selected")).not.toBeInTheDocument();
  });

  it("localizes the multi-combobox accessible selection announcement", () => {
    render(<Lithuanian><MultiCombobox ariaLabel="Narystės" onChange={() => undefined} options={[{ value: "a", label: "A" }, { value: "b", label: "B" }]} placeholder="Pasirinkite" selected={["a", "b"]} /></Lithuanian>);
    expect(screen.getByText("Pasirinkta: 2")).toHaveAttribute("aria-live", "polite");
    expect(screen.queryByText("2 selected")).not.toBeInTheDocument();
  });

  it("localizes the confirmation remainder without altering object names", () => {
    render(<Lithuanian><ConfirmActionDialog confirmLabel="Pašalinti" consequence="Bus pašalinta." eyebrow="Patvirtinimas" objects={["Shop A", "Shop B", "Shop C", "Shop D", "Shop E", "Shop F", "Shop G"]} onCancel={() => undefined} onConfirm={() => undefined} open pending={false} pendingLabel="Šalinama" title="Pašalinti objektus?" /></Lithuanian>);
    expect(screen.getByText("Shop A")).toBeInTheDocument();
    expect(screen.getByText("ir dar 2")).toBeInTheDocument();
    expect(screen.queryByText("and 2 more")).not.toBeInTheDocument();
  });
});

it("uses canonical execution status labels across locale changes and preserves unknown values", () => {
  const executionMessages: Record<string, string> = {
    "jobs.status.in_progress": "Vykdoma",
    "jobs.status.completed": "Baigta",
  };
  const badges = <>
    <LocalizedStatusBadge tone="neutral" value="in_progress" />
    <LocalizedStatusBadge tone="green" value="completed" />
    <LocalizedStatusBadge tone="neutral" value="provider_waiting_custom" />
  </>;
  const { rerender } = render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages: executionMessages, t: (key, defaultText = "", params) => interpolate(executionMessages[key] || defaultText, params) }}>{badges}</I18nContext.Provider>);
  expect(screen.getByText("Vykdoma")).toBeInTheDocument();
  expect(screen.getByText("Baigta")).toBeInTheDocument();
  expect(screen.getByText("provider_waiting_custom")).toBeInTheDocument();
  expect(screen.queryByText("in_progress")).not.toBeInTheDocument();
  expect(screen.queryByText("completed")).not.toBeInTheDocument();

  rerender(<I18nContext.Provider value={fallbackContext}>{badges}</I18nContext.Provider>);
  expect(screen.getByText("In Progress")).toBeInTheDocument();
  expect(screen.getByText("Completed")).toBeInTheDocument();
  expect(screen.getByText("provider_waiting_custom")).toBeInTheDocument();
  expect(screen.queryByText("Vykdoma")).not.toBeInTheDocument();
  expect(screen.queryByText("Baigta")).not.toBeInTheDocument();
});
