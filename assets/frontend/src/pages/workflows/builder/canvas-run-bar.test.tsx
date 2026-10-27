import { act, fireEvent, render, screen, within } from "@testing-library/react";

import { CanvasRunBar, type CanvasRunBarProps } from "./canvas-run-bar";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

const rows = [
  { jobId: "job-latest", number: 2, status: "Finished", tone: "success" as const, meta: "14 s · 15:07:43", label: "Run #2 · Finished · 14 s · 15:07:43" },
  { jobId: "job-old", number: 1, status: "Failed", tone: "danger" as const, meta: "8.5 s · 15:07:36", label: "Run #1 · Failed · 8.5 s · 15:07:36" },
];

function props(overrides: Partial<CanvasRunBarProps> = {}): CanvasRunBarProps {
  return {
    selection: { kind: "verified", run: { jobId: "job-latest", tone: "running", title: "Test Run running", sub: "Classify request · 3 steps done · 0.6 s", runNumber: 2 } },
    history: { kind: "ready", rows, latestJobId: "job-latest" },
    onViewResults: vi.fn(),
    onSelectHistory: vi.fn(),
    onBackToLatest: vi.fn(),
    ...overrides,
  };
}

it("is a card with the tone, the title and one sub line in names; Results is an owning callback", () => {
  const value = props();
  render(<CanvasRunBar {...value} />);
  const bar = screen.getByRole("region", { name: "Test Run running" });
  expect(bar).toHaveAccessibleDescription("Classify request · 3 steps done · 0.6 s");
  expect(bar).toHaveAttribute("data-job-id", "job-latest");
  expect(within(bar).queryByText("job-latest")).not.toBeInTheDocument();
  fireEvent.click(within(bar).getByRole("button", { name: "Results" }));
  expect(value.onViewResults).toHaveBeenCalledExactlyOnceWith();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^(Stop|Approve|Reject|Run again|Back to latest)$/ })).not.toBeInTheDocument();
});

it.each([
  ["danger", "danger"],
  ["attention", "attention"],
  ["success", undefined],
] as const)("a %s run colours the card border", (tone, attribute) => {
  render(<CanvasRunBar {...props({ selection: { kind: "verified", run: { jobId: "job-latest", tone, title: "Title", sub: "" } } })} />);
  const bar = screen.getByRole("region", { name: "Title" });
  if (attribute) expect(bar).toHaveAttribute("data-tone", attribute);
  else expect(bar).not.toHaveAttribute("data-tone");
});

