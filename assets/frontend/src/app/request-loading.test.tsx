import { act, render, screen, within } from "@testing-library/react";

import { App } from "@/app/App";
import { DsDialog } from "@/components/shared/design-system-overlays";
import { focusAfterRequestLoading } from "@/components/shared/request-loading-focus";
import { fetchJson } from "@/lib/http/client";
import { hasPendingRequests } from "@/lib/http/request-activity";

vi.mock("@/app/router", () => ({ AppRouter: () => null }));

const settlePending: Array<() => void> = [];
const requests: Array<Promise<unknown>> = [];

function observe<T>(request: Promise<T>) {
  const outcome = request.then(
    (value) => ({ ok: true as const, value }),
    (error: unknown) => ({ ok: false as const, error }),
  );
  requests.push(outcome);
  return outcome;
}

function pendingBody() {
  let finished = false;
  let finish = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      finish = () => {
        if (finished) return;
        finished = true;
        controller.enqueue(new TextEncoder().encode('{"ready":true}'));
        controller.close();
      };
    },
  });
  settlePending.push(() => finish());
  return {
    response: new Response(body, { headers: { "content-type": "application/json" } }),
    finish: () => finish(),
  };
}

function pendingFetch() {
  let resolve!: (response: Response) => void;
  let reject!: (error: unknown) => void;
  const response = new Promise<Response>((resolveResponse, rejectResponse) => {
    resolve = resolveResponse;
    reject = rejectResponse;
  });
  settlePending.push(() => resolve(new Response("{}")));
  return { response, resolve, reject };
}

