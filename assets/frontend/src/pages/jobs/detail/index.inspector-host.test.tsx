import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { MemoryRouter } from "react-router";

import { jobPayload, primeApi } from "./index.test-helpers";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { DsDrawer, type DrawerGeometry } from "@/components/shared/design-system-overlays";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { DrawerResizeHandle, WorkflowDrawerShell } from "@/pages/workflows/detail/detail-drawer-shell";
import { useWorkflowBuilderDrawerResize } from "@/pages/workflows/detail/detail-builder-drawer-resize";
import { BUILDER_DRAWER_WIDTH_STORAGE_KEY } from "@/pages/workflows/detail/detail-core";
import { JobsDetailPage } from "./index";

const authValue: AuthContextValue = {
  error: null, isAuthenticated: true, status: "authenticated",
  logout: async () => undefined, refresh: async () => undefined, switchAccount: async () => undefined,
  session: {
    authenticated: true, local_auth_enabled: true, bootstrap_required: false, external_auth_enabled: false,
    roles: ["admin"], permissions: {}, account_memberships: [],
    user: { id: "inspector-user", email: "inspector@example.test", username: "inspector", display_name: "Inspector",
      roles: ["admin"], account_id: "account-test", account_ids: ["account-test"], account_memberships: [], timezone: "Europe/Vilnius" },
    i18n: { locale: "en", fallback_locale: "en", supported_locales: [], messages: {} },
    feature_flags: {}, environment: { edition: "community", mode: "development" },
    settings_pages: [], registered_pages: [], shell_navigation: {},
  },
};

function HostHarness() {
  const [open, setOpen] = useState(true);
  const [geometry, setGeometry] = useState<DrawerGeometry | null>(null);
  return <WorkflowDrawerShell ctx={{
    builderSurface: true, drawerOpen: open, drawerMode: "workflow-test-run", drawerTab: "test-run",
    drawerTitle: "Test Run results", builderDrawerStyle: { width: "450px" },
    closeDrawer: () => setOpen(false), changeDrawerTab: () => undefined,
    onDrawerGeometryChange: setGeometry,
  }}>
    <JobsDetailPage embedded jobId="42" expectedWorkflowId="wf_weekly_report" stepInspectorHost={geometry} />
  </WorkflowDrawerShell>;
}
function primeStep() {
  primeApi(jobPayload({ status: "completed", execution_steps: [{
    step_id: "shape", step_execution_id: "shape-pass", title: "Shape", status: "completed",
    inputs: { qty: 2, sku: "A-1" }, outputs: { qty: 2 }, duration_ms: 22,
  }] }));
}

