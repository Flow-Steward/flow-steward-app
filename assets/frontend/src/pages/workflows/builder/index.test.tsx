import userEvent from "@testing-library/user-event";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactFlowInstance } from "@xyflow/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider, Route, Routes } from "react-router";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";

import {
  fetchWorkflowCanvasGraph,
  fetchWorkflowContract,
  fetchWorkflowDag,
  fetchWorkflowDetail,
  fetchWorkflowExportPolicy,
  fetchWorkflowValidation,
  type WorkflowCanvasGraph,
  type WorkflowCanvasPort,
} from "@/pages/workflows/shared/api";
import { WorkflowsBuilderPage } from "@/pages/workflows/builder";
import { buildCanvasLayout } from "@/pages/workflows/builder/layout";
import type * as CanvasModule from "@/pages/workflows/builder/canvas";

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

vi.mock("@/pages/workflows/shared/api", () => {
  return {
    fetchWorkflowCanvasGraph: vi.fn(),
    fetchWorkflowContract: vi.fn(),
    fetchWorkflowDag: vi.fn(),
    fetchWorkflowDetail: vi.fn(),
    fetchWorkflowExportPolicy: vi.fn(),
    fetchWorkflowStepContractPreview: vi.fn(async () => ({ outputs: [] })),
    fetchWorkflowValidation: vi.fn(),
  };
});
vi.mock("@/lib/auth/use-is-admin", () => ({
  useIsAdmin: () => true,
}));
// Record what the page asks the canvas to focus; the real canvas still renders.
const canvasFlowObserver = vi.hoisted(() => ({ current: null as ReactFlowInstance | null }));
const canvasRenderedGraphs = vi.hoisted(() => [] as WorkflowCanvasGraph[]);
const canvasFocusRequests = vi.hoisted(() => [] as Array<{ id: string; nonce: number; place: boolean }>);
vi.mock("@/pages/workflows/builder/canvas", async (importOriginal) => {
  const actual = await importOriginal<typeof CanvasModule>();
  const Actual = actual.WorkflowCanvasBuilderSurface;
  return {
    ...actual,
    WorkflowCanvasBuilderSurface: (props: Parameters<typeof Actual>[0]) => {
      canvasRenderedGraphs.push(props.canvasGraph);
      canvasFocusRequests.push({ id: props.focusNodeId || "", nonce: props.focusNodeNonce || 0, place: Boolean(props.focusNodePlace) });
      return <Actual {...props} onCanvasReady={(instance) => { canvasFlowObserver.current = instance; props.onCanvasReady?.(instance); }} />;
    },
  };
});

const mockedFetchWorkflowCanvasGraph = vi.mocked(fetchWorkflowCanvasGraph);
const mockedFetchWorkflowContract = vi.mocked(fetchWorkflowContract);
const mockedFetchWorkflowDag = vi.mocked(fetchWorkflowDag);
const mockedFetchWorkflowDetail = vi.mocked(fetchWorkflowDetail);
const mockedFetchWorkflowExportPolicy = vi.mocked(fetchWorkflowExportPolicy);
const mockedFetchWorkflowValidation = vi.mocked(fetchWorkflowValidation);

const emptySummary = { rule_count: 0, enabled_count: 0, issue_count: 0, error_count: 0, warning_count: 0 };

function graph(partial: Partial<WorkflowCanvasGraph>): WorkflowCanvasGraph {
  return {
    workflow: { id: "router", display_name: "Router", enabled: false, callable_by_parent: false },
    interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
    output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
    entry: null,
    groups: [],
    steps: [],
    edges: [],
    validation_issues: [],
    automation_rules: [],
    ...partial,
  };
}

const NEXT_PORT: WorkflowCanvasPort[] = [{ outcome: { name: "success" }, label: "Next" }];

function layoutCallbacks() {
  return {
    onOpenGroup: vi.fn(),
    onOpenGroupAutomation: vi.fn(),
    onOpenNewStep: vi.fn(),
    onOpenStep: vi.fn(),
    onOpenStepAutomation: vi.fn(),
  };
}

function layoutFor(canvasGraph: WorkflowCanvasGraph, measuredStepHeights?: Record<string, number>) {
  return buildCanvasLayout({ ...layoutCallbacks(), canvasGraph, measuredStepHeights });
}

function node(nodes: ReturnType<typeof buildCanvasLayout>["nodes"], id: string) {
  const found = nodes.find((candidate) => candidate.id === id);
  if (!found) {
    throw new Error(`Missing node ${id}`);
  }
  return found;
}

const routerGraph = graph({
  workflow: { id: "sample_content_campaign_router", display_name: "Demo Content Campaign Router", enabled: true, callable_by_parent: false },
  interface_contract: {
    trigger_channels: ["ui", "parent_workflow"],
    input_mode: "fields",
    expected_inputs: [{ name: "channel", type: "string", required: true }],
    strict_inputs: true,
  },
  output_contract: {
    output_mode: "variables",
    expected_outputs: [{ name: "brief", type: "string", required: true }],
    returned_files: [],
  },
  entry: { kind: "step", id: "route" },
  steps: [
    {
      id: "route",
      step_id: "route",
      kind: "switch",
      node_type: "switchStep",
      display_name: "Route content",
      config_summary: "state.channel (1 case)",
      ports: [
        { outcome: { name: "case", case_id: "email" }, label: "email" },
        { outcome: { name: "default" }, label: "Default" },
      ],
      automation_summary: { ...emptySummary, rule_count: 1, enabled_count: 1 },
      validation_summary: { ...emptySummary, issue_count: 1, error_count: 1 },
    },
    {
      id: "compose_email",
      step_id: "compose_email",
      kind: "agent",
      node_type: "action",
      display_name: "Compose email",
      config_summary: "agent writer",
      ports: [{ outcome: { name: "success" }, label: "Next" }],
    },
    {
      id: "done",
      step_id: "done",
      kind: "stop_success",
      node_type: "end",
      display_name: "Done",
      config_summary: "",
      ports: [],
    },
  ],
  edges: [
    {
      source: { kind: "step", id: "route" },
      outcome: { name: "case", case_id: "email" },
      target: { kind: "step", id: "compose_email" },
    },
    {
      source: { kind: "step", id: "route" },
      outcome: { name: "default" },
      target: { kind: "step", id: "done" },
    },
    {
      source: { kind: "step", id: "compose_email" },
      outcome: { name: "success" },
      target: { kind: "step", id: "done" },
    },
  ],
  validation_issues: [
    {
      scope: "step",
      scope_id: "route",
      authored_id: "route",
      step_id: "route",
      severity: "error",
      level: "error",
      code: "workflow.demo",
      title: "Fix branch target",
      message: "Switch route needs a valid target.",
      field_path: "workflow.steps.route.override.switch.cases",
      suggested_action: "Open the affected router step and fix its condition or selector.",
      issues: [{ message: "Switch route needs a valid target." }],
    },
  ],
});

// Page authoring journeys use the same authenticated admin actor as useIsAdmin.
// Supply its real context so useAuth and environment policy execute normally.
const authValue: AuthContextValue = {
  error: null, isAuthenticated: true, status: "authenticated",
  logout: async () => undefined, refresh: async () => undefined, switchAccount: async () => undefined,
  session: {
    authenticated: true, local_auth_enabled: true, bootstrap_required: false, external_auth_enabled: false,
    roles: ["admin"], permissions: { can_read: true, can_mutate: true }, account_memberships: [],
    user: { id: "builder-admin", email: "builder@example.test", username: "builder", display_name: "Builder",
      roles: ["admin"], account_id: "account-test", account_ids: ["account-test"], account_memberships: [] },
    i18n: { locale: "en", fallback_locale: "en", supported_locales: [], messages: {} },
    feature_flags: {}, environment: { edition: "community", mode: "production" },
    settings_pages: [], registered_pages: [], shell_navigation: {},
  },
};

