import { fireEvent, render, screen, within } from "@testing-library/react";

import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Pagination } from "@/components/shared/design-system-table";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { MonitoringStatusStrip } from "@/pages/monitoring/monitoring-panels";

const messages: Record<string, string> = {
  "common.cancel": "Atšaukti",
  "common.confirm": "Patvirtinti",
  "react.monitoring.system_status": "Sistemos būsena",
  "react.monitoring.ready": "Paruošta:",
  "react.monitoring.no": "ne",
};

function LocalizedViews({ locale }: Readonly<{ locale: "en" | "lt" }>) {
  const localizedMessages = locale === "lt" ? messages : {};
  return (
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages: localizedMessages, t: (key, fallback = "") => localizedMessages[key] || fallback || key }}>
      <ConfirmDialog title="Operator-owned title" open body="Operator-owned content" destructive={false} onCancel={() => undefined} onConfirm={() => undefined} />
    </I18nContext.Provider>
  );
}

describe("platform locale rendering", () => {
  it("localizes the whole pagination range while preserving page navigation and English rendering", () => {
    const paginationMessages: Record<string, string> = {
      "react.shared.pagination_range": "{start}–{end} iš {total}",
      "react.shared.next_page": "Kitas puslapis",
    };
    let selectedPage = 0;
    const pagination = <Pagination onPageChange={(page) => { selectedPage = page; }} page={2} perPage={10} total={23} />;
    const { rerender } = render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages: paginationMessages, t: (key, fallback = "", params) => interpolate(paginationMessages[key] || fallback || key, params) }}>
        {pagination}
      </I18nContext.Provider>,
    );
    expect(screen.getByText("11–20 iš 23")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Kitas puslapis" }));
    expect(selectedPage).toBe(3);

    rerender(<I18nContext.Provider value={fallbackContext}>{pagination}</I18nContext.Provider>);
    expect(screen.getByText("11–20 of 23")).toBeInTheDocument();
  });

  it("translates readiness values as presentation while preserving runtime identifiers", () => {
    render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, fallback = "") => messages[key] || fallback || key }}>
        <MonitoringStatusStrip components={null} meta={{ version: "release-2026.10", extension_runtime_target: "python3.13" }} ready={false} summaryUnavailable={false} totals={{ checks_failed: 0, checks_ok: 2, checks_total: 2 }} />
      </I18nContext.Provider>,
    );

    expect(within(screen.getByRole("group", { name: "Sistemos būsena" })).getByText("Paruošta: ne")).toBeInTheDocument();
    expect(screen.getByText("release-2026.10")).toBeInTheDocument();
    expect(screen.getByText("python3.13")).toBeInTheDocument();
  });

  it("translates shared actions without changing operator content", () => {
    render(<LocalizedViews locale="lt" />);

    const dialog = screen.getByRole("dialog", { name: "Operator-owned title" });
    expect(within(dialog).getByText("Operator-owned content")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Atšaukti" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Patvirtinti" })).toBeInTheDocument();
  });

  it("updates existing action labels when the locale changes", () => {
    const { rerender } = render(<LocalizedViews locale="en" />);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();

    rerender(<LocalizedViews locale="lt" />);
    expect(screen.getByRole("button", { name: "Atšaukti" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });
});