it.each([
  ["empty", "No Test Run selected"],
  ["loading", "Loading Test Run…"],
  ["error", "Test Run could not be verified"],
] as const)("keeps %s selection free of borrowed run content and Results actions", (kind, message) => {
  render(<CanvasRunBar {...props({ selection: { kind }, history: { kind: "ready", rows: [], latestJobId: null } })} />);
  expect(screen.getByText(message)).toBeVisible();
  expect(screen.queryByRole("button", { name: "Results" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Back to latest" })).not.toBeInTheDocument();
});

it("an earlier run names its number, offers Back to latest and no Run again", () => {
  const value = props({
    selection: { kind: "verified", run: { jobId: "job-old", tone: "danger", title: "Failed at Create shipment", sub: "Fulfilment · item 1 of 2 · needs a connection", runNumber: 1 } },
    runAgainAction: { onInvoke: vi.fn() },
    openFailedStepAction: { onInvoke: vi.fn() },
  });
  render(<CanvasRunBar {...value} />);
  const bar = screen.getByRole("region", { name: "Run #1 · Failed at Create shipment" });
  expect(within(bar).queryByRole("button", { name: "Run again" })).not.toBeInTheDocument();
  expect(within(bar).getAllByRole("button").map((button) => button.textContent)).toEqual(["Open step", "Back to latest", "Results"]);
  fireEvent.click(within(bar).getByRole("button", { name: "Back to latest" }));
  expect(value.onBackToLatest).toHaveBeenCalledExactlyOnceWith();
});

it("refuses a dangling latest pointer as an action target", () => {
  render(<CanvasRunBar {...props({ selection: { kind: "verified", run: { jobId: "job-old", tone: "danger", title: "Failed", sub: "" } },
    history: { kind: "ready", rows, latestJobId: "unwitnessed" } })} />);
  expect(screen.queryByRole("button", { name: "Back to latest" })).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Failed" })).toBeVisible();
});

it("respects parent busy state", () => {
  const value = props({ selection: { kind: "verified", run: { jobId: "job-old", tone: "danger", title: "Failed", sub: "", runNumber: 1 } } });
  render(<CanvasRunBar {...value} navigationBusy />);
  const results = screen.getByRole("button", { name: "Results" });
  const back = screen.getByRole("button", { name: "Back to latest" });
  // Busy openers stay focusable so a guard's confirmation can return focus to them.
  expect(results).toHaveAttribute("aria-disabled", "true");
  expect(back).toHaveAttribute("aria-disabled", "true");
  fireEvent.click(results);
  fireEvent.click(back);
  expect(value.onViewResults).not.toHaveBeenCalled();
  expect(value.onBackToLatest).not.toHaveBeenCalled();
});

it("does not project a rejected old navigation promise onto a newly supplied run", async () => {
  let rejectOld!: (reason: Error) => void;
  const oldNavigation = new Promise<boolean>((_resolve, reject) => { rejectOld = reject; });
  const first = props({ onViewResults: vi.fn(() => oldNavigation) });
  const view = render(<CanvasRunBar {...first} />);
  fireEvent.click(screen.getByRole("button", { name: "Results" }));
  expect(first.onViewResults).toHaveBeenCalledOnce();
  const next = props({ selection: { kind: "verified", run: { jobId: "job-latest", tone: "neutral", title: "Test Run stopped", sub: "" } }, onViewResults: vi.fn(() => Promise.resolve(false)) });
  view.rerender(<CanvasRunBar {...next} />);
  await act(async () => { rejectOld(new Error("Old owner refused")); });
  expect(screen.getByRole("region", { name: "Test Run stopped" })).toBeVisible();
  expect(screen.queryByText("Old owner refused")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("renders Stop, Follow run and Clear marks only from explicitly supplied action authority", () => {
  const stop = vi.fn();
  const clear = vi.fn();
  const follow = vi.fn();
  const view = render(<CanvasRunBar {...props()} stopAction={{ onInvoke: stop }} clearMarksAction={{ onInvoke: clear }} followRunAction={{ onInvoke: follow }} />);
  expect(screen.getAllByRole("button").map((button) => button.textContent || button.getAttribute("aria-label")))
    .toEqual(["Follow run", "Results", "Stop", "Clear marks"]);
  fireEvent.click(screen.getByRole("button", { name: "Stop" }));
  fireEvent.click(screen.getByRole("button", { name: "Clear marks" }));
  fireEvent.click(screen.getByRole("button", { name: "Follow run" }));
  expect(stop).toHaveBeenCalledOnce();
  expect(clear).toHaveBeenCalledOnce();
  expect(follow).toHaveBeenCalledOnce();
  view.rerender(<CanvasRunBar {...props()} stopAction={{ onInvoke: stop, busy: true }} clearMarksAction={{ onInvoke: clear, disabled: true }} />);
  expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Clear marks" })).toBeDisabled();
  view.rerender(<CanvasRunBar {...props({ selection: { kind: "error" } })} stopAction={{ onInvoke: stop }} clearMarksAction={{ onInvoke: clear }} />);
  expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Clear marks" })).not.toBeInTheDocument();
});

it("shows a supplied action notice", () => {
  render(<CanvasRunBar {...props()} actionNotice={{ kind: "error", message: "Cancel refused by server" }} />);
  expect(screen.getByRole("alert")).toHaveTextContent("Cancel refused by server");
});

it("runs again only through the supplied action and keeps the action disabled while starting", () => {
  const onInvoke = vi.fn();
  const finished = { selection: { kind: "verified" as const, run: { jobId: "job-latest", tone: "success" as const, title: "Test Run finished in 14 s", sub: "20 steps done" } } };
  const view = render(<CanvasRunBar {...props(finished)} runAgainAction={{ onInvoke }} />);
  expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["Results", "Run again"]);
  fireEvent.click(screen.getByRole("button", { name: "Run again" }));
  expect(onInvoke).toHaveBeenCalledOnce();
  view.rerender(<CanvasRunBar {...props(finished)} runAgainAction={{ onInvoke, busy: true }} />);
  expect(screen.getByRole("button", { name: "Run again" })).toBeDisabled();
});

it("uses contextual Review request and Open step callbacks without decision controls", () => {
  const review = vi.fn();
  const open = vi.fn();
  const view = render(<CanvasRunBar {...props()} reviewRequestAction={{ onInvoke: review }} openFailedStepAction={{ onInvoke: open }} />);
  fireEvent.click(screen.getByRole("button", { name: "Review request" }));
  fireEvent.click(screen.getByRole("button", { name: "Open step" }));
  expect(review).toHaveBeenCalledOnce();
  expect(open).toHaveBeenCalledOnce();
  expect(screen.queryByRole("button", { name: /^(Approve|Reject)$/ })).not.toBeInTheDocument();
  view.rerender(<CanvasRunBar {...props()} reviewRequestAction={{ onInvoke: review, disabled: true }} openFailedStepAction={{ onInvoke: open, busy: true }} />);
  expect(screen.getByRole("button", { name: "Review request" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Open step" })).toBeDisabled();
});

it("localizes its actions", () => {
  const messages: Record<string, string> = { "react.workflows.run_bar_results": "Rezultatai", "react.workflows.run_bar_back_to_latest": "Grįžti į naujausią" };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages,
    t: (key, defaultText = "", params) => interpolate(messages[key] || defaultText, params) }}>
    <CanvasRunBar {...props({ selection: { kind: "verified", run: { jobId: "job-old", tone: "danger", title: "Failed", sub: "", runNumber: 1 } } })} />
  </I18nContext.Provider>);
  expect(screen.getByRole("button", { name: "Rezultatai" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Grįžti į naujausią" })).toBeVisible();
});