describe("embedded Jobs step inspector host", () => {
  it("uses the measured contextual Results bounds through resize, retaining I/O and Close", async () => {
    primeStep();
    const user = userEvent.setup();
    let rect = new DOMRect(window.innerWidth - 450, 64, 450, window.innerHeight - 64);
    const originalRect = HTMLElement.prototype.getBoundingClientRect;
    const measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("workflow-builder-drawer") ? rect : originalRect.call(this);
    });
    const previousObserver = Object.getOwnPropertyDescriptor(globalThis, "ResizeObserver");
    const observers = new Set<() => void>();
    class Observer {
      constructor(private callback: () => void) {}
      observe() { observers.add(this.callback); }
      disconnect() { observers.delete(this.callback); }
      unobserve() {}
    }
    Object.defineProperty(globalThis, "ResizeObserver", { configurable: true, writable: true, value: Observer });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let view: ReturnType<typeof render> | undefined;
    try {
      view = render(<AuthContext.Provider value={authValue}><I18nContext.Provider value={fallbackContext}><QueryClientProvider client={queryClient}>
        <MemoryRouter><HostHarness /></MemoryRouter>
      </QueryClientProvider></I18nContext.Provider></AuthContext.Provider>);
      const step = await screen.findByRole("button", { name: /Shape/i });
      await user.click(step);
      const inspector = await screen.findByTestId("job-step-inspector-drawer");
      expect(inspector).not.toHaveAttribute("aria-modal");
      expect(inspector).toHaveStyle({ zIndex: "calc(var(--fs-z-inline-dialog) + var(--fs-z-local-raised))" });
      expect(inspector).toHaveStyle({ top: "64px", right: "0px", width: "450px", height: `${window.innerHeight - 64}px`, minWidth: "0px" });
      expect(within(inspector).getByRole("heading", { name: "Inputs" })).toBeInTheDocument();
      expect(within(inspector).getByRole("heading", { name: "Outputs" })).toBeInTheDocument();
      expect(within(inspector).getByText("A-1")).toBeInTheDocument();
      rect = new DOMRect(window.innerWidth - 537, 64, 537, window.innerHeight - 64);
      act(() => { for (const callback of observers) callback(); });
      await waitFor(() => expect(inspector).toHaveStyle({ width: "537px", top: "64px", right: "0px" }));
      expect(within(inspector).getByText("A-1")).toBeInTheDocument();
      await user.click(within(inspector).getByRole("button", { name: "Close step details" }));
      expect(screen.queryByTestId("job-step-inspector-drawer")).not.toBeInTheDocument();
      expect(screen.getByRole("dialog", { name: "Test Run results" })).toBeInTheDocument();
      await user.click(step);
      expect(await screen.findByTestId("job-step-inspector-drawer")).toHaveStyle({ width: "537px" });
      fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(observers.size).toBe(0);
    } finally {
      view?.unmount();
      queryClient.clear();
      measure.mockRestore();
      if (previousObserver) Object.defineProperty(globalThis, "ResizeObserver", previousObserver);
      else Reflect.deleteProperty(globalThis, "ResizeObserver");
    }
  });

  it("keeps standalone Jobs step details on the shared default geometry", async () => {
    primeStep();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<AuthContext.Provider value={authValue}><I18nContext.Provider value={fallbackContext}>
      <QueryClientProvider client={queryClient}><MemoryRouter><JobsDetailPage jobId="42" /></MemoryRouter></QueryClientProvider>
    </I18nContext.Provider></AuthContext.Provider>);
    try {
      await userEvent.click(await screen.findByRole("button", { name: /Shape/i }));
      const inspector = await screen.findByTestId("job-step-inspector-drawer");
      expect(inspector.style.getPropertyValue("top")).toBe("");
      expect(inspector.style.getPropertyValue("z-index")).toBe("");
      expect(inspector.style.getPropertyValue("width")).toBe("");
      expect(inspector).not.toHaveAttribute("aria-modal");
      expect(within(inspector).getByText("A-1")).toBeInTheDocument();
    } finally { view.unmount(); queryClient.clear(); }
  });
});


describe("contextual drawer motion geometry", () => {
  it("publishes the settled panel position without a resize and retires its local event listeners", async () => {
    let rect = new DOMRect(window.innerWidth, 64, 450, window.innerHeight - 64);
    const originalRect = HTMLElement.prototype.getBoundingClientRect;
    const measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("motion-host") ? rect : originalRect.call(this);
    });
    const publish = vi.fn();
    let view: ReturnType<typeof render> | undefined;
    try {
      view = render(<DsDrawer open onClose={() => undefined} title="Animated Results" className="motion-host" onGeometryChange={publish}>
        <span>Recorded I/O</span>
      </DsDrawer>);
      const panel = screen.getByRole("dialog", { name: "Animated Results" });
      await waitFor(() => expect(publish).toHaveBeenLastCalledWith({ top: 64, right: -450, width: 450, height: window.innerHeight - 64 }));
      rect = new DOMRect(window.innerWidth - 450, 64, 450, window.innerHeight - 64);
      const callsBeforeChildMotion = publish.mock.calls.length;
      fireEvent.animationEnd(screen.getByText("Recorded I/O"));
      expect(publish).toHaveBeenCalledTimes(callsBeforeChildMotion);
      fireEvent.animationEnd(panel);
      expect(publish).toHaveBeenLastCalledWith({ top: 64, right: 0, width: 450, height: window.innerHeight - 64 });
      rect = new DOMRect(window.innerWidth - 537, 64, 537, window.innerHeight - 64);
      fireEvent.transitionEnd(panel, { propertyName: "transform" });
      expect(publish).toHaveBeenLastCalledWith({ top: 64, right: 0, width: 537, height: window.innerHeight - 64 });
      view.unmount();
      expect(publish).toHaveBeenLastCalledWith(null);
      const callsAfterUnmount = publish.mock.calls.length;
      fireEvent.animationEnd(panel);
      fireEvent.transitionEnd(panel);
      expect(publish).toHaveBeenCalledTimes(callsAfterUnmount);
    } finally { view?.unmount(); measure.mockRestore(); }
  });
});