describe("application request loading", () => {
  it("releases foreground protection while an overlapping background read is still pending", async () => {
    const foreground = pendingBody();
    const background = pendingFetch();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(foreground.response).mockReturnValueOnce(background.response);
    render(<><label>Draft<input aria-label="Draft" defaultValue="Unsaved value" /></label><App /></>);
    const field = screen.getByRole("textbox", { name: "Draft" });
    field.focus();
    let foregroundOutcome!: ReturnType<typeof observe>;
    let backgroundOutcome!: ReturnType<typeof observe>;
    let backgroundSettled = false;
    await act(async () => { foregroundOutcome = observe(fetchJson("/api/foreground")); });
    const modal = screen.getByRole("dialog", { name: "Loading requests" });
    expect(modal).toHaveFocus();
    await act(async () => {
      backgroundOutcome = observe(fetchJson("/api/background", {}, { presentation: "background" }));
      void backgroundOutcome.then(() => { backgroundSettled = true; });
    });
    act(() => { vi.advanceTimersByTime(400); });
    expect(screen.getByRole("dialog", { name: "Loading requests" })).toBe(modal);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(hasPendingRequests()).toBe(true); expect(backgroundSettled).toBe(false);
    await act(async () => { foreground.finish(); await foregroundOutcome; });
    expect(backgroundSettled).toBe(false);
    expect(hasPendingRequests()).toBe(false);
    expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Draft" })).toBe(field);
    expect(field).toHaveFocus(); expect(field).toHaveValue("Unsaved value");
    await act(async () => {
      background.resolve(new Response(JSON.stringify({ ready: true }), { headers: { "content-type": "application/json" } }));
      await expect(backgroundOutcome).resolves.toEqual({ ok: true, value: { ready: true } });
    });
    expect(backgroundSettled).toBe(true);
    expect(hasPendingRequests()).toBe(false);
    expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(field).toHaveFocus(); expect(field).toHaveValue("Unsaved value");
  });

  it.each([undefined, "foreground"] as const)("keeps background reads usable and preserves an overlapping %s modal until its own body finishes", async (presentation) => {
    const background = pendingFetch();
    const foreground = pendingBody();
    vi.spyOn(globalThis, "fetch").mockReturnValueOnce(background.response).mockResolvedValueOnce(foreground.response);
    render(<><label>Draft<input aria-label="Draft" defaultValue="Unsaved value" /></label><App /></>);
    const field = screen.getByRole("textbox", { name: "Draft" });
    field.focus();
    let backgroundOutcome!: ReturnType<typeof observe>;
    let foregroundOutcome!: ReturnType<typeof observe>;
    await act(async () => { backgroundOutcome = observe(fetchJson("/api/background", {}, { presentation: "background" })); });
    act(() => { vi.advanceTimersByTime(30_000); });
    expect(hasPendingRequests()).toBe(false);
    expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
    expect(field).toHaveFocus();
    expect(field).toHaveValue("Unsaved value");
    await act(async () => { foregroundOutcome = observe(fetchJson("/api/foreground", {}, presentation ? { presentation } : undefined)); });
    expect(hasPendingRequests()).toBe(true);
    const modal = screen.getByRole("dialog", { name: "Loading requests" });
    expect(modal).toHaveFocus();
    await act(async () => { background.resolve(new Response(JSON.stringify({ error: "Temporary status failure" }), { status: 503, headers: { "content-type": "application/json" } })); await backgroundOutcome; });
    expect(screen.getByRole("dialog", { name: "Loading requests" })).toBe(modal);
    expect(hasPendingRequests()).toBe(true);
    act(() => { vi.advanceTimersByTime(400); });
    expect(screen.getByRole("status")).toBeInTheDocument();
    await act(async () => { foreground.finish(); await foregroundOutcome; });
    expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
    expect(hasPendingRequests()).toBe(false);
    expect(field).toHaveFocus();
    expect(field).toHaveValue("Unsaved value");
  });

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(async () => {
    await act(async () => {
      for (const settle of settlePending.splice(0)) settle();
      await Promise.all(requests.splice(0));
    });
    vi.restoreAllMocks();
    vi.useRealTimers();
    document.body.style.overflow = "";
  });

  it("blocks immediately with a modal, reveals loading after the delay and prevents dismissal", async () => {
    const pending = pendingFetch();
    vi.spyOn(globalThis, "fetch").mockReturnValue(pending.response);
    render(<><button type="button">Submit form</button><App /></>);
    const opener = screen.getByRole("button", { name: "Submit form" });
    opener.focus();
    document.body.style.overflow = "scroll";
    let outcome!: ReturnType<typeof observe>;
    await act(async () => { outcome = observe(fetchJson("/api/blocking")); });
    expect(globalThis.fetch).toHaveBeenCalledOnce();
    expect(hasPendingRequests()).toBe(true);

    const dialog = screen.getByRole("dialog", { name: "Loading requests" });
    expect(dialog).toHaveAttribute("open");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-busy", "true");
    expect(dialog).toHaveFocus();
    expect(document.body).toHaveProperty(["style", "overflow"], "scroll");
    expect(document.body).toHaveStyle({overflow:"hidden"});
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    const cancel = new Event("cancel", { cancelable: true });
    act(() => { dialog.dispatchEvent(cancel); });
    expect(cancel.defaultPrevented).toBe(true);
    expect(dialog).toHaveAttribute("open");

    act(() => { vi.advanceTimersByTime(400); });
    expect(screen.getByRole("status")).toBeInTheDocument();
    await act(async () => { pending.resolve(new Response("{}")); await outcome; });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.body).toHaveStyle({overflow:"scroll"});
    expect(opener).toHaveFocus();
  });

  it("releases the modal and scroll lock if the application unmounts while a request is pending", async () => {
    const pending = pendingFetch();
    vi.spyOn(globalThis, "fetch").mockReturnValue(pending.response);
    const view = render(<App />);
    await act(async () => { void observe(fetchJson("/api/unmount")); });
    expect(screen.getByRole("dialog")).toHaveAttribute("open");
    expect(document.body).toHaveStyle({overflow:"hidden"});
    view.unmount();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.body).toHaveStyle({overflow:""});
  });

  it("returns focus to a dialog opened during loading without overwriting its scroll lock", async () => {
    const pending = pendingFetch();
    vi.spyOn(globalThis, "fetch").mockReturnValue(pending.response);
    const content = (open: boolean) => (
      <>
        <button type="button">Open plan</button>
        <App />
        <DsDialog open={open} onClose={() => {}} title="Action plan" description="Plan details" eyebrow="Plan">
          <input aria-label="Plan name" />
        </DsDialog>
      </>
    );
    const view = render(content(false));
    screen.getByRole("button", { name: "Open plan" }).focus();
    document.body.style.overflow = "scroll";
    let outcome!: ReturnType<typeof observe>;
    await act(async () => { outcome = observe(fetchJson("/api/plan")); });
    view.rerender(content(true));
    await act(async () => { pending.resolve(new Response("{}")); await outcome; });
    const plan = screen.getByRole("dialog", { name: "Action plan" });
    expect(within(plan).getByRole("button", { name: "Close Action plan" })).toHaveFocus();
    expect(document.body).toHaveStyle({overflow:"hidden"});
    view.rerender(content(false));
    await Promise.resolve();
    expect(screen.getByRole("button", { name: "Open plan" })).toHaveFocus();
    expect(document.body).toHaveStyle({overflow:"scroll"});
  });

  it("keeps the delayed indicator until every overlapping response body finishes", async () => {
    const first = pendingBody();
    const second = pendingBody();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(first.response)
      .mockResolvedValueOnce(second.response);
    render(<App />);

    await act(async () => {
      void observe(fetchJson("/api/first"));
      void observe(fetchJson("/api/second"));
    });
    act(() => { vi.advanceTimersByTime(400); });
    expect(screen.getByRole("status")).toBeInTheDocument();

    await act(async () => { first.finish(); await requests[0]; });
    expect(screen.getByRole("status")).toBeInTheDocument();

    await act(async () => { second.finish(); await requests[1]; });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it.each(["HTTP", "envelope", "network", "abort", "body"] as const)(
    "removes the indicator when a request ends with an %s failure",
    async (failure) => {
      const pending = pendingFetch();
      vi.spyOn(globalThis, "fetch").mockReturnValue(pending.response);
      render(<App />);
      let outcome!: ReturnType<typeof observe>;
      const controller = new AbortController();
      await act(async () => {
        outcome = observe(fetchJson("/api/failure", { signal: controller.signal }));
      });
      act(() => { vi.advanceTimersByTime(400); });
      expect(screen.getByRole("status")).toBeInTheDocument();

      await act(async () => {
        if (failure === "HTTP") {
          pending.resolve(new Response('{"error":"Unavailable"}', {
            status: 503, headers: { "content-type": "application/json" },
          }));
        } else if (failure === "envelope") {
          pending.resolve(new Response(JSON.stringify({
            ok: false, kind: "detail", data: null,
            error: { code: "forbidden", message: "Denied", status: 403 }, meta: {},
          }), { headers: { "content-type": "application/json" } }));
        } else if (failure === "body") {
          pending.resolve(new Response("invalid json", {
            headers: { "content-type": "application/json" },
          }));
        } else if (failure === "abort") {
          controller.abort();
          pending.reject(new DOMException("Aborted", "AbortError"));
        } else {
          pending.reject(new TypeError("Network unavailable"));
        }
        await outcome;
      });
      expect(await outcome).toEqual(expect.objectContaining({ ok: false }));
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(document.body).toHaveStyle({overflow:""});
    },
  );

  it("never flashes for a request completed before the delay", async () => {
    const pending = pendingFetch();
    vi.spyOn(globalThis, "fetch").mockReturnValue(pending.response);
    render(<App />);
    let outcome!: ReturnType<typeof observe>;
    await act(async () => { outcome = observe(fetchJson("/api/fast")); });
    act(() => { vi.advanceTimersByTime(399); });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await act(async () => { pending.resolve(new Response("{}")); await outcome; });
    act(() => { vi.advanceTimersByTime(400); });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("starts a fresh delay for a later request episode", async () => {
    const first = pendingBody();
    const second = pendingBody();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(first.response)
      .mockResolvedValueOnce(second.response);
    render(<App />);
    await act(async () => { void observe(fetchJson("/api/earlier")); });
    act(() => { vi.advanceTimersByTime(400); });
    expect(screen.getByRole("status")).toBeInTheDocument();
    await act(async () => { first.finish(); await requests[0]; });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    await act(async () => { void observe(fetchJson("/api/later")); });
    act(() => { vi.advanceTimersByTime(399); });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.getByRole("status")).toBeInTheDocument();
    await act(async () => { second.finish(); await requests[1]; });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("delivers queued focus after overlapping request bodies settle without a loading indicator", async () => {
    const first = pendingBody();
    const second = pendingBody();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(first.response).mockResolvedValueOnce(second.response);
    render(<><button type="button">Return to Results</button><button type="button">Current control</button></>);
    const opener = screen.getByRole("button", { name: "Return to Results" });
    const current = screen.getByRole("button", { name: "Current control" });
    current.focus();
    const onFocused = vi.fn();
    await act(async () => {
      void observe(fetchJson("/api/no-indicator-first"));
      void observe(fetchJson("/api/no-indicator-second"));
    });
    const cancel = focusAfterRequestLoading(opener, onFocused);
    try {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(current).toHaveFocus();
      await act(async () => { first.finish(); await requests[0]; });
      expect(current).toHaveFocus();
      expect(onFocused).not.toHaveBeenCalled();
      await act(async () => { second.finish(); await requests[1]; });
      expect(opener).toHaveFocus();
      expect(onFocused).toHaveBeenCalledOnce();
    } finally { cancel(); }
  });

  it("keeps a surviving modal ahead of default queued focus without a loading indicator", async () => {
    const pending = pendingFetch();
    vi.spyOn(globalThis, "fetch").mockReturnValue(pending.response);
    const content = (open: boolean) => <>
      <button type="button">Outside return target</button>
      <DsDialog open={open} onClose={() => {}} title="Retained confirmation" description="Still requires a response" eyebrow="Confirm">
        <input aria-label="Confirmation detail" />
      </DsDialog>
    </>;
    const view = render(content(false));
    const outside = screen.getByRole("button", { name: "Outside return target" });
    outside.focus();
    let outcome!: ReturnType<typeof observe>;
    await act(async () => { outcome = observe(fetchJson("/api/no-indicator-modal")); });
    const onFocused = vi.fn();
    const cancel = focusAfterRequestLoading(outside, onFocused);
    let cancelInside = () => {};
    try {
      view.rerender(content(true));
      await act(async () => { await Promise.resolve(); });
      const close = within(screen.getByRole("dialog", { name: "Retained confirmation" }))
        .getByRole("button", { name: "Close Retained confirmation" });
      expect(close).toHaveFocus();
      const inside = within(screen.getByRole("dialog", { name: "Retained confirmation" }))
        .getByRole("textbox", { name: "Confirmation detail" });
      const insideFocused = vi.fn();
      cancelInside = focusAfterRequestLoading(inside, insideFocused);
      await act(async () => { pending.resolve(new Response("{}")); await outcome; });
      expect(inside).toHaveFocus();
      expect(insideFocused).toHaveBeenCalledOnce();
      expect(onFocused).not.toHaveBeenCalled();
    } finally { cancel(); cancelInside(); }
  });

  it("cancels queued no-indicator focus and permits a later request episode to deliver only its own target", async () => {
    const first = pendingFetch();
    const second = pendingFetch();
    vi.spyOn(globalThis, "fetch").mockReturnValueOnce(first.response).mockReturnValueOnce(second.response);
    render(<><button type="button">Retired return target</button><button type="button">New return target</button></>);
    const retired = screen.getByRole("button", { name: "Retired return target" });
    const current = screen.getByRole("button", { name: "New return target" });
    current.focus();
    const retiredFocused = vi.fn();
    let firstOutcome!: ReturnType<typeof observe>;
    await act(async () => { firstOutcome = observe(fetchJson("/api/no-indicator-retired")); });
    const cancelRetired = focusAfterRequestLoading(retired, retiredFocused);
    cancelRetired();
    await act(async () => { first.resolve(new Response("{}")); await firstOutcome; });
    expect(current).toHaveFocus();
    expect(retiredFocused).not.toHaveBeenCalled();

    retired.focus();
    let secondOutcome!: ReturnType<typeof observe>;
    await act(async () => { secondOutcome = observe(fetchJson("/api/no-indicator-current")); });
    const currentFocused = vi.fn();
    const cancelCurrent = focusAfterRequestLoading(current, currentFocused);
    try {
      expect(retired).toHaveFocus();
      await act(async () => { second.resolve(new Response("{}")); await secondOutcome; });
      expect(current).toHaveFocus();
      expect(currentFocused).toHaveBeenCalledOnce();
      expect(retiredFocused).not.toHaveBeenCalled();
    } finally { cancelCurrent(); }
  });
});