function renderPage({
  initialEntry = "/registry/workflows/sample_content_campaign_router/builder",
}: {
  initialEntry?: string;
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <AuthContext.Provider value={authValue}><QueryClientProvider client={queryClient}>
      <RouterProvider router={createMemoryRouter([{ path: "*", element: (
        <Routes>
          <Route
            element={<WorkflowsBuilderPage />}
            path="/registry/workflows/:workflowId/builder"
          />
          <Route element={<p>Detail page</p>} path="/registry/workflows/:workflowId" />
        </Routes>
       ) }], { initialEntries: [initialEntry] })} />
    </QueryClientProvider></AuthContext.Provider>,
  );
}

describe("WorkflowsBuilderPage", () => {

  describe("contextual isolated Step Play", () => {
    it.each([
      ["Compose email", "compose_email"],
      ["Route content", "route"],
      ["Done", "done"],
    ])("opens %s from its hovered Play without falling through to normal card navigation", async (name, id) => {
      mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "sample_content_campaign_router", structure_change_allowed: true, step_test_run_allowed: true } as never);
      const user = userEvent.setup({ skipHover: true });
      renderPage();
      const card = await screen.findByRole("group", { name: `Open step ${name}` });
      expect(canvasFlowObserver.current?.getNodes().find((node) => node.id === id)?.selected).not.toBe(true);
      await user.hover(card);
      const play = await within(card).findByRole("button", { name: "Test step" });
      // user-event 14.6.1 omits relatedTarget during hover transitions. Emit the
      // browser's card-to-descendant transition; pixel-gap geometry is real-browser checks.
      fireEvent.mouseOut(card, { relatedTarget: play });
      fireEvent.mouseOver(play, { relatedTarget: card });
      expect(play).toBeInTheDocument();
      expect(play).toBeVisible();
      await user.click(play);
      const drawer = await screen.findByRole("dialog", { name: "Step editor" });
      expect(within(drawer).getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
      // The drawer's subtitle names the step it edits: its ID, after its container's when it has one.
      expect(within(drawer).getByText(new RegExp(`^(?:\\S+ · )?${id}$`))).toBeVisible();
      expect(within(drawer).getByRole("button", { name: "Run step test run" })).toBeInTheDocument();
      expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("dialog", { name: "Workflow interface" })).not.toBeInTheDocument();
    });

    it("exposes the same hover Play on an If card", async () => {
      mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "sample_content_campaign_router", structure_change_allowed: true, step_test_run_allowed: true } as never);
      mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "sample_content_campaign_router", revision: 4, display_name: "Check", enabled: false, groups: [], steps: [{ step_id: "check", display_name: "Check address", step_kind: "if", override: { if: { condition: { selector: "state.address", operator: "truthy" } } } }], settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "check" }, edges: [] } } } as never);
      mockedFetchWorkflowCanvasGraph.mockResolvedValue(graph({ entry: { kind: "step", id: "check" }, steps: [{ id: "check", step_id: "check", display_name: "Check address", kind: "if", node_type: "ifStep", config_summary: "state.address", ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }] }] }));
      const user = userEvent.setup({ skipHover: true });
      renderPage();
      const card = await screen.findByRole("group", { name: "Open step Check address" });
      await user.hover(card);
      const play = await within(card).findByRole("button", { name: "Test step" });
      fireEvent.mouseOut(card, { relatedTarget: play });
      fireEvent.mouseOver(play, { relatedTarget: card });
      expect(play).toBeInTheDocument();
      expect(play).toBeVisible();
      await user.click(play);
      const drawer = await screen.findByRole("dialog", { name: "Step editor" });
      expect(within(drawer).getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
      expect(within(drawer).getByText("check", { exact: true })).toBeVisible();
    });

    it.each(["{Enter}", " "])("retains selected-card Play for Tab and %s after the pointer leaves", async (key) => {
      mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "sample_content_campaign_router", structure_change_allowed: true, step_test_run_allowed: true } as never);
      const user = userEvent.setup();
      renderPage();
      const card = await screen.findByRole("group", { name: "Open step Compose email" });
      const descriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
      // jsdom supplies no layout hit-test; return the actual publicly queried clicked card.
      Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [card] });
      try {
        // Vitest aliases window/defaultView to its global; d3 needs the existing JSDOM Window.
        const nativeRealm = (globalThis as typeof globalThis & { jsdom?: { window: Window } }).jsdom?.window;
        if (!nativeRealm) throw new Error("Selected-card mouse fixture requires Vitest's existing JSDOM Window.");
        fireEvent.mouseDown(card, { view: nativeRealm, button: 0, buttons: 1, detail: 1 });
        fireEvent.mouseUp(card, { view: nativeRealm, button: 0, buttons: 0, detail: 1 });
        fireEvent.click(card, { view: nativeRealm, button: 0, buttons: 0, detail: 1 });
        const drawer = await screen.findByRole("dialog", { name: "Step editor" });
        await user.click(within(drawer).getByRole("button", { name: "Close" }));
        await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
        await user.unhover(card);
        const play = within(card).getByRole("button", { name: "Test step" });
        const more = within(card).getByRole("button", { name: "More actions for Compose email" });
        act(() => more.focus());
        await user.tab({ shift: true });
        expect(play).toHaveFocus();
        await user.keyboard(key);
        const opened = await screen.findByRole("dialog", { name: "Step editor" });
        expect(within(opened).getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
        // The drawer names the step it opened by its id under the title (Panels › Drawer header).
        expect(within(opened).getByText("compose_email", { exact: true })).toBeVisible();
        await user.click(within(opened).getByRole("button", { name: "Close" }));
        await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
        await user.click(more);
        // More holds Duplicate, Disable and Delete (Components › Step actions).
        expect(screen.getByRole("menuitem", { name: "Disable" })).toBeVisible();
      } finally {
        if (descriptor) Object.defineProperty(document, "elementsFromPoint", descriptor);
        else Reflect.deleteProperty(document, "elementsFromPoint");
      }
    });
  });
  it("selects a genuine container created after a preselected source and makes its Next slot own the following append", async () => {
    const edges = routerGraph.edges.filter((edge) => edge.source.id !== "compose_email");
    mockedFetchWorkflowCanvasGraph.mockResolvedValue({ ...routerGraph, edges } as never);
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "sample_content_campaign_router", revision: 4, display_name: "Router", enabled: false, groups: [], steps: [{ step_id: "route", display_name: "Route content", step_kind: "switch", override: { switch: { selector: "state.channel", cases: [{ case_id: "email", when: "email" }] } } }, { step_id: "compose_email", display_name: "Compose email", step_kind: "agent", agent_id: "writer" }, { step_id: "done", display_name: "Done", step_kind: "stop_success" }], settings: { execution_graph: { schema_version: 1, entry: routerGraph.entry, edges } } } as never);
    renderPage();
    const source = await screen.findByRole("group", { name: "Open step Compose email" });
    const descriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
    Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [source] });
    try {
      fireEvent.click(source);
      await waitFor(() => expect(canvasFlowObserver.current?.getNodes().find((node) => node.id === "compose_email")?.selected).toBe(true));
      fireEvent.click(await screen.findByRole("button", { name: "Add next step" }));
      fireEvent.click(screen.getByRole("option", { name: "Parallel lanes" }));
      await waitFor(() => expect(canvasFlowObserver.current?.getNodes().some((node) => node.id === "group:parallel_lanes")).toBe(true));
      expect(canvasFlowObserver.current?.getNodes().find((node) => node.id === "group:parallel_lanes")?.selected).toBe(true);
      expect(canvasFlowObserver.current?.getNodes().find((node) => node.id === "compose_email")?.selected).toBe(false);
      expect(screen.getByRole("dialog", { name: "Group editor" })).toBeVisible();
      fireEvent.click(await screen.findByRole("button", { name: "Add next step" }));
      fireEvent.click(screen.getByRole("option", { name: "Wait" }));
      await screen.findByRole("group", { name: "Open step Wait" });
      expect(canvasRenderedGraphs.at(-1)?.edges).toEqual(expect.arrayContaining([
        { source: { kind: "step", id: "compose_email" }, outcome: { name: "success" }, target: { kind: "group", id: "parallel_lanes" } },
        { source: { kind: "group", id: "parallel_lanes" }, outcome: { name: "success" }, target: { kind: "step", id: "wait" } },
      ]));
    } finally {
      if (descriptor) Object.defineProperty(document, "elementsFromPoint", descriptor);
      else Reflect.deleteProperty(document, "elementsFromPoint");
    }
  });

  it("omits disabled callable outputs from successful Stop summaries while preserving returned files", () => {
    const { nodes } = layoutFor(graph({
      entry: { kind: "step", id: "done" },
      steps: [{ id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] }],
      output_contract: { output_mode: "none", expected_outputs: [{ name: "stale_result", type: "text" }], returned_files: [{ name: "report" }] },
    }));
    expect(node(nodes, "done").data).toMatchObject({ contractSummary: { outputs: [], artifacts: ["report"] } });
  });

  beforeEach(() => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    const svgPrototype = SVGElement.prototype as SVGElement & {
      getBBox?: () => { height: number; width: number; x: number; y: number };
    };
    if (!svgPrototype.getBBox) {
      Object.defineProperty(SVGElement.prototype, "getBBox", {
        configurable: true,
        value: () => ({ height: 16, width: 120, x: 0, y: 0 }),
      });
    }
    canvasFlowObserver.current = null;
    canvasRenderedGraphs.length = 0;
    globalThis.localStorage.clear();
    mockedFetchWorkflowCanvasGraph.mockReset();
    mockedFetchWorkflowContract.mockReset();
    mockedFetchWorkflowDag.mockReset();
    mockedFetchWorkflowDetail.mockReset();
    mockedFetchWorkflowExportPolicy.mockReset();
    mockedFetchWorkflowValidation.mockReset();

    mockedFetchWorkflowExportPolicy.mockResolvedValue({
      workflow_id: "sample_content_campaign_router",
      extension_owned: false,
      protected: false,
      full_export_allowed: true,
      clone_allowed: true,
      structure_change_allowed: true,
    } as never);
    mockedFetchWorkflowContract.mockResolvedValue({
      workflow_id: "sample_content_campaign_router",
      interface: {
        enabled: true,
        trigger_channels: ["ui"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [],
        output_mode: "variables",
        expected_outputs: [],
        returned_files: [],
      },
      definition_issues: [],
    } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      workflow_id: "sample_content_campaign_router",
      validation: { valid: false, errors: [{ code: "workflow.demo", message: "Fix branch target" }], warnings: [] },
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ workflow_id: "sample_content_campaign_router", nodes: [], edges: [] } as never);
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "sample_content_campaign_router",
      revision: 4,
      display_name: "Demo Content Campaign Router",
      enabled: true,
      description: "Campaign routing demo",
      steps: [
        {
          step_id: "route",
          display_name: "Route content",
          step_kind: "switch",
          override: { switch: { selector: "state.channel", cases: [{ case_id: "email", when: "email" }] } },
        },
        { step_id: "compose_email", display_name: "Compose email", step_kind: "agent", agent_id: "writer" },
        { step_id: "done", display_name: "Done", step_kind: "stop_success" },
      ],
      groups: [],
      settings: { execution_graph: { schema_version: 1, entry: routerGraph.entry, edges: routerGraph.edges } },
    } as never);
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(routerGraph as never);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows Add next only for the selected source's free outcome and keeps cancellation local", async () => {
    const edges = routerGraph.edges.filter((edge) => !(edge.source.id === "route" && edge.outcome.name === "default"));
    mockedFetchWorkflowCanvasGraph.mockResolvedValue({ ...routerGraph, edges } as never);
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "sample_content_campaign_router", revision: 4, display_name: "Router", enabled: false, groups: [], steps: [{ step_id: "route", display_name: "Route content", step_kind: "switch", override: { switch: { selector: "state.channel", cases: [{ case_id: "email", when: "email" }] } } }, { step_id: "compose_email", display_name: "Compose email", step_kind: "agent", agent_id: "writer" }, { step_id: "done", display_name: "Done", step_kind: "stop_success" }], settings: { execution_graph: { schema_version: 1, entry: routerGraph.entry, edges } } } as never);
    renderPage();
    const source = await screen.findByRole("group", { name: "Open step Route content" });
    const hitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
    // jsdom has no layout hit testing; this fixture returns the actual clicked node.
    Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [source] });
    try {
      fireEvent.click(source);
      const slot = await screen.findByRole("button", { name: "Default: add step" });
      expect(screen.queryByRole("button", { name: "email: add step" })).not.toBeInTheDocument();
      fireEvent.click(slot);
      // Add step names what it was asked for: after Route content's Default outcome.
      const search = within(await screen.findByRole("dialog", { name: "Add after Route content · Default" })).getByRole("combobox", { name: "Search steps" });
      fireEvent.change(search, { target: { value: "Wait" } });
      fireEvent.keyDown(search, { key: "Escape", code: "Escape" });
      expect(screen.queryByRole("dialog", { name: "Add after Route content · Default" })).not.toBeInTheDocument();
      expect(screen.queryByRole("group", { name: "Open step Wait" })).not.toBeInTheDocument();
      expect(slot).toHaveFocus();
    } finally {
      if (hitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", hitTestDescriptor);
      else Reflect.deleteProperty(document, "elementsFromPoint");
    }
  });

  it("opens a scoped searchable palette and stages the chosen kind before opening its editor", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Add step" }));
    const palette = await screen.findByRole("dialog", { name: "Add step" });
    const search = within(palette).getByRole("combobox", { name: "Search steps" });
    fireEvent.change(search, { target: { value: "Wait" } });
    fireEvent.keyDown(search, { key: "ArrowDown", code: "ArrowDown" });
    fireEvent.keyDown(search, { key: "Enter", code: "Enter" });
    expect(await screen.findByRole("group", { name: "Open step Wait" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Add step" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent(/Structure not applied/));
    expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue("Wait");
  });

  it("uses the physical A shortcut inside the canvas while keeping search input editing native", async () => {
    renderPage();
    await screen.findByRole("button", { name: "Add step" });
    fireEvent.keyDown(screen.getByRole("application"), { key: "ф", code: "KeyA" });
    const palette = await screen.findByRole("dialog", { name: "Add step" });
    const search = within(palette).getByRole("combobox", { name: "Search steps" });
    fireEvent.change(search, { target: { value: "a" } });
    fireEvent.keyDown(search, { key: "a", code: "KeyA" });
    expect(search).toHaveValue("a");
    expect(screen.getAllByRole("dialog", { name: "Add step" })).toHaveLength(1);
    fireEvent.keyDown(search, { key: "Escape", code: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Add step" })).not.toBeInTheDocument();
  });

  it("Escape closes a queried palette without staging a step and returns focus", async () => {
    renderPage();
    const trigger = await screen.findByRole("button", { name: "Add step" });
    fireEvent.click(trigger);
    const palette = await screen.findByRole("dialog", { name: "Add step" });
    const search = within(palette).getByRole("combobox", { name: "Search steps" });
    fireEvent.change(search, { target: { value: "Wait" } });
    fireEvent.keyDown(search, { key: "Escape", code: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Add step" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Open step Wait" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("keeps a long HTTP request address available as a native tooltip on its canvas summary", async () => {
    const address = `https://example.com/${"a".repeat(400)}.csv`;
    const summary = `GET ${address}`;
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(
      graph({
        steps: [
          {
            id: "download",
            step_id: "download",
            kind: "http_request",
            display_name: "Download CSV",
            config_summary: summary,
            ports: [{ outcome: { name: "success" }, label: "Next" }],
          },
        ],
      }) as never,
    );
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "sample_content_campaign_router",
      revision: 1,
      display_name: "HTTP workflow",
      enabled: false,
      steps: [{ step_id: "download", display_name: "Download CSV", step_kind: "http_request" }],
      groups: [],
      settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "download" }, edges: [] } },
    } as never);

    renderPage();

    const frame = await screen.findByRole("group", { name: "Open step Download CSV" });
    const visibleSummary = within(frame).getByText(summary);
    expect(visibleSummary).toHaveAttribute("title", summary);
  });

  it("renders the toolbar with the structure status, Apply and the add menu", async () => {
    renderPage({
      initialEntry:
        "/registry/workflows/sample_content_campaign_router/builder?project_id=project_alpha",
    });

    expect(
      await screen.findByText((content) => content.includes("Canvas") && content.includes("beta")),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back" })).toHaveAttribute(
      "href",
      "/registry/workflows/sample_content_campaign_router?project_id=project_alpha",
    );
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied · 1 error");
    expect(screen.getByRole("button", { name: "Apply changes" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Discard changes" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /1 error/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Test Run" })).toHaveAttribute(
      "href",
      "/registry/workflows/sample_content_campaign_router/builder?drawer=workflow-test-run&project_id=project_alpha",
    );
    expect(screen.getByRole("button", { name: "No failure paths available" })).toBeDisabled();
    const toolbar = screen.getByRole("toolbar", { name: "Canvas tools" });
    // eslint-disable-next-line testing-library/no-node-access -- Compare native button/link toolbar controls in exact DOM order.
    expect(Array.from(toolbar.querySelectorAll("button, a[href]"))).toEqual([
      within(toolbar).getByRole("button", { name: "Add step" }), within(toolbar).getByRole("button", { name: "Add group" }),
      within(toolbar).getByRole("button", { name: "Add note" }),
      within(toolbar).getByRole("button", { name: /Search canvas/ }), within(toolbar).getByRole("button", { name: "Auto layout" }),
      within(toolbar).getByRole("button", { name: "Layout" }),
      within(toolbar).getByRole("button", { name: "No failure paths available" }),
      within(toolbar).getByRole("button", { name: "Zoom out (−)" }), within(toolbar).getByRole("button", { name: "Zoom to 100% (Shift+0)" }),
      within(toolbar).getByRole("button", { name: "Zoom in (+)" }), within(toolbar).getByRole("button", { name: "Fit to screen (Shift+1)" }),
      within(toolbar).getByRole("link", { name: "Test Run" }),
    ]);
    fireEvent.focus(within(toolbar).getByRole("button", { name: "Auto layout" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Auto layout (Shift+A)");
    expect(screen.getByRole("tooltip")).toHaveTextContent("Drag a step to place it manually. Auto layout restores the graph layout.");
    fireEvent.blur(within(toolbar).getByRole("button", { name: "Auto layout" }));
    fireEvent.click(screen.getByRole("button", { name: /Search canvas/ }));
    expect(screen.getByRole("textbox", { name: /Search workflow canvas/i })).toHaveAttribute(
      "placeholder",
      "Search step or group",
    );
    // No phase vocabulary is left on the canvas.
    expect(screen.queryByText(/phase/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add group" }));
    // A plain Group first, then Parallel lanes; For each is a loop and sits in Add step › Flow.
    expect(screen.getAllByRole("menuitem").map((item) => item.getAttribute("aria-label"))).toEqual(["Group", "Parallel lanes"]);
  });

  it("keeps Group, Search, More, Diagnostics and Legend transient surfaces mutually exclusive", async () => {
    renderPage();
    const more = await screen.findByRole("button", { name: "More" });
    const toolbar = screen.getByRole("toolbar", { name: "Canvas tools" });
    const group = within(toolbar).getByRole("button", { name: "Add group" });
    const search = within(toolbar).getByRole("button", { name: /Search canvas/ });
    fireEvent.click(more);
    expect(screen.getByRole("button", { name: "Show legend" })).toBeVisible();
    fireEvent.click(group);
    expect(screen.queryByRole("button", { name: "Show legend" })).not.toBeInTheDocument();
    expect(group).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(more);
    expect(group).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(search);
    expect(screen.queryByRole("button", { name: "Show legend" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Search workflow canvas" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Diagnostics/ }));
    expect(screen.queryByRole("textbox", { name: "Search workflow canvas" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Validation issues" })).toBeVisible();
    fireEvent.click(more);
    expect(screen.queryByRole("region", { name: "Validation issues" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show legend" }));
    expect(screen.getByText("Click nodes, badges and issues to open the inspector in context.")).toBeVisible();
    fireEvent.click(group);
    expect(screen.queryByText("Click nodes, badges and issues to open the inspector in context.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Parallel lanes" }));
    expect(group).toHaveAttribute("aria-expanded", "false");
  });

  it("draws every switch case as its own named port and every edge from that port", () => {
    const { edges, nodes } = layoutFor(routerGraph);
    const route = node(nodes, "route");
    const handleIds = (route.handles || []).map((handle) => handle.id);
    expect(handleIds).toEqual(expect.arrayContaining(["in", "case:email", "default"]));
    // There is no generic "out" exit that a drag could fall back to.
    expect(handleIds).not.toContain("out");
    expect(
      edges.filter((edge) => edge.id !== "visual:entry").map((edge) => [edge.source, edge.sourceHandle, edge.target, edge.targetHandle]),
    ).toEqual([
      ["route", "case:email", "compose_email", "in"],
      ["route", "default", "done", "in"],
      ["compose_email", "success", "done", "in"],
    ]);
    expect(edges.find((edge) => edge.sourceHandle === "case:email")?.label).toBe("email");
    expect(edges.find((edge) => edge.id === "visual:entry")).toMatchObject({ source: "visual:start", target: "route", selectable: false });
    expect(edges.find((edge) => edge.source === "compose_email")?.label).toBeUndefined();
  });

  it("keeps root steps top-level and draws only real groups as containers", () => {
    const groupGraph = graph({
      entry: { kind: "step", id: "fetch" },
      groups: [
        {
          id: "per_item",
          group_id: "per_item",
          display_name: "Per item",
          body: { kind: "sequence", entry: { kind: "step", id: "enrich" } },
        },
      ],
      steps: [
        { id: "fetch", step_id: "fetch", kind: "http_request", display_name: "Fetch", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
        { id: "enrich", step_id: "enrich", group_id: "per_item", kind: "agent", display_name: "Enrich", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
        { id: "finish", step_id: "finish", kind: "stop_success", display_name: "Finish", config_summary: "", ports: [] },
      ],
      edges: [
        { source: { kind: "step", id: "fetch" }, outcome: { name: "success" }, target: { kind: "group", id: "per_item" } },
        { source: { kind: "step", id: "enrich" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "per_item", lane_id: null } },
        { source: { kind: "group", id: "per_item" }, outcome: { name: "success" }, target: { kind: "step", id: "finish" } },
      ],
    });
    const { edges, nodes } = layoutFor(groupGraph);
    const fetchNode = node(nodes, "fetch");
    const finishNode = node(nodes, "finish");
    const groupNode = node(nodes, "group:per_item");
    const enrichNode = node(nodes, "enrich");

    expect(fetchNode.parentId).toBeUndefined();
    expect(fetchNode.extent).toBeUndefined();
    expect(finishNode.parentId).toBeUndefined();
    expect(groupNode.type).toBe("groupContainer");
    expect(enrichNode.parentId).toBe("group:per_item");
    expect(enrichNode.extent).toBe("parent");
    expect((fetchNode.data as { isEntry: boolean }).isEntry).toBe(true);
    expect((enrichNode.data as { isEntry: boolean }).isEntry).toBe(true);
    // Columns follow the graph from the entry, not the list order.
    expect(groupNode.position.x).toBeGreaterThan(fetchNode.position.x);
    expect(finishNode.position.x).toBeGreaterThan(groupNode.position.x);
    expect(
      edges.filter((edge) => edge.id !== "visual:entry").map((edge) => [edge.source, edge.sourceHandle, edge.target, edge.targetHandle]),
    ).toEqual([
      ["fetch", "success", "group:per_item", "entry"],
      ["enrich", "success", "group:per_item", "exit"],
      ["group:per_item", "success", "finish", "in"],
    ]);
  });

  it("draws a For each inside its Group and the item steps inside the For each", () => {
    const nested = graph({
      entry: { kind: "group", id: "prepare" },
      groups: [
        {
          id: "prepare",
          group_id: "prepare",
          display_name: "Prepare",
          body: { kind: "sequence", entry: { kind: "for_each", id: "each_order" } },
        },
      ],
      for_each: [
        {
          id: "each_order",
          for_each_id: "each_order",
          group_id: "prepare",
          display_name: "Each order",
          over: "state.orders",
          item_name: "order",
          body: { kind: "sequence", entry: { kind: "step", id: "keep" } },
        },
      ],
      steps: [
        { id: "keep", step_id: "keep", for_each_id: "each_order", kind: "collect", display_name: "Keep", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
        { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
      ],
      edges: [
        { source: { kind: "step", id: "keep" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "each_order", lane_id: null } },
        { source: { kind: "for_each", id: "each_order" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "prepare", lane_id: null } },
        { source: { kind: "group", id: "prepare" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
      ],
    });
    const { edges, nodes } = layoutFor(nested);
    const forEachNode = node(nodes, "for_each:each_order");
    const keepNode = node(nodes, "keep");

    expect(forEachNode.type).toBe("forEachContainer");
    expect(forEachNode.parentId).toBe("group:prepare");
    expect(keepNode.parentId).toBe("for_each:each_order");
    expect((forEachNode.data as { isEntry: boolean }).isEntry).toBe(true);
    // A parent node is listed before its children, as React Flow requires.
    const order = nodes.map((item) => item.id);
    expect(order.indexOf("group:prepare")).toBeLessThan(order.indexOf("for_each:each_order"));
    expect(order.indexOf("for_each:each_order")).toBeLessThan(order.indexOf("keep"));
    expect(
      edges.filter((edge) => edge.id !== "visual:entry").map((edge) => [edge.source, edge.sourceHandle, edge.target, edge.targetHandle]),
    ).toEqual([
      ["keep", "success", "for_each:each_order", "exit"],
      ["for_each:each_order", "success", "group:prepare", "exit"],
      ["group:prepare", "success", "done", "in"],
    ]);
  });

  it("lays out parallel lanes as rows of one group and flags nodes the entry never reaches", () => {
    const parallelGraph = graph({
      entry: { kind: "group", id: "fanout" },
      groups: [
        {
          id: "fanout",
          group_id: "fanout",
          display_name: "Fan out",
          body: {
            kind: "lanes",
            lanes: [
              { lane_id: "copy", entry: { kind: "step", id: "copy" } },
              { lane_id: "image", entry: { kind: "step", id: "image" } },
            ],
          },
        },
      ],
      steps: [
        { id: "copy", step_id: "copy", group_id: "fanout", lane_id: "copy", kind: "agent", display_name: "Copy", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
        { id: "image", step_id: "image", group_id: "fanout", lane_id: "image", kind: "agent", display_name: "Image", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
        { id: "orphan", step_id: "orphan", kind: "set_variable", display_name: "Orphan", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      ],
      edges: [],
    });
    const { nodes } = layoutFor(parallelGraph);
    const copy = node(nodes, "copy");
    const image = node(nodes, "image");
    expect(copy.parentId).toBe("group:fanout");
    expect(image.parentId).toBe("group:fanout");
    expect(image.position.y).toBeGreaterThan(copy.position.y);
    expect((node(nodes, "orphan").data as { unreachable: boolean }).unreachable).toBe(true);
    expect((node(nodes, "group:fanout").data as { lanes: string[] }).lanes).toEqual(["copy", "image"]);
  });

  it("places a step the entry does not reach at the common column after the main graph", () => {
    const linear = graph({
      entry: { kind: "step", id: "fetch" },
      steps: [
        { id: "fetch", step_id: "fetch", kind: "http_request", display_name: "Fetch", config_summary: "", ports: NEXT_PORT },
        { id: "store", step_id: "store", kind: "set_variable", display_name: "Store", config_summary: "", ports: NEXT_PORT },
        { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
        { id: "new_step", step_id: "new_step", kind: "set_variable", display_name: "New step", config_summary: "", ports: NEXT_PORT },
      ],
      edges: [
        { source: { kind: "step", id: "fetch" }, outcome: { name: "success" }, target: { kind: "step", id: "store" } },
        { source: { kind: "step", id: "store" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
      ],
    });
    const { nodes } = layoutFor(linear);
    const fetchNode = node(nodes, "fetch");
    const added = node(nodes, "new_step");
    expect(added.position.x).toBe(node(nodes, "done").position.x + node(nodes, "done").width! + 96);
    expect(added.position.y).toBe(fetchNode.position.y);
    expect(added.position.x).toBeGreaterThan(node(nodes, "store").position.x);
    expect(added.position.x).toBeGreaterThan(node(nodes, "done").position.x);
    expect(node(layoutFor(linear).nodes, "new_step").position).toEqual(added.position);
    expect((added.data as { unreachable: boolean }).unreachable).toBe(true);
  });

  it("offers a + only on routed outcomes that lead nowhere, never on Stop steps or a read-only canvas", () => {
    const branching = graph({
      entry: { kind: "step", id: "check" },
      steps: [
        {
          id: "check", step_id: "check", kind: "if", display_name: "Check", config_summary: "",
          ports: [{ outcome: { name: "true" }, label: "True" }, { outcome: { name: "false" }, label: "False" }],
        },
        { id: "yes", step_id: "yes", kind: "set_variable", display_name: "Yes", config_summary: "", ports: NEXT_PORT },
        { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
      ],
      edges: [
        { source: { kind: "step", id: "check" }, outcome: { name: "true" }, target: { kind: "step", id: "yes" } },
        { source: { kind: "step", id: "done" }, outcome: { name: "success" }, target: { kind: "step", id: "yes" } },
      ],
    });
    const onAppendStep = vi.fn();
    const free = (nodes: ReturnType<typeof buildCanvasLayout>["nodes"], id: string) =>
      (node(nodes, id).data as { freeOutcomes?: string[] }).freeOutcomes;
    const authoring = buildCanvasLayout({ ...layoutCallbacks(), canvasGraph: branching, onAppendStep });
    expect(free(authoring.nodes, "check")).toEqual(["false"]);
    expect(free(authoring.nodes, "yes")).toEqual(["success"]);
    expect(free(authoring.nodes, "done")).toEqual([]);
    // The error port stays open by design: unconnected means the run fails.
    expect(free(authoring.nodes, "yes")).not.toContain("error");
    for (const locked of [{ readOnly: true }, { structureLocked: true }, { onAppendStep: undefined }]) {
      const view = buildCanvasLayout({ ...layoutCallbacks(), canvasGraph: branching, onAppendStep, ...locked });
      expect(free(view.nodes, "check")).toEqual([]);
      expect(free(view.nodes, "yes")).toEqual([]);
    }
  });

  it("uses measured step heights to grow a step and its group", () => {
    const groupGraph = graph({
      entry: { kind: "group", id: "loop" },
      groups: [{ id: "loop", group_id: "loop", display_name: "Loop", body: { kind: "sequence", entry: { kind: "step", id: "inner" } } }],
      steps: [{ id: "inner", step_id: "inner", group_id: "loop", kind: "agent", display_name: "Inner", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] }],
    });
    const base = layoutFor(groupGraph);
    const measured = layoutFor(groupGraph, { inner: 600 });
    expect(Number(node(measured.nodes, "inner").height)).toBeGreaterThan(Number(node(base.nodes, "inner").height));
    expect(Number(node(measured.nodes, "group:loop").height)).toBeGreaterThan(
      Number(node(base.nodes, "group:loop").height),
    );
  });

  it("offers an error port before routing is configured as well as after", () => {
    const errorGraph = graph({
      entry: { kind: "step", id: "call" },
      steps: [
        {
          id: "call",
          step_id: "call",
          kind: "http_request",
          display_name: "Call",
          config_summary: "",
          ports: [
            { outcome: { name: "success" }, label: "Next" },
            { outcome: { name: "error" }, label: "On error" },
          ],
        },
        { id: "plain", step_id: "plain", kind: "http_request", display_name: "Plain", config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      ],
    });
    const { nodes } = layoutFor(errorGraph);
    expect((node(nodes, "call").handles || []).map((handle) => handle.id)).toContain("error");
    expect((node(nodes, "plain").handles || []).map((handle) => handle.id)).toContain("error");
  });

  it("opens validation issues in the step drawer", async () => {
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /1 error/i }));
    const validationPanel = await screen.findByRole("region", {
      name: "Validation issues",
    });
    expect(within(validationPanel).getByText("step: Route content (route)")).toBeInTheDocument();
    fireEvent.click(within(validationPanel).getByRole("button", { name: /Fix branch target/i }));

    await waitFor(() => {
      expect(screen.getByText("Step editor")).toBeInTheDocument();
    });
    expect(screen.getByText("Switch route needs a valid target.")).toBeInTheDocument();
  });

  function dryRunFetch(answer: (body: Record<string, unknown>) => Response) {
    const bodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/changes") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        bodies.push(body);
        return answer(body);
      }
      return new Response("{}", { status: 404, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    return bodies;
  }

  function jsonResponse(payload: unknown, status = 200) {
    return new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
  }

  it("keeps More to view and workflow commands, with notes on the canvas toolbar", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "More" }));
    const moreMenu = screen.getByRole("dialog", { name: "More" });
    expect(within(moreMenu).getByRole("button", { name: "History" })).toBeVisible();
    for (const removed of ["Add note", "For each", "Workflow settings", "Schedules"]) {
      expect(within(moreMenu).queryByRole("button", { name: removed })).not.toBeInTheDocument();
    }
    expect(within(moreMenu).getByRole("button", { name: "Show legend" })).toBeVisible();
    const toolbar = screen.getByRole("toolbar", { name: "Canvas tools" });
    expect(within(toolbar).getByRole("button", { name: "Add note" })).toBeVisible();
    fireEvent.click(within(moreMenu).getByRole("button", { name: "History" }));
    expect(await screen.findByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
  });

  it("preserves separate note persistence from the canvas toolbar", async () => {
    const writes: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/annotations/") && init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        writes.push(body);
        return jsonResponse({ revision: 4, annotations_revision: 1, annotations: [body.annotation] });
      }
      return jsonResponse({}, 404);
    }));
    renderPage();
    const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
    fireEvent.click(within(toolbar).getByRole("button", { name: "Add note" }));
    // The note is written in place on the canvas: no dialog, the paper holds the text field.
    const field = await screen.findByRole("textbox", { name: "Note text" });
    expect(screen.queryByRole("dialog", { name: "Note" })).not.toBeInTheDocument();
    expect(field).toHaveAttribute("placeholder", "Write a note… Markdown works");
    expect(screen.getByLabelText("Add image")).toBeEnabled();
    fireEvent.change(field, { target: { value: "# Saved note\nCheck the customer data." } });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes).toEqual([{ expected_revision: 4, expected_annotations_revision: 0, annotation: expect.objectContaining({ text: "# Saved note\nCheck the customer data.", width: 256, height: 160 }) }]);
    expect(screen.getByRole("button", { name: "Apply changes" })).toBeDisabled();
    expect(await screen.findByRole("heading", { name: "Saved note" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Note text" })).not.toBeInTheDocument();
  });

  it("deletes a note selected by a click on its paper with Delete, and Undo brings it back", async () => {
    const writes: Record<string, unknown>[] = [];
    const deletes: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/annotations/") && init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        writes.push(body);
        return jsonResponse({ revision: 4, annotations_revision: writes.length, annotations: [body.annotation] });
      }
      if (String(input).includes("/annotations/") && init?.method === "DELETE") deletes.push(String(input));
      return jsonResponse({}, 404);
    }));
    renderPage();
    const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
    fireEvent.click(within(toolbar).getByRole("button", { name: "Add note" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Note text" }), { target: { value: "# Packing\nPer item." } });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    const paper = await screen.findByRole("group", { name: "Note: Packing" });
    // jsdom has no hit testing; a node click asks it whether a secondary action was hit.
    const hitTest = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
    Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] });
    onTestFinished(() => { if (hitTest) Object.defineProperty(document, "elementsFromPoint", hitTest); else Reflect.deleteProperty(document, "elementsFromPoint"); });
    // The note keeps its own selection; the canvas Delete key must still reach it.
    fireEvent.click(paper);
    fireEvent.keyDown(paper, { key: "Delete" });
    const undo = await screen.findByRole("button", { name: "Undo" });
    expect(screen.getByText("Note deleted.")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Note: Packing" })).not.toBeInTheDocument();
    expect(deletes).toEqual([]);
    fireEvent.click(undo);
    expect(await screen.findByRole("group", { name: "Note: Packing" })).toBeInTheDocument();
  });

  it("drops a new note left empty without writing it", async () => {
    const request = vi.fn<(input: RequestInfo | URL) => Promise<Response>>(async () => jsonResponse({}, 404));
    vi.stubGlobal("fetch", request);
    renderPage();
    const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
    fireEvent.click(within(toolbar).getByRole("button", { name: "Add note" }));
    fireEvent.keyDown(await screen.findByRole("textbox", { name: "Note text" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Note text" })).not.toBeInTheDocument());
    expect(request.mock.calls.filter(([url]) => String(url).includes("/annotations"))).toEqual([]);
  });

  async function addReviewDraftStep() {
    fireEvent.click(await screen.findByRole("button", { name: "Add step" }));
    const palette = await screen.findByRole("dialog", { name: "Add step" });
    fireEvent.click(within(palette).getByRole("option", { name: "Set variable" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Review draft" } });
    // The choice created the canonical local node already. Saving its fields
    // updates that staged create operation and retains its generated identity.
    fireEvent.click(await screen.findByRole("button", { name: "Update new step" }));
  }

  it("adds a root step to the unapplied structure, dry-runs it, and warns before leaving with it", async () => {
    const bodies = dryRunFetch(() =>
      jsonResponse({
        workflow_id: "wf_router",
        revision: 3,
        valid: false,
        applied: false,
        issues: [{ code: "workflow.outcome_unrouted", message: "Outcome success of set_variable leads nowhere.", path: "workflow.steps.set_variable" }],
        blocking_issues: [],
        workflow: {},
      }),
    );
    renderPage();
    await addReviewDraftStep();

    await waitFor(() => {
      expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    });
    expect(screen.getByRole("button", { name: "Apply changes" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Discard changes" })).toBeInTheDocument();
    expect(await screen.findByRole("group", { name: "Open step Review draft" })).toBeInTheDocument();
    // The server judges the exact local set without writing: the issues come from its dry run.
    const kept = await screen.findByTestId("change-check-kept");
    expect(kept).toHaveTextContent("Issues the saved definition keeps:");
    expect(kept).toHaveTextContent("Outcome success of set_variable leads nowhere.");
    expect(screen.queryByTestId("change-check-blocking")).not.toBeInTheDocument();
    expect(bodies.at(-1)).toMatchObject({
      dry_run: true,
      operations: [{ op: "create_step", step: { step_id: "set_variable", step_kind: "set_variable" } }],
    });

    fireEvent.click(screen.getByRole("link", { name: "Back" }));
    const leaveDialog = await screen.findByRole("dialog", { name: "Leave the builder?" });
    fireEvent.click(within(leaveDialog).getByRole("button", { name: "Stay" }));
    expect(screen.queryByRole("dialog", { name: "Leave the builder?" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    await waitFor(() => {
      expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    });
    expect(screen.queryByRole("group", { name: "Open step Review draft" })).not.toBeInTheDocument();
    expect(bodies.every((body) => body.dry_run === true)).toBe(true);
  }, 15_000);

  it("shows what would make Apply refuse an enabled workflow's set", async () => {
    dryRunFetch(() =>
      jsonResponse({
        workflow_id: "wf_router",
        revision: 3,
        valid: false,
        applied: false,
        issues: [
          { code: "workflow.outcome_unrouted", message: "Outcome success of set_variable leads nowhere." },
          { code: "readiness.agent_missing", message: "Agent writer is not installed." },
        ],
        blocking_issues: [{ code: "workflow.outcome_unrouted", message: "Outcome success of set_variable leads nowhere." }],
        workflow: {},
      }),
    );
    renderPage();
    await addReviewDraftStep();

    const blocking = await screen.findByTestId("change-check-blocking");
    expect(blocking).toHaveTextContent("Apply will be refused:");
    expect(blocking).toHaveTextContent("Outcome success of set_variable leads nowhere.");
    const kept = screen.getByTestId("change-check-kept");
    expect(kept).toHaveTextContent("Agent writer is not installed.");
    expect(kept).not.toHaveTextContent("leads nowhere");
  });

  it("falls back to the local estimate only when the dry run cannot be made", async () => {
    dryRunFetch(() => jsonResponse({ detail: "unavailable" }, 503));
    renderPage();
    await addReviewDraftStep();

    expect(await screen.findByText("The server check is unavailable; these issues are a local estimate.")).toBeInTheDocument();
    expect(screen.getByTestId("change-check-kept")).toHaveTextContent(/leads nowhere/);
  });

  it("asks the canvas to centre on a root step it adds, placed in view", async () => {
    canvasFocusRequests.length = 0;
    dryRunFetch(() => jsonResponse({ workflow_id: "wf_router", revision: 4, valid: true, applied: false, issues: [], blocking_issues: [], workflow: {} }));
    renderPage();
    // Before the step exists nothing is focused; adding it is one new request.
    expect(await screen.findByRole("button", { name: "Add step" })).toBeInTheDocument();
    expect(canvasFocusRequests.every((item) => item.nonce === 0)).toBe(true);
    await addReviewDraftStep();

    await waitFor(() => expect(canvasFocusRequests.at(-1)).toMatchObject({ id: "set_variable", nonce: 1, place: true }));
  });

  it("adds a step after a free outcome from its +, created already connected after that step", async () => {
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(
      graph({
        entry: { kind: "step", id: "plan" },
        steps: [{ id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan", config_summary: "", ports: NEXT_PORT }],
      }) as never,
    );
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "sample_content_campaign_router",
      revision: 4,
      display_name: "Plan workflow",
      enabled: false,
      steps: [{ step_id: "plan", display_name: "Plan", step_kind: "set_variable" }],
      groups: [],
      settings: { execution_graph: { schema_version: 1, entry: { kind: "step", id: "plan" }, edges: [] } },
    } as never);
    const bodies = dryRunFetch(() => jsonResponse({ workflow_id: "wf_router", revision: 4, valid: true, applied: false, issues: [], blocking_issues: [], workflow: {} }));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Add a step after Plan · Next, not connected" }));
    const palette = await screen.findByRole("dialog", { name: "Add after Plan · Next" });
    // A free outcome takes branching kinds as well as an ordinary Next kind.
    for (const name of ["If", "Switch", "Set variable"]) expect(within(palette).getByRole("option", { name })).toBeInTheDocument();
    fireEvent.click(within(palette).getByRole("option", { name: "Set variable" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Store plan" } });
    fireEvent.click(await screen.findByRole("button", { name: "Update new step" }));

    await waitFor(() => expect(bodies.at(-1)).toMatchObject({
      dry_run: true,
      operations: [
        { op: "create_step", step: { step_id: "set_variable", step_kind: "set_variable", group_id: null, for_each_id: null, lane_id: null } },
        { op: "connect", edge: { source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "step", id: "set_variable" } } },
      ],
    }));
    // The outcome is taken now, so its + is gone; the new step offers its own.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Add a step after Plan · Next, not connected" })).not.toBeInTheDocument());
    expect(await screen.findByRole("button", { name: "Add a step after Store plan · Next, not connected" })).toBeInTheDocument();
    // The canvas centres on it; a connected step keeps its place in the layout.
    expect(canvasFocusRequests.at(-1)).toMatchObject({ id: "set_variable", place: false });
  });

  describe("responsive canvas toolbar", () => {
    function observeToolbarWidth() {
      const observers = new Set<ControlledResizeObserver>();
      class ControlledResizeObserver implements ResizeObserver {
        readonly targets = new Set<Element>();
        constructor(readonly callback: ResizeObserverCallback) { observers.add(this); }
        observe(target: Element) { this.targets.add(target); }
        unobserve(target: Element) { this.targets.delete(target); }
        disconnect() { this.targets.clear(); observers.delete(this); }
        notify() { this.callback([], this); }
      }
      vi.stubGlobal("ResizeObserver", ControlledResizeObserver);
      return async () => {
        const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
        const region = screen.getByTestId("workflow-canvas-controls");
        let available = 804;
        let labelled = 696.5;
        const regionRect = vi.spyOn(region, "getBoundingClientRect").mockImplementation(() => new DOMRect(0, 0, available, 42));
        const toolbarRect = vi.spyOn(toolbar, "getBoundingClientRect").mockImplementation(() => new DOMRect(0, 0, labelled, 42));
        return {
          toolbar,
          resize(width: number, labelledWidth = 696.5) {
            available = width;
            labelled = labelledWidth;
            act(() => { observers.forEach((observer) => { if (observer.targets.has(region)) observer.notify(); }); });
          },
          restore() { regionRect.mockRestore(); toolbarRect.mockRestore(); },
        };
      };
    }

    function expectToolbarActions(toolbar: HTMLElement) {
      const tools = within(toolbar);
      const controls = tools.getAllByRole("button").concat(tools.getAllByRole("link"));
      // Eleven actions plus the Layout arrow beside Auto layout.
      expect(controls).toHaveLength(12);
      expect(tools.getByRole("button", { name: "Layout" })).toBeEnabled();
      expect(tools.getByRole("button", { name: "Add step" })).toBeEnabled();
      expect(tools.getByRole("button", { name: "Add group" })).toBeEnabled();
      expect(tools.getByRole("button", { name: "Add note" })).toBeEnabled();
      expect(tools.getByRole("button", { name: /Search canvas/ })).toBeEnabled();
      expect(tools.getByRole("button", { name: "Auto layout" })).toBeEnabled();
      expect(tools.getByRole("button", { name: "No failure paths available" })).toBeDisabled();
      expect(tools.getByRole("button", { name: "Zoom out (−)" })).toBeEnabled();
      expect(tools.getByRole("button", { name: "Zoom to 100% (Shift+0)" })).toHaveTextContent(/\d+%/);
      expect(tools.getByRole("button", { name: "Zoom in (+)" })).toBeEnabled();
      expect(tools.getByRole("button", { name: "Fit to screen (Shift+1)" })).toBeEnabled();
      expect(tools.getByRole("link", { name: "Test Run" })).toHaveAttribute("href", "/registry/workflows/sample_content_campaign_router/builder?drawer=workflow-test-run&project_id=project_alpha");
      return tools;
    }

    it("keeps all eleven actions while collapsing captions only when their measured row does not fit", async () => {
      const connect = observeToolbarWidth();
      renderPage({ initialEntry: "/registry/workflows/sample_content_campaign_router/builder?project_id=project_alpha" });
      const measured = await connect();
      try {
        measured.resize(534);
        const tools = expectToolbarActions(measured.toolbar);
        expect(tools.getByText("Step", { exact: true })).not.toBeVisible();
        expect(tools.getByText("Group", { exact: true })).not.toBeVisible();
        expect(tools.getByText("Auto layout", { exact: true })).not.toBeVisible();
        expect(tools.getByText("Test Run", { exact: true })).not.toBeVisible();
        expect(tools.getByText("0", { exact: true })).toBeVisible();
        // The expanded read sees the captions the compact row hides; reading
        // them hidden flips compact back and loops until React gives up.
        measured.toolbar.dataset.measureExpanded = "true";
        expect(tools.getByText("Step", { exact: true })).toBeVisible();
        expect(tools.getByText("Step", { exact: true })).not.toHaveAttribute("hidden");
        delete measured.toolbar.dataset.measureExpanded;
        const autoLayout = tools.getByRole("button", { name: "Auto layout" });
        act(() => autoLayout.focus());
        measured.resize(804);
        expect(tools.getByText("Step", { exact: true })).toBeVisible();
        expect(tools.getByText("Group", { exact: true })).toBeVisible();
        expect(tools.getByText("Auto layout", { exact: true })).toBeVisible();
        expect(tools.getByText("Test Run", { exact: true })).toBeVisible();
        expect(autoLayout).toHaveFocus();
        expect(tools.getByRole("button", { name: "Auto layout" })).toBe(autoLayout);
        measured.resize(534);
        expect(tools.getByText("Step", { exact: true })).not.toBeVisible();
        expect(autoLayout).toHaveFocus();
        expectToolbarActions(measured.toolbar);
      } finally { measured.restore(); }
    });

    it("re-evaluates intrinsic caption width without a viewport breakpoint", async () => {
      const connect = observeToolbarWidth();
      renderPage({ initialEntry: "/registry/workflows/sample_content_campaign_router/builder?project_id=project_alpha" });
      const measured = await connect();
      try {
        measured.resize(1235, 1100);
        expect(within(measured.toolbar).getByText("Step", { exact: true })).toBeVisible();
        measured.resize(804, 900);
        expect(within(measured.toolbar).getByText("Step", { exact: true })).not.toBeVisible();
        measured.resize(804, 696.5);
        expect(within(measured.toolbar).getByText("Step", { exact: true })).toBeVisible();
        expectToolbarActions(measured.toolbar);
      } finally { measured.restore(); }
    });

    it("preserves compact keyboard activation, names and actual shortcut help", async () => {
      const user = userEvent.setup();
      const connect = observeToolbarWidth();
      renderPage({ initialEntry: "/registry/workflows/sample_content_campaign_router/builder?project_id=project_alpha" });
      const measured = await connect();
      try {
        measured.resize(534);
        const tools = expectToolbarActions(measured.toolbar);
        expect(tools.getByText("Step", { exact: true })).not.toBeVisible();
        const group = tools.getByRole("button", { name: "Add group" });
        act(() => group.focus());
        await user.keyboard("{Enter}");
        expect(group).toHaveAttribute("aria-expanded", "true");
        expect(screen.getByRole("menuitem", { name: "Parallel lanes" })).toBeVisible();
        expect(screen.getByRole("menuitem", { name: "Group" })).toBeVisible();
        await user.keyboard("{Control>}f{/Control}");
        expect(group).toHaveAttribute("aria-expanded", "false");
        const search = screen.getByRole("textbox", { name: "Search workflow canvas" });
        expect(search).toHaveFocus();
        await user.keyboard("a");
        expect(search).toHaveValue("a");
        expect(screen.queryByRole("listbox", { name: "Add step" })).not.toBeInTheDocument();
        const autoLayout = tools.getByRole("button", { name: "Auto layout" });
        act(() => autoLayout.focus());
        expect(screen.getByRole("tooltip")).toHaveTextContent("Auto layout (Shift+A)");
        expect(screen.getByRole("tooltip")).toHaveTextContent("Drag a step to place it manually.");
        // Tab goes on to the Layout arrow beside Auto layout, then past the disabled failure paths to zoom.
        await user.tab();
        expect(tools.getByRole("button", { name: "Layout" })).toHaveFocus();
        await user.tab();
        expect(tools.getByRole("button", { name: "Zoom out (−)" })).toHaveFocus();
      } finally { measured.restore(); }
    });

    it("ignores zero geometry and recovers on a valid controls-region observation", async () => {
      const connect = observeToolbarWidth();
      renderPage({ initialEntry: "/registry/workflows/sample_content_campaign_router/builder?project_id=project_alpha" });
      const measured = await connect();
      try {
        measured.resize(0, 0);
        expect(within(measured.toolbar).getByText("Step", { exact: true })).toBeVisible();
        measured.resize(534);
        expect(within(measured.toolbar).getByText("Step", { exact: true })).not.toBeVisible();
        measured.resize(0, 0);
        expect(within(measured.toolbar).getByText("Step", { exact: true })).not.toBeVisible();
        measured.resize(804);
        expect(within(measured.toolbar).getByText("Step", { exact: true })).toBeVisible();
      } finally { measured.restore(); }
    });
  });
});