function ResizableHostHarness() {
  const resize = useWorkflowBuilderDrawerResize(true);
  const [geometry, setGeometry] = useState<DrawerGeometry | null>(null);
  return <WorkflowDrawerShell ctx={{ ...resize, builderSurface: true, drawerOpen: true,
    drawerMode: "workflow-test-run", drawerTab: "test-run", drawerTitle: "Resizable Results",
    closeDrawer: () => undefined, changeDrawerTab: () => undefined, onDrawerGeometryChange: setGeometry,
  }}>
    <JobsDetailPage embedded jobId="42" expectedWorkflowId="wf_weekly_report" stepInspectorHost={geometry}
      stepInspectorChrome={<div className="workflow-builder-drawer"><DrawerResizeHandle
        beginResize={resize.beginBuilderDrawerResize} resizable={resize.builderDrawerResizable}
        setWidth={resize.setBuilderDrawerWidth} viewportWidth={resize.builderDrawerViewportWidth} width={resize.builderDrawerWidth}
      /></div>} />
  </WorkflowDrawerShell>;
}

describe("nested inspector shared resize owner", () => {
  it("lets its own visible resize handle update the same Results owner and persisted width", async () => {
    primeStep();
    const previousWidth = localStorage.getItem(BUILDER_DRAWER_WIDTH_STORAGE_KEY);
    localStorage.setItem(BUILDER_DRAWER_WIDTH_STORAGE_KEY, "450");
    const originalRect = HTMLElement.prototype.getBoundingClientRect;
    const measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (!this.classList.contains("fs-drawer") || !this.classList.contains("workflow-builder-drawer")) return originalRect.call(this);
      const width = Number.parseFloat(this.style.width);
      return new DOMRect(window.innerWidth - width, 64, width, window.innerHeight - 64);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let view: ReturnType<typeof render> | undefined;
    try {
      view = render(<AuthContext.Provider value={authValue}><I18nContext.Provider value={fallbackContext}><QueryClientProvider client={queryClient}>
        <MemoryRouter><ResizableHostHarness /></MemoryRouter>
      </QueryClientProvider></I18nContext.Provider></AuthContext.Provider>);
      await userEvent.click(await screen.findByRole("button", { name: /Shape/i }));
      const inspector = await screen.findByTestId("job-step-inspector-drawer");
      const handle = within(inspector).getByRole("button", { name: "Resize builder inspector" });
      handle.focus();
      expect(handle).toHaveFocus();
      fireEvent.keyDown(handle, { key: "ArrowLeft" });
      const results = screen.getByRole("dialog", { name: "Resizable Results" });
      expect(results.style.getPropertyValue("width")).toBe("474px");
      expect(localStorage.getItem(BUILDER_DRAWER_WIDTH_STORAGE_KEY)).toBe("474");
      // JSDOM has no layout engine: deliver the real local owner's settled event.
      fireEvent.transitionEnd(results, { propertyName: "width" });
      await waitFor(() => expect(inspector).toHaveStyle({ width: "474px", top: "64px" }));
      expect(within(inspector).getByText("A-1")).toBeInTheDocument();
      await userEvent.click(within(inspector).getByRole("button", { name: "Close step details" }));
      expect(screen.queryByTestId("job-step-inspector-drawer")).not.toBeInTheDocument();
      expect(results.style.getPropertyValue("width")).toBe("474px");
    } finally {
      view?.unmount(); queryClient.clear(); measure.mockRestore();
      if (previousWidth === null) localStorage.removeItem(BUILDER_DRAWER_WIDTH_STORAGE_KEY);
      else localStorage.setItem(BUILDER_DRAWER_WIDTH_STORAGE_KEY, previousWidth);
    }
  });
});
