import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { OperationalOverview, type OperationalOverviewProps } from "@/pages/dashboard/operational-overview";

const messages: Record<string, string> = {
  "react.dashboard.oldest_failing_age": "Seniausia nepavykusi: {age}",
  "react.dashboard.none_right_now": "Šiuo metu nėra",
  "react.dashboard.oldest_waiting_age": "Ilgiausiai laukianti: {age}",
  "react.dashboard.success_rate": "Sėkminga: {rate}",
};
const props: OperationalOverviewProps = { range: "24h", onRangeChange: () => undefined, needsAttention: 1, inProgress: 0, failedOrStuck: 1, awaitingApproval: 1, waiting: 0, completed: 4, queued: 0, longestRunningMs: null, oldestFailingMs: 60_000, oldestWaitingMs: 120_000, successRate: "80%", successRateDeltaPp: null, generatedAt: "" };

function overview(overrides: Partial<OperationalOverviewProps> = {}) {
  return <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><MemoryRouter><OperationalOverview {...props} {...overrides} /></MemoryRouter></I18nContext.Provider>;
}

describe("Lithuanian dashboard metric context", () => {
  it("localizes dynamic failure waiting and success context without changing metric values", () => {
    render(overview());
    expect(screen.getByText("Seniausia nepavykusi: 1m")).toBeInTheDocument();
    expect(screen.getByText("Ilgiausiai laukianti: 2m")).toBeInTheDocument();
    expect(screen.getByText("Sėkminga: 80%")).toBeInTheDocument();
    expect(screen.queryByText(/Oldest failing|Oldest waiting|80% success/)).not.toBeInTheDocument();
  });

  it("localizes the current empty queue state independently of historical range", () => {
    render(overview({ needsAttention: 0, failedOrStuck: 0, oldestFailingMs: null, range: "30d" }));
    expect(screen.getByText("Šiuo metu nėra")).toBeInTheDocument();
    expect(screen.queryByText("None right now")).not.toBeInTheDocument();
  });
});
