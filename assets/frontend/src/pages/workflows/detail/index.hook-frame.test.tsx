import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  canvasGraphFixture,
  mockedUseAuth,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowExportPolicy,
  mockedFindWorkflowLinkedProjectId,
  mockedFetchWorkflowLinkedProjects,
  mockedTestRunWorkflowStep,
  mockedTestRunWorkflow,
  mockedFetchWorkflowContract,
  postedChangeSets,
  dryRunChangeSets,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";
import { WorkflowHookOfLinks } from "./detail-hook-of-links";
import { WorkflowsDetailPage } from "./index";
import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
const hitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
beforeAll(() => Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] }));
afterAll(() => {
  if (hitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", hitTestDescriptor);
  else Reflect.deleteProperty(document, "elementsFromPoint");
});


const executionEvidence = vi.hoisted(() => ({
  parent: { jobId: "parent-marks", definitionWorkflowId: "weekly_report", definitionRevision: 7,
    definitionSteps: [{ stepId: "before_finish", hookId: "before_finish" }],
    steps: [{ stepId: "before_finish", stepExecutionId: "hook-execution", status: "completed" }],
    childInvocations: [{ stepId: "before_finish", stepExecutionId: "hook-execution", invocationKey: "parent:hook-execution", childJobId: "child-marks", childWorkflowId: "client_fix" }], complete: true, terminal: true } as JobExecutionSnapshot,
  child: { jobId: "child-marks", definitionWorkflowId: "client_fix", definitionRevision: 3,
    definitionSteps: [{ stepId: "fix_name" }], steps: [{ stepId: "fix_name", stepExecutionId: "fix-execution", status: "completed" }], childInvocations: [], complete: true, terminal: true } as JobExecutionSnapshot,
}));
// Keep the real workflow shell/surface, observers, ownership mapper and canvas.
vi.mock("@/pages/jobs/detail", async () => {
  const { useEffect } = await import("react");
  return { JobsDetailPage: ({ jobId, onExecutionSnapshotChange }: { jobId: string; onExecutionSnapshotChange?: (snapshot: JobExecutionSnapshot | null) => void }) => {
    useEffect(() => { if (jobId === "parent-marks") onExecutionSnapshotChange?.(executionEvidence.parent); }, [jobId, onExecutionSnapshotChange]);
    return null;
  } };
});
// The selected run's own observer reads the parent archive (Results no longer embed Job Details).
vi.mock("@/pages/jobs/detail/job-execution-snapshot", () => ({ useJobExecutionSnapshot: (jobId: string, enabled: boolean) => ({
  snapshot: !enabled ? null : jobId === "child-marks" ? executionEvidence.child : jobId === "parent-marks" ? executionEvidence.parent : null, pending: false, error: null }) }));

vi.setConfig({ testTimeout: 20_000 });

const NEXT = [{ outcome: { name: "success" as const }, label: "Next" }];
const PROTECTED = {
  extension_owned: true,
  protected: true,
  structure_change_allowed: false,
  step_test_run_allowed: false,
  reference_export_allowed: true,
};
const HOOK = {
  hook_id: "before_finish",
  contract_version: 1,
  display_name: "Before the order is finished",
  description: "Adjust the customer name before the order is finished.",
  input_schema: { type: "object", required: ["customer"], properties: { customer: { type: "string" } } },
  input_mapping: { customer: "state.inputs.customer" },
  mutable_fields: ["customer"],
  additional_outputs: {},
  stage_contract: { summary: "Runs before the order is finished.", effects_before: [], effects_after: [] },
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const changeUrls: string[] = [];

/** The author's protected workflow with one hook, and the project's client workflow connected to it. */
function installHookedWorkflow({ connected = true, authorEditable = false, storage }: { connected?: boolean; authorEditable?: boolean; storage?: "empty" | "one-step" } = {}) {
  changeUrls.length = 0;
  const authorSteps = [
    { step_id: "prepare", display_name: "Prepare the order", step_kind: "set_variable", override: { set_variable: { assignments: { "state.order": true } } } },
    { step_id: "before_finish", display_name: "Before the order is finished", step_kind: "invoke_workflow", override: { invoke_workflow: { hook: HOOK } } },
    { step_id: "done", display_name: "Order finished", step_kind: "stop_success" },
  ];
  const authorEdges = [
    { source: { kind: "step", id: "prepare" }, outcome: { name: "success" }, target: { kind: "step", id: "before_finish" } },
    { source: { kind: "step", id: "before_finish" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
  ];
  // R2-11: a storage child keeps the hook's steps and ends at its hidden end;
  // the older fixture is a child workflow with a visible Stop of its own.
  const endId = storage ? "hook_end" : "return";
  const fixName = { step_id: "fix_name", display_name: "Fix the customer name", step_kind: "set_variable", override: { set_variable: { assignments: { "state.customer": "Acme" } } } };
  const childSteps = [
    ...(storage === "empty" ? [] : [fixName]),
    storage ? { step_id: endId, step_kind: "stop_success" } : { step_id: endId, display_name: "Return the result", step_kind: "stop_success" },
  ];
  const childEdges = storage === "empty" ? [] : [
    { source: { kind: "step", id: "fix_name" }, outcome: { name: "success" }, target: { kind: "step", id: endId } },
  ];
  const childEntry = storage === "empty" ? { kind: "step", id: endId } : { kind: "step", id: "fix_name" };
  const author = workflowDetailFixture({ steps: authorSteps, edges: authorEdges, portability: authorEditable
    ? { ...PROTECTED, extension_owned: false, protected: false, structure_change_allowed: true }
    : PROTECTED });
  const child = { ...(workflowDetailFixture({ steps: childSteps, edges: childEdges, entry: childEntry, revision: 3 }) as Record<string, unknown>), workflow_id: "client_fix", display_name: "Fix the name" };
  mockedFetchWorkflowDetail.mockImplementation(async (workflowId: string) => (workflowId === "client_fix" ? child : author) as never);
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (workflowId: string) => (workflowId === "client_fix"
    ? canvasGraphFixture({
        entry: childEntry,
        steps: [
          ...(storage === "empty" ? [] : [{ id: "fix_name", step_id: "fix_name", kind: "set_variable", display_name: "Fix the customer name", config_summary: "state.customer", ports: NEXT }]),
          { id: endId, step_id: endId, kind: "stop_success", display_name: storage ? "" : "Return the result", config_summary: "", ports: [] },
        ],
        edges: childEdges,
        workflow: { id: "client_fix", display_name: "Fix the name", enabled: true, callable_by_parent: true, revision: 3 },
      })
    : canvasGraphFixture({
        entry: { kind: "step", id: "prepare" },
        steps: [
          { id: "prepare", step_id: "prepare", kind: "set_variable", display_name: "Prepare the order", config_summary: "state.order", ports: NEXT },
          { id: "before_finish", step_id: "before_finish", kind: "invoke_workflow", display_name: "Before the order is finished", config_summary: "hook", ports: NEXT },
          { id: "done", step_id: "done", kind: "stop_success", display_name: "Order finished", config_summary: "", ports: [] },
        ],
        edges: authorEdges,
        protected_workflow: !authorEditable,
        read_only: !authorEditable,
      })) as never);
  mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "weekly_report", ...PROTECTED,
    ...(authorEditable ? { extension_owned: false, protected: false, structure_change_allowed: true } : {}),
  } as never);
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  mockedFetchWorkflowLinkedProjects.mockResolvedValue([{ id: "project_alpha", name: "Alpha project" }] as never);
  let bound = connected;
  const hookRow = () => ({
    project_id: "project_alpha",
    workflow_id: "weekly_report",
    hook_id: "before_finish",
    step_id: "before_finish",
    display_name: HOOK.display_name,
    description: HOOK.description,
    contract_version: 1,
    hook_semantic_digest: "digest",
    contract: HOOK,
    binding_revision: bound ? 1 : 0,
    binding: bound
      ? { revision_id: "r1", target_workflow_id: "client_fix", input_mapping: { customer: "customer" }, output_mapping: { customer: "customer" } }
      : null,
  });
  const fallback = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    const url = stringFromUnknownValue(input);
    if (url === "/api/projects/project_alpha/workflows") return json({ project_id: "project_alpha", workflow_ids: ["weekly_report", "client_fix", "client_other"] });
    if (url.endsWith("/hooks")) return json({ project_id: "project_alpha", workflow_id: "weekly_report", hooks: [hookRow()] });
    if (url.endsWith("/hooks/before_finish")) return json(hookRow());
    if (url.endsWith("/hooks/before_finish/create-and-bind")) {
      bound = true;
      return json({ project_id: "project_alpha", workflow_id: "weekly_report", hook_id: "before_finish", binding_revision: 1, revision_id: "r1", target_workflow_id: "client_fix" });
    }
    if (url.includes("/where-used")) return json({ project_id: "project_alpha", workflow_id: "client_fix", consumers: [{ kind: "hook", caller_workflow_id: "weekly_report", hook_id: "before_finish", step_id: "before_finish", caller_display_name: "Weekly Report", hook_display_name: "Before the order is finished" }] });
    if (new URL(url, "http://localhost").pathname.endsWith("/reactions") && (!init?.method || init.method === "GET")) {
      const requestUrl = new URL(url, "http://localhost");
      return json({
        workflow_id: requestUrl.pathname.split("/")[3] || "", project_id: "project_alpha",
        scope_kind: requestUrl.searchParams.get("scope_kind") || "workflow",
        scope_id: requestUrl.searchParams.get("scope_id") || "",
        reactions: [], available_trigger_sources: [],
      });
    }
    if (url.endsWith("/changes") && init?.method === "POST") {
      const body = JSON.parse(String(init.body || "{}")) as { dry_run?: boolean; expected_revision?: number };
      const targetWorkflowId = url.includes("client_fix") ? "client_fix" : "weekly_report";
      if (body.dry_run === true) return json({
        workflow_id: targetWorkflowId, revision: body.expected_revision,
        valid: true, applied: false, issues: [], blocking_issues: [], workflow: {},
      });
      changeUrls.push(url);
      return json({ workflow_id: targetWorkflowId, revision: 4, valid: true, issues: [] });
    }
    return fallback ? fallback(input, init) : json({});
  });
}

/** A second client scope must never replace an unapplied first hook draft. */
function addSecondHook({ displayName = "After the order is finished" }: { displayName?: string } = {}) {
  const detail = mockedFetchWorkflowDetail.getMockImplementation()!;
  const canvas = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  const originalFetch = fetchSpy.getMockImplementation()!;
  const nextHook = { ...HOOK, hook_id: "after_finish", display_name: displayName };
  mockedFetchWorkflowDetail.mockImplementation(async (id: string) => {
    if (id === "client_other") {
      const child = await detail("client_fix");
      return { ...child, workflow_id: id, display_name: "Other client actions" } as never;
    }
    const row = await detail(id);
    if (id !== "weekly_report") return row;
    return { ...row, steps: [...(row.steps || []), {
      step_id: "after_finish", display_name: nextHook.display_name, step_kind: "invoke_workflow",
      override: { invoke_workflow: { hook: nextHook } },
    }] } as never;
  });
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (id: string) => {
    if (id === "client_other") {
      const graph = await canvas("client_fix");
      return { ...graph, workflow: { ...graph.workflow, id, display_name: "Other client actions" },
        steps: graph.steps.map((step) => ({ ...step, display_name: `Other: ${step.display_name}` })),
      };
    }
    const graph = await canvas(id);
    if (id !== "weekly_report") return graph;
    return { ...graph, steps: [...graph.steps, {
      id: "after_finish", step_id: "after_finish", kind: "invoke_workflow", display_name: nextHook.display_name,
      config_summary: "hook", ports: NEXT,
    }] };
  });
  const hookRow = {
    project_id: "project_alpha", workflow_id: "weekly_report", hook_id: "after_finish", step_id: "after_finish",
    display_name: nextHook.display_name, contract_version: 1, hook_semantic_digest: "other-digest", contract: nextHook,
    binding_revision: 1, binding: { revision_id: "other-r1", target_workflow_id: "client_other", input_mapping: { customer: "customer" }, output_mapping: { customer: "customer" } },
  };
  fetchSpy.mockImplementation(async (input, init) => {
    const url = stringFromUnknownValue(input);
    if (url.endsWith("/hooks/after_finish")) return json(hookRow);
    const response = await originalFetch(input, init);
    if (!url.endsWith("/hooks")) return response;
    const body = await response.json();
    return json({ ...body, hooks: [...body.hooks, hookRow] });
  });
}

async function choosePaletteOption(name: string) {
  const palette = await screen.findByRole("dialog", { name: "Add step" });
  fireEvent.click(within(palette).getByRole("option", { name }));
}

async function stageHookStep() {
  fireEvent.click(within(frame()).getByRole("button", { name: "Add step to Before the order is finished" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "Add step to Before the order is finished" })).getByRole("button", { name: "StepA step in this hook." }));
  await choosePaletteOption("Set variable");
  fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Pending client action" } });
  expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("set_variable");
  fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
  await screen.findByRole("group", { name: "Open step Pending client action" });
}

async function renderHookedBuilder() {
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  return screen.findByRole("group", { name: "Open step Fix the customer name" }, { timeout: 5_000 });
}

function frame() {
  return screen.getByText((_, element) => element?.getAttribute("data-hook-frame") === "before_finish");
}


function addNestedClientContainers() {
  const detail = mockedFetchWorkflowDetail.getMockImplementation()!;
  const canvas = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  mockedFetchWorkflowDetail.mockImplementation(async (workflowId: string) => {
    const current = await detail(workflowId);
    if (workflowId !== "client_fix") return current;
    return {
      ...current,
      groups: [{ group_id: "checks", display_name: "Client checks", body: { kind: "sequence", entry: { kind: "for_each", id: "items" } } }],
      for_each: [{ for_each_id: "items", group_id: "checks", display_name: "Client items", over: "state.items", item_name: "item", body: { kind: "sequence", entry: { kind: "step", id: "fix_name" } } }],
      steps: (current.steps || []).map((step) => step.step_id === "fix_name" ? { ...step, for_each_id: "items" } : step),
      graph: { schema_version: 2, entry: { kind: "group", id: "checks" }, edges: [
        { source: { kind: "step", id: "fix_name" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "items", lane_id: null } },
        { source: { kind: "for_each", id: "items" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "checks", lane_id: null } },
        { source: { kind: "group", id: "checks" }, outcome: { name: "success" }, target: { kind: "step", id: "return" } },
      ] },
    } as never;
  });
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (workflowId: string) => {
    const current = await canvas(workflowId);
    if (workflowId !== "client_fix") return current;
    return {
      ...current,
      entry: { kind: "group", id: "checks" },
      groups: [{ id: "checks", group_id: "checks", display_name: "Client checks", body: { kind: "sequence", entry: { kind: "for_each", id: "items" } } }],
      for_each: [{ id: "items", for_each_id: "items", group_id: "checks", display_name: "Client items", over: "state.items", item_name: "item", body: { kind: "sequence", entry: { kind: "step", id: "fix_name" } } }],
      steps: current.steps.map((step) => step.id === "fix_name" ? { ...step, for_each_id: "items" } : step),
      edges: [
        { source: { kind: "step", id: "fix_name" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "items", lane_id: null } },
        { source: { kind: "for_each", id: "items" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "checks", lane_id: null } },
        { source: { kind: "group", id: "checks" }, outcome: { name: "success" }, target: { kind: "step", id: "return" } },
      ],
    } as never;
  });
}

it.each([false, true])("refreshes selected-project Hook-of membership after successful binding Save from linked=%s", async (initiallyLinked) => {
  installHookedWorkflow();
  let linked = initiallyLinked; let membershipReads = 0; let whereUsedReads = 0;
  fetchSpy.mockImplementation(async (input) => {
    const path = new URL(String(input), "http://localhost").pathname;
    if (path === "/api/projects/project_alpha/workflows") {
      membershipReads += 1;
      return json({ project_id: "project_alpha", workflow_ids: linked ? ["client_fix"] : [] });
    }
    if (path === "/api/projects/project_alpha/workflows/client_fix/where-used") {
      whereUsedReads += 1;
      return json({ project_id: "project_alpha", workflow_id: "client_fix", consumers: [{ kind: "hook", hook_id: "before_finish", caller_workflow_id: "weekly_report" }] });
    }
    throw new Error(`Unexpected request ${path}`);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><MemoryRouter><WorkflowHookOfLinks projectId="project_alpha" workflowId="client_fix" /></MemoryRouter></QueryClientProvider>);
  try {
    await waitFor(() => expect(membershipReads).toBe(1));
    if (initiallyLinked) {
      expect(await screen.findByRole("navigation", { name: "Hook of" })).toBeInTheDocument();
      expect(whereUsedReads).toBe(1);
    } else {
      await act(async () => { await Promise.resolve(); });
      expect(screen.queryByRole("navigation", { name: "Hook of" })).not.toBeInTheDocument();
      expect(whereUsedReads).toBe(0);
    }
    linked = !initiallyLinked;
    // The existing B04 successful Save invalidates this public prefix for the same client.
    await act(async () => { await client.invalidateQueries({ queryKey: ["workflows", "client_fix", "linked-project-id"] }); });
    await waitFor(() => expect(membershipReads).toBe(2));
    if (initiallyLinked) {
      await waitFor(() => expect(screen.queryByRole("navigation", { name: "Hook of" })).not.toBeInTheDocument());
      expect(whereUsedReads).toBe(1);
    } else {
      expect(await screen.findByRole("navigation", { name: "Hook of" })).toBeInTheDocument();
      expect(whereUsedReads).toBe(1);
    }
  } finally { view.unmount(); client.clear(); }
});

it("keeps unfinished connector mapping values inside their owning hook when child step ids match", async () => {
  installHookedWorkflow();
  const detail = mockedFetchWorkflowDetail.getMockImplementation()!;
  const canvas = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  mockedFetchWorkflowDetail.mockImplementation(async (id: string) => {
    const current = await detail(id);
    if (id !== "client_fix") return current;
    return { ...current, steps: (current.steps || []).map((step: Record<string, unknown>) => step.step_id === "fix_name" ? {
      ...step, step_kind: "connector", connector_id: "github",
      override: { connector: { function_name: "lookup_issue", connection_binding: { connection_id: "conn_1" } } },
      input_mapping_jsonb: {}, output_mapping_jsonb: {},
    } : step) } as never;
  });
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (id: string) => {
    const graph = await canvas(id);
    return id === "client_fix" ? { ...graph, steps: graph.steps.map((step) => step.id === "fix_name" ? { ...step, kind: "connector" } : step) } : graph;
  });
  addSecondHook();
  const fallback = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    if (stringFromUnknownValue(input).includes("/meta/connector-catalog")) return json({ connectors: [{
      connector_id: "github", display_name: "GitHub", source: "registry_tool", tool_id: "github",
      operations: [{ operation_id: "lookup_issue", display_name: "Look up issue", parameters: [{ name: "query", type: "string", required: false }], returns: { name: "issue", type: "json", required: false } }],
      connections: [{ connection_id: "conn_1", display_name: "Primary GitHub" }],
    }] });
    return fallback(input, init);
  });
  await renderHookedBuilder();
  async function openChild(hookId: string) {
    const card = await screen.findByText((_, element) => element?.getAttribute("data-workflow-step-measure-id") === `hook:${hookId}:fix_name`);
    const matchingFrames = screen.getAllByRole("group", { name: /^Open step / }).filter((candidate) =>
      within(candidate).queryByText((_, element) => element === card) === card);
    expect(matchingFrames).toHaveLength(1);
    const [frame] = matchingFrames;
    fireEvent.contextMenu(frame);
    await userEvent.click(screen.getByRole("menuitem", { name: "Open" }));
    await screen.findByDisplayValue(`hook:${hookId}:fix_name`);
  }

  // Load both client definitions before switching: a loading remount must not
  // accidentally hide the collision between their identical bare step ids.
  await openChild("after_finish");
  expect(await screen.findByRole("combobox", { name: "Parameter 1" })).toHaveValue("");
  await openChild("before_finish");
  const firstValue = await screen.findByRole("textbox", { name: "Parameter value 1" });
  fireEvent.change(firstValue, { target: { value: "state.before_finish_only" } });
  expect(firstValue).toHaveValue("state.before_finish_only");
  expect(screen.getByRole("combobox", { name: "Parameter 1" })).toHaveValue("");
  expect(changeUrls).toEqual([]);

  await openChild("after_finish");
  expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue("Fix the customer name");
  const secondValue = await screen.findByRole("textbox", { name: "Parameter value 1" });
  expect(secondValue).toHaveValue("");
  fireEvent.change(secondValue, { target: { value: "state.after_finish_only" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Parameter 1" }), { target: { value: "query" } });
  fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(changeUrls).toHaveLength(1));
  expect(changeUrls[0]).toContain("/api/workflows/client_other/changes");
  expect(postedChangeSets()[0]).toMatchObject({ expected_revision: 3, operations: [{ op: "update_step_config", step_id: "fix_name", patch: { input_mapping_jsonb: { query: "state.after_finish_only" } } }] });
  expect(JSON.stringify(postedChangeSets())).not.toContain("before_finish_only");
});

it("keeps the requested hook child selection while its detail is deferred and saves only its own mapping", async () => {
  installHookedWorkflow();
  const detail = mockedFetchWorkflowDetail.getMockImplementation()!;
  const canvas = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  mockedFetchWorkflowDetail.mockImplementation(async (id: string) => {
    const current = await detail(id);
    if (id !== "client_fix") return current;
    return { ...current, steps: (current.steps || []).map((step: Record<string, unknown>) => step.step_id === "fix_name" ? {
      ...step, step_kind: "connector", connector_id: "github",
      override: { connector: { function_name: "lookup_issue", connection_binding: { connection_id: "conn_1" } } },
      input_mapping_jsonb: {}, output_mapping_jsonb: {},
    } : step) } as never;
  });
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (id: string) => {
    const graph = await canvas(id);
    return id === "client_fix" ? { ...graph, steps: graph.steps.map((step) => step.id === "fix_name" ? { ...step, kind: "connector" } : step) } : graph;
  });
  addSecondHook();
  const fallback = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    if (stringFromUnknownValue(input).includes("/meta/connector-catalog")) return json({ connectors: [{
      connector_id: "github", display_name: "GitHub", source: "registry_tool", tool_id: "github",
      operations: [{ operation_id: "lookup_issue", display_name: "Look up issue", parameters: [{ name: "query", type: "string", required: false }], returns: { name: "issue", type: "json", required: false } }],
      connections: [{ connection_id: "conn_1", display_name: "Primary GitHub" }],
    }] });
    return fallback(input, init);
  });
  const detailWithBothHooks = mockedFetchWorkflowDetail.getMockImplementation()!;
  let releaseOtherDetail!: () => void;
  const otherDetailReady = new Promise<void>((resolve) => { releaseOtherDetail = resolve; });
  mockedFetchWorkflowDetail.mockImplementation(async (id: string) => {
    if (id === "client_other") await otherDetailReady;
    return detailWithBothHooks(id);
  });
  const user = userEvent.setup({ skipHover: true });
  await renderHookedBuilder();
  async function openChild(hookId: string) {
    const card = await screen.findByText((_, element) => element?.getAttribute("data-workflow-step-measure-id") === `hook:${hookId}:fix_name`);
    const matchingFrames = screen.getAllByRole("group", { name: /^Open step / }).filter((candidate) =>
      within(candidate).queryByText((_, element) => element === card) === card);
    expect(matchingFrames).toHaveLength(1);
    const [frame] = matchingFrames;
    // Right-click › Open is the step's editor, for a hook member as for any step.
    fireEvent.contextMenu(frame);
    await user.click(screen.getByRole("menuitem", { name: "Open" }));
    await screen.findByDisplayValue(`hook:${hookId}:fix_name`);
  }

  const otherCard = await screen.findByText((_, element) => element?.getAttribute("data-workflow-step-measure-id") === "hook:after_finish:fix_name");
  const matchingFrames = screen.getAllByRole("group", { name: /^Open step / }).filter((candidate) =>
    within(candidate).queryByText((_, element) => element === otherCard) === otherCard);
  expect(matchingFrames).toHaveLength(1);
  const [otherFrame] = matchingFrames;
  fireEvent.contextMenu(otherFrame);
  await user.click(screen.getByRole("menuitem", { name: "Open" }));
  await waitFor(() => expect(mockedFetchWorkflowDetail).toHaveBeenCalledWith("client_other"));
  expect(screen.queryByDisplayValue("hook:before_finish:fix_name")).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue("fix_name")).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Step label" })).not.toBeInTheDocument();
  expect(changeUrls).toEqual([]);
  await act(async () => {
    releaseOtherDetail();
    await otherDetailReady;
  });
  expect(await screen.findByDisplayValue("hook:after_finish:fix_name")).toBeInTheDocument();
  expect(await screen.findByRole("combobox", { name: "Parameter 1" })).toHaveValue("");
  await openChild("before_finish");
  const firstValue = await screen.findByRole("textbox", { name: "Parameter value 1" });
  fireEvent.change(firstValue, { target: { value: "state.before_finish_only" } });
  expect(firstValue).toHaveValue("state.before_finish_only");
  expect(screen.getByRole("combobox", { name: "Parameter 1" })).toHaveValue("");
  expect(changeUrls).toEqual([]);

  await openChild("after_finish");
  expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue("Fix the customer name");
  const secondValue = await screen.findByRole("textbox", { name: "Parameter value 1" });
  expect(secondValue).toHaveValue("");
  fireEvent.change(secondValue, { target: { value: "state.after_finish_only" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Parameter 1" }), { target: { value: "query" } });
  fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(changeUrls).toHaveLength(1));
  expect(changeUrls[0]).toContain("/api/workflows/client_other/changes");
  expect(postedChangeSets()[0]).toMatchObject({ expected_revision: 3, operations: [{ op: "update_step_config", step_id: "fix_name", patch: { input_mapping_jsonb: { query: "state.after_finish_only" } } }] });
  expect(JSON.stringify(postedChangeSets())).not.toContain("before_finish_only");
});

it("keeps whole-workflow child marks from the selected run's own execution snapshot through the real shell and surface", async () => {
  installHookedWorkflow();
  mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", interface: { enabled: true, expected_inputs: [] }, definition_issues: [] } as never);
  mockedTestRunWorkflow.mockResolvedValue({
    job_id: "parent-marks", workflow_id: "weekly_report", project_id: "project_alpha",
    status: "completed", final_status: "success",
  } as never);
  const fallback = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(stringFromUnknownValue(input), "http://localhost");
    if (url.pathname === "/api/v2/jobs/parent-marks") return json({
      id: "parent-marks", project_id: "project_alpha", selected_workflow: "weekly_report", status: "completed",
      job: { id: "parent-marks", project_id: "project_alpha", selected_workflow: "weekly_report", status: "completed" },
    });
    return fallback(input, init);
  });
  await renderHookedBuilder();
  fireEvent.click(screen.getByRole("link", { name: "Test Run" }));
  fireEvent.change(await screen.findByLabelText("Test Run Summary"), { target: { value: "Verify hook result marks" } });
  const run = await screen.findByRole("button", { name: "Run workflow test run" });
  await waitFor(() => expect(run).toBeEnabled());
  fireEvent.click(run);
  await screen.findByRole("dialog", { name: "Test Run results" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Start a test run" })).not.toBeInTheDocument());
  const childNode = await screen.findByRole("group", { name: "Open step Fix the customer name" }, { timeout: 5_000 });
  // A finished pass without a recorded time says Done on the card's top edge.
  expect(await within(childNode).findByTestId("workflow-runtime-mark")).toHaveTextContent("Done");
  expect(within(childNode).getByTestId("workflow-runtime-mark")).toHaveAttribute("data-run", "done");
  expect(within(childNode).getByTestId("workflow-runtime-mark")).toHaveAttribute("data-job-id", "child-marks");
  expect(within(screen.getByRole("group", { name: "Open step Prepare the order" })).queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
});

it("marks the real hook child canvas when an isolated hook Test Run starts through its shell and panel", async () => {
  installHookedWorkflow();
  mockedTestRunWorkflow.mockResolvedValue({ job_id: "child-marks", status: "completed", final_status: "success" } as never);
  await renderHookedBuilder();
  within(frame()).getAllByRole("button", { name: /Before the order is finished/ })[0].focus();
  await userEvent.keyboard("{Enter}");
  const drawer = within(screen.getByRole("dialog", { name: "Step editor" }));
  await userEvent.click(await drawer.findByRole("tab", { name: "Test Run" }));
  const run = await drawer.findByRole("button", { name: "Run hook test run" });
  await waitFor(() => expect(run).toBeEnabled());
  await userEvent.click(run);
  await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({ workflowId: "client_fix", projectId: "project_alpha" })));
  const childNode = screen.getByRole("group", { name: "Open step Fix the customer name" });
  // A finished pass without a recorded time says Done on the card's top edge.
  expect(await within(childNode).findByTestId("workflow-runtime-mark")).toHaveTextContent("Done");
  expect(within(childNode).getByTestId("workflow-runtime-mark")).toHaveAttribute("data-run", "done");
  expect(within(childNode).getByTestId("workflow-runtime-mark")).toHaveAttribute("data-job-id", "child-marks");
  expect(within(screen.getByRole("group", { name: "Open step Prepare the order" })).queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
});

it.each([
  ["group", "checks", "Client checks", "Save group", "update_group_config"],
  ["for_each", "items", "Client items", "Save For each", "update_for_each_config"],
])("edits a child %s inside the hook with the child's revision and persisted id", async (kind, id, name, saveLabel, op) => {
  installHookedWorkflow();
  addNestedClientContainers();
  await renderHookedBuilder();
  const container = await screen.findByText((_, element) => element?.getAttribute(kind === "group" ? "data-group-id" : "data-for-each-id") === `hook:before_finish:${id}`);
  within(container).getAllByRole("button", { name: new RegExp(name) })[0].focus();
  await userEvent.keyboard("{Enter}");
  const drawer = await screen.findByRole("dialog", { name: kind === "group" ? "Group editor" : "For each editor" });
  const editor = within(within(drawer).getByText((_, element) => element?.getAttribute("id") === "group-editor-surface"));
  // Save lives in the drawer's sticky footer, outside the editor body.
  const save = await within(drawer).findByRole("button", { name: saveLabel });
  const label = editor.getByRole("textbox", { name: "Display name" });
  expect(label).toHaveValue(name);
  fireEvent.change(label, { target: { value: `${name} updated` } });
  fireEvent.click(save);
  await waitFor(() => expect(changeUrls).toHaveLength(1));
  expect(changeUrls[0]).toContain("/api/workflows/client_fix/changes");
  expect(postedChangeSets()[0]).toMatchObject({ expected_revision: 3, operations: [{ op, [kind === "group" ? "group_id" : "for_each_id"]: id, patch: { display_name: `${name} updated` } }] });
});

it("adds a step from a nested child For each without flattening its placement into the author", async () => {
  installHookedWorkflow();
  addNestedClientContainers();
  await renderHookedBuilder();
  const container = await screen.findByText((_, element) => element?.getAttribute("data-for-each-id") === "hook:before_finish:items");
  fireEvent.click(within(container).getByRole("button", { name: "Add step to Client items" }));
  await choosePaletteOption("Set variable");
  fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Process item" } });
  expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("set_variable");
  fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
  fireEvent.click(await screen.findByRole("button", { name: /Apply changes/ }));
  await waitFor(() => expect(postedChangeSets()).not.toHaveLength(0));
  expect(changeUrls.at(-1)).toContain("/api/workflows/client_fix/changes");
  expect(postedChangeSets().at(-1)).toMatchObject({ expected_revision: 3, operations: [{ op: "create_step", step: { step_id: "set_variable", group_id: null, for_each_id: "items" } }] });
  expect(changeUrls.some((url) => url.includes("/api/workflows/weekly_report/changes"))).toBe(false);
});

it("creates a Group from the hook's ordinary Add menu in the child change set", async () => {
  installHookedWorkflow();
  await renderHookedBuilder();
  fireEvent.click(within(frame()).getByRole("button", { name: "Add step to Before the order is finished" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "Add step to Before the order is finished" })).getByRole("button", { name: "GroupSteps that belong together. Can become a child workflow." }));
  const drawer = await screen.findByRole("dialog", { name: "Group editor" });
  const editor = within(within(drawer).getByText((_, element) => element?.getAttribute("id") === "group-editor-surface"));
  const save = await within(drawer).findByRole("button", { name: "Save group" });
  fireEvent.change(editor.getByRole("textbox", { name: "Display name" }), { target: { value: "Client processing" } });
  fireEvent.click(save);
  fireEvent.click(await screen.findByRole("button", { name: /Apply changes/ }));
  await waitFor(() => expect(postedChangeSets()).not.toHaveLength(0));
  expect(changeUrls.at(-1)).toContain("/api/workflows/client_fix/changes");
  expect(postedChangeSets().at(-1)).toMatchObject({ expected_revision: 3, operations: [{ op: "create_group", group: { group_id: "group", display_name: "Group" } }, { op: "update_group_config", group_id: "group", patch: { display_name: "Client processing" } }] });
});

describe("a hook on the builder canvas (B14)", () => {
  it("is drawn by the Group container in the hook tone, holding the connected workflow's steps", async () => {
    installHookedWorkflow();
    await renderHookedBuilder();

    const container = frame();
    expect(container).toHaveAttribute("data-group-id", "before_finish");
    expect(within(container).getByText("Hook")).toBeInTheDocument();
    expect(within(container).getByText("Before the order is finished")).toBeInTheDocument();
    expect(within(container).getByRole("button", { name: "Add step to Before the order is finished" })).toBeInTheDocument();
    // The client's steps are members of the frame; the hook step itself is not a plain node any more.
    expect(screen.getByRole("group", { name: "Open step Fix the customer name" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Open step Before the order is finished" })).not.toBeInTheDocument();
    // Author steps stay locked; the client's are editable.
    await userEvent.hover(screen.getByRole("group", { name: "Open step Prepare the order" }));
    expect(screen.queryByRole("button", { name: "More actions for Prepare the order" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Edit step Prepare the order" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("group", { name: "Open step Prepare the order" }));
    expect(await screen.findByTestId("protected-step-view")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
    expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue("Fix the customer name");
  });

  it("edits a member in the ordinary step drawer and saves it to the hook's workflow", async () => {
    installHookedWorkflow();
    await renderHookedBuilder();

    fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
    const label = await screen.findByRole("textbox", { name: "Step label" });
    expect(label).toHaveValue("Fix the customer name");
    expect(screen.queryByTestId("protected-step-view")).not.toBeInTheDocument();

    fireEvent.change(label, { target: { value: "Fix the name properly" } });
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    await waitFor(() => expect(changeUrls).toHaveLength(1));
    expect(changeUrls[0]).toContain("/api/workflows/client_fix/changes");
    expect(postedChangeSets()[0]).toMatchObject({
      expected_revision: 3,
      operations: [{ op: "update_step_config", step_id: "fix_name" }],
    });
  });

  it("adds a step inside the frame to the hook's workflow and applies it there", async () => {
    installHookedWorkflow();
    await renderHookedBuilder();

    fireEvent.click(within(frame()).getByRole("button", { name: "Add step to Before the order is finished" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Add step to Before the order is finished" })).getByRole("button", { name: "StepA step in this hook." }));
    await choosePaletteOption("Set variable");
    fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Notify sales" } });
    expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("set_variable");
    fireEvent.click(screen.getByRole("button", { name: "Update new step" }));

    const apply = await screen.findByRole("button", { name: /Apply changes/ });
    await waitFor(() => expect(apply).toBeEnabled());
    fireEvent.click(apply);

    await waitFor(() => expect(changeUrls.filter((url) => !url.includes("dry"))).not.toHaveLength(0));
    const applied = postedChangeSets().at(-1) || {};
    expect(changeUrls.at(-1)).toContain("/api/workflows/client_fix/changes");
    expect(applied).toMatchObject({ expected_revision: 3 });
    // Added at the bottom, it goes last: before the hook's finishing step, in the hook's own ids (R3-6).
    expect(applied.operations).toEqual([
      { op: "insert_step_between", source: { kind: "step", id: "fix_name" }, outcome: { name: "success" }, step: expect.objectContaining({ step_id: "set_variable", step_kind: "set_variable", group_id: null }) },
    ]);
    expect(changeUrls.some((url) => url.includes("/api/workflows/weekly_report/changes"))).toBe(false);
  });

  it("deletes a member from the hook's workflow, and keeps the author's steps locked", async () => {
    installHookedWorkflow();
    await renderHookedBuilder();

    fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
    await screen.findByRole("textbox", { name: "Step label" });
    fireEvent.click(screen.getByRole("button", { name: "Delete step" }));
    fireEvent.click(await screen.findByRole("button", { name: /Apply changes/ }));

    await waitFor(() => expect(postedChangeSets()).not.toHaveLength(0));
    expect(changeUrls.at(-1)).toContain("/api/workflows/client_fix/changes");
    // The removed first step hands the start of the hook to what came after it (R3-2).
    expect(postedChangeSets().at(-1)?.operations).toEqual([
      { op: "delete_step", step_id: "fix_name" },
      { op: "set_entry", target: { kind: "step", id: "return" } },
    ]);

    // An author step opens read-only, with nothing to delete or save.
    window.editStep?.("prepare");
    expect(await screen.findByTestId("protected-step-view")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete step" })).not.toBeInTheDocument();
    expect(changeUrls.some((url) => url.includes("/api/workflows/weekly_report/changes"))).toBe(false);
  });

  it("prepares the hook's place for steps on the first Add of an empty hook, naming no workflow", async () => {
    installHookedWorkflow({ connected: false });
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
    await waitFor(() => expect(within(frame()).getByText("Empty")).toBeInTheDocument());
    expect(screen.queryByText(/Connected:/)).not.toBeInTheDocument();

    fireEvent.click(within(frame()).getByRole("button", { name: "Add step to Before the order is finished" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Add step to Before the order is finished" })).getByRole("button", { name: "StepA step in this hook." }));
    await choosePaletteOption("Set variable");

    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([input]) => stringFromUnknownValue(input).endsWith("/hooks/before_finish/create-and-bind"))).toBe(true),
    );
    const body = JSON.parse(String(fetchSpy.mock.calls.find(([input]) => stringFromUnknownValue(input).endsWith("/create-and-bind"))?.[1]?.body || "{}"));
    expect(body).toMatchObject({ expected_revision: 0, child_workflow_id: "weekly_report_before_finish" });
    expect(await screen.findByRole("textbox", { name: "Step label" })).toBeInTheDocument();
  });

  it("opens the hook drawer from the frame header with Smart setup, TEST RUN and Automation", async () => {
    installHookedWorkflow();
    await renderHookedBuilder();

    // The header is the Group header: Enter (or a click) opens its drawer.
    fireEvent.keyDown(within(frame()).getAllByRole("button", { name: /Before the order is finished/ })[0], { key: "Enter" });

    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    expect(await within(drawer).findByRole("tab", { name: "Smart setup" })).toBeInTheDocument();
    expect(within(drawer).getByRole("tab", { name: "Test Run" })).toBeInTheDocument();
    expect(within(drawer).getByRole("tab", { name: "Automation" })).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("tab", { name: "Test Run" }));
    expect(await within(drawer).findByRole("button", { name: "Run hook test run" })).toBeInTheDocument();
  });

  it("runs a member's step Test Run against the hook's workflow", async () => {
    installHookedWorkflow();
    mockedTestRunWorkflowStep.mockResolvedValue({ status: "completed", final_status: "success" } as never);
    await renderHookedBuilder();

    fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
    await screen.findByRole("textbox", { name: "Step label" });
    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
    const run = await screen.findByRole("button", { name: "Run step test run" });
    await waitFor(() => expect(run).toBeEnabled());
    fireEvent.click(run);

    await waitFor(() => expect(mockedTestRunWorkflowStep).toHaveBeenCalled());
    expect(mockedTestRunWorkflowStep.mock.calls[0][0]).toMatchObject({ workflowId: "client_fix", stepId: "fix_name" });
  });

  it("draws a new hook as an empty frame and starts it with the first step, which ends the hook", async () => {
    installHookedWorkflow({ storage: "empty" });
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
    // R2-11: no Build the result / Return the result / done nobody added; the hidden end is no card.
    await waitFor(() => expect(within(frame()).getByText("Empty")).toBeInTheDocument());
    expect(screen.queryByRole("group", { name: /Open step (Return the result|hook_end)/ })).not.toBeInTheDocument();

    await stageHookStep();
    expect(within(frame()).queryByText("Empty")).not.toBeInTheDocument();
    const apply = await screen.findByRole("button", { name: /Apply changes/ });
    await waitFor(() => expect(apply).toBeEnabled());
    fireEvent.click(apply);

    await waitFor(() => expect(changeUrls).not.toHaveLength(0));
    expect(changeUrls.at(-1)).toContain("/api/workflows/client_fix/changes");
    const stepId = (postedChangeSets().at(-1)?.operations as Array<{ op: string; step?: { step_id: string } }>)[0].step?.step_id;
    expect(postedChangeSets().at(-1)?.operations).toEqual([
      { op: "create_step", step: expect.objectContaining({ step_kind: "set_variable", group_id: null }) },
      { op: "set_entry", target: { kind: "step", id: stepId } },
      { op: "connect", edge: { source: { kind: "step", id: stepId }, outcome: { name: "success" }, target: { kind: "step", id: "hook_end" } } },
    ]);
    expect(fetchSpy.mock.calls.some(([input]) => stringFromUnknownValue(input).endsWith("/create-and-bind"))).toBe(false);
  });

  it("clears the hook at once with a confirmation, and a new step never brings the old ones back", async () => {
    installHookedWorkflow({ storage: "one-step" });
    await renderHookedBuilder();
    fireEvent.keyDown(within(frame()).getAllByRole("button", { name: /Before the order is finished/ })[0], { key: "Enter" });
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    // No workflow selector, mapping, Connect or Disconnect: the hook explains itself.
    expect(within(drawer).queryByRole("combobox", { name: "Workflow to run here" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: /^(Connect|Disconnect|Check|Create and connect)$/ })).not.toBeInTheDocument();
    expect(await within(drawer).findByText("1 step")).toBeInTheDocument();

    fireEvent.click(within(drawer).getByRole("button", { name: "Clear the hook" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Clear the hook?" })).getByRole("button", { name: "Clear the hook" }));
    // Gone at once, not after a refetch.
    await waitFor(() => expect(screen.queryByRole("group", { name: "Open step Fix the customer name" })).not.toBeInTheDocument());
    expect(within(frame()).getByText("Empty")).toBeInTheDocument();

    await stageHookStep();
    expect(screen.queryByRole("group", { name: "Open step Fix the customer name" })).not.toBeInTheDocument();
    const apply = await screen.findByRole("button", { name: /Apply changes/ });
    await waitFor(() => expect(apply).toBeEnabled());
    fireEvent.click(apply);
    await waitFor(() => expect(changeUrls).not.toHaveLength(0));
    expect(changeUrls.at(-1)).toContain("/api/workflows/client_fix/changes");
    const operations = postedChangeSets().at(-1)?.operations as Array<Record<string, unknown>>;
    expect(operations).toContainEqual({ op: "delete_step", step_id: "fix_name" });
    expect(operations.filter((operation) => operation.op === "create_step")).toHaveLength(1);
    expect(operations.at(-1)).toMatchObject({ op: "connect", edge: { target: { kind: "step", id: "hook_end" } } });
    // The same place keeps the steps: no other workflow is created or bound.
    expect(fetchSpy.mock.calls.some(([input]) => stringFromUnknownValue(input).endsWith("/create-and-bind"))).toBe(false);
  });

  it("without a project, disables a hook's Add step with the reason and asks no project API", async () => {
    installHookedWorkflow();
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("");
    mockedFetchWorkflowLinkedProjects.mockResolvedValue([] as never);
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder" });
    await screen.findByRole("group", { name: "Open step Prepare the order" });

    const picker = await screen.findByTestId("hook-project-picker");
    expect(picker).toHaveTextContent("Link this workflow to a project to connect its hooks.");
    expect(within(picker).getByRole("link", { name: "Link a project" })).toHaveAttribute("href", "/registry/workflows/weekly_report/projects");
    expect(within(frame()).getByRole("button", { name: "Add step to Before the order is finished" })).toBeDisabled();
    expect(within(frame()).getByText("Choose a project in the header first: each project keeps its own steps in a hook.")).toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([input]) => stringFromUnknownValue(input).includes("/api/projects//"))).toBe(false);
  });

  it("offers the linked projects in the builder header and opens the hooks of the chosen one", async () => {
    installHookedWorkflow();
    mockedFetchWorkflowLinkedProjects.mockResolvedValue([{ id: "project_alpha", name: "Alpha project" }, { id: "project_beta", name: "Beta project" }] as never);
    const { router } = renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
    await screen.findByRole("group", { name: "Open step Fix the customer name" }, { timeout: 5_000 });
    const select = within(await screen.findByTestId("hook-project-picker")).getByRole("combobox", { name: "Project for hooks" });
    expect(select).toHaveTextContent("Alpha project");
    fireEvent.click(select);
    fireEvent.click(await screen.findByRole("option", { name: "Beta project" }));
    await waitFor(() => expect(router.state.location.search).toContain("project_id=project_beta"));
  });

  it("links the connected workflow's builder back to the hook it serves", async () => {
    installHookedWorkflow();
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/client_fix/builder?project_id=project_alpha" });

    const back = await screen.findByTestId("hook-of-links");
    expect(await within(back).findByRole("link", { name: "Weekly Report · Before the order is finished" })).toHaveAttribute(
      "href",
      "/registry/workflows/weekly_report/builder?project_id=project_alpha&drawer=step&step_id=before_finish",
    );
  });
});


it("blocks an isolated hook Test Run while its child has unapplied changes", async () => {
  installHookedWorkflow();
  await renderHookedBuilder();
  await stageHookStep();
  fireEvent.keyDown(within(frame()).getAllByRole("button", { name: /Before the order is finished/ })[0], { key: "Enter" });
  const drawer = await screen.findByRole("dialog", { name: "Step editor" });
  fireEvent.click(within(drawer).getByRole("tab", { name: "Test Run" }));
  expect(await within(drawer).findByRole("button", { name: "Run hook test run" })).toBeDisabled();
  expect(within(drawer).getByText("Apply or discard the structure changes before running a test.")).toBeInTheDocument();
});

it.each(["header", "Add step"])("keeps an unapplied hook draft when another hook is opened through %s", async (command) => {
  installHookedWorkflow();
  addSecondHook();
  await renderHookedBuilder();
  await screen.findByRole("group", { name: "Open step Other: Fix the customer name" }, { timeout: 5_000 });
  await stageHookStep();
  const other = screen.getByText((_, element) => element?.getAttribute("data-hook-frame") === "after_finish");
  if (command === "header") {
    fireEvent.keyDown(within(other).getAllByRole("button", { name: /After the order is finished/ })[0], { key: "Enter" });
  } else {
    fireEvent.click(within(other).getByRole("button", { name: "Add step to After the order is finished" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Add step to After the order is finished" })).getByRole("button", { name: "StepA step in this hook." }));
    await choosePaletteOption("Set variable");
  }
  expect(await screen.findByText("Apply or discard the changes in hook Before the order is finished first.")).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Open step Pending client action" })).toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
  expect(changeUrls).toEqual([]);
});

it.each([
  ["group", "checks", "Client checks", "Delete group", "Delete group and its steps", "delete_group"],
  ["for_each", "items", "Client items", "Delete For each", "Delete For each and its steps", "delete_for_each"],
])("deletes a nested child %s through the child change set", async (kind, id, name, command, confirmation, op) => {
  installHookedWorkflow(); addNestedClientContainers(); await renderHookedBuilder();
  const container = await screen.findByText((_, element) => element?.getAttribute(kind === "group" ? "data-group-id" : "data-for-each-id") === `hook:before_finish:${id}`);
  within(container).getAllByRole("button", { name: new RegExp(name) })[0].focus();
  await userEvent.keyboard("{Enter}");
  const drawer = await screen.findByRole("dialog", { name: kind === "group" ? "Group editor" : "For each editor" });
  // Delete sits in the drawer's sticky footer, outside the editor body.
  fireEvent.click(await within(drawer).findByRole("button", { name: command }));
  fireEvent.click(await screen.findByRole("button", { name: /Apply changes/ }));
  await waitFor(() => expect(postedChangeSets()).not.toHaveLength(0));
  expect(changeUrls.at(-1)).toContain("/api/workflows/client_fix/changes");
  expect(postedChangeSets().at(-1)).toMatchObject({ expected_revision: 3, operations: [{ op, [kind === "group" ? "group_id" : "for_each_id"]: id, with_contents: true }] });
});

it("moves a nested child For each to the child's root while retaining its owning workflow", async () => {
  installHookedWorkflow(); addNestedClientContainers(); await renderHookedBuilder();
  const container = await screen.findByText((_, element) => element?.getAttribute("data-for-each-id") === "hook:before_finish:items");
  within(container).getAllByRole("button", { name: /Client items/ })[0].focus();
  await userEvent.keyboard("{Enter}");
  const drawer = await screen.findByRole("dialog", { name: "For each editor" });
  const editor = within(within(drawer).getByText((_, element) => element?.getAttribute("id") === "group-editor-surface"));
  fireEvent.change(await editor.findByRole("combobox", { name: "Placement" }), { target: { value: "" } });
  fireEvent.click(within(await screen.findByRole("dialog", { name: "Move this For each?" })).getByRole("button", { name: "Move For each" }));
  fireEvent.click(await screen.findByRole("button", { name: /Apply changes/ }));
  await waitFor(() => expect(postedChangeSets()).not.toHaveLength(0));
  expect(changeUrls.at(-1)).toContain("/api/workflows/client_fix/changes");
  expect(postedChangeSets().at(-1)).toMatchObject({ operations: [{ op: "move_for_each", for_each_id: "items", group_id: null }] });
});

it("cancels an empty hook's first Add before creating or switching the client workflow", async () => {
  installHookedWorkflow(); addSecondHook();
  const previous = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    const response = await previous(input, init);
    const url = stringFromUnknownValue(input);
    if (!url.endsWith("/hooks") && !url.endsWith("/hooks/after_finish")) return response;
    const body = await response.json();
    const empty = (row: Record<string, unknown>) => row.hook_id === "after_finish" ? { ...row, binding: null, binding_revision: 0 } : row;
    return json(Array.isArray(body.hooks) ? { ...body, hooks: body.hooks.map(empty) } : empty(body));
  });
  await renderHookedBuilder();
  fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
  const label = await screen.findByRole("textbox", { name: "Step label" });
  fireEvent.change(label, { target: { value: "Still editing this client" } });
  const other = screen.getByText((_, element) => element?.getAttribute("data-hook-frame") === "after_finish");
  fireEvent.click(within(other).getByRole("button", { name: "Add step to After the order is finished" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "Add step to After the order is finished" })).getByRole("button", { name: "StepA step in this hook." }));
  await choosePaletteOption("Set variable");
  fireEvent.click(within(await screen.findByRole("dialog", { name: "Discard unsaved parameters?" })).getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Still editing this client");
  expect(fetchSpy.mock.calls.some(([input]) => stringFromUnknownValue(input).endsWith("/create-and-bind"))).toBe(false);
  expect(changeUrls).toEqual([]);
});


// DEVHOOK03: real page, palette, controller and renderer. These assertions cover
// local state only; real persistence/reload remains native root acceptance.
function setDeclarationSession(mode?: string) {
  mockedUseAuth.mockReturnValue({ session: { authenticated: true, roles: ["admin"], user: { roles: ["admin"] }, environment: { edition: "community", ...(mode === undefined ? {} : { mode }) } } } as ReturnType<typeof mockedUseAuth>);
}
function installPersonalDeclarationAuthor() {
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ enabled: false, steps: [] }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ enabled: false }));
  mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "weekly_report", extension_owned: false, protected: false, structure_change_allowed: true } as never);
}
async function openDeclarationPalette() {
  fireEvent.click(await screen.findByRole("button", { name: "Add step" }, { timeout: 5_000 }));
  return within(await screen.findByRole("dialog", { name: "Add step" }));
}

it.each([undefined, "production", "unknown", "local_development"])("does not offer a new declaration for server mode %s", async (mode) => {
  setDeclarationSession(mode); installPersonalDeclarationAuthor(); renderPage({ surface: "builder" });
  await openDeclarationPalette();
  const palette = within(screen.getByRole("dialog", { name: "Add step" }));
  expect(palette.queryByRole("option", { name: "Hook" })).not.toBeInTheDocument();
  expect(palette.getByRole("option", { name: "Invoke workflow" })).toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
});

it("stages a Development declaration as an empty frame before persistence and keeps filling unavailable until Apply", async () => {
  setDeclarationSession("development"); installPersonalDeclarationAuthor(); renderPage({ surface: "builder" });
  fireEvent.click((await openDeclarationPalette()).getByRole("option", { name: "Hook" }));
  const id = await screen.findByRole("textbox", { name: "Hook ID" });
  expect((id as HTMLInputElement).value).toMatch(/^[a-z][a-z0-9_]*$/);
  expect(screen.getByRole("textbox", { name: "Stage summary" })).toHaveValue("");
  const draftFrame = await screen.findByText((_, element) => element?.getAttribute("data-hook-frame") === (id as HTMLInputElement).value);
  expect(within(draftFrame).getByText("Not applied")).toBeInTheDocument();
  expect(within(draftFrame).getByRole("button", { name: "Add step to Hook" })).toBeDisabled();
  expect(within(draftFrame).getByText("Apply the declaration before adding its steps.")).toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
  expect(fetchSpy.mock.calls.some(([input]) => stringFromUnknownValue(input).endsWith("/create-and-bind"))).toBe(false);
});

it("keeps existing personal declaration fields editable in Production and locks its identity", async () => {
  setDeclarationSession("production"); installHookedWorkflow({ connected: false });
  const detail = mockedFetchWorkflowDetail.getMockImplementation()!;
  const canvas = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  mockedFetchWorkflowDetail.mockImplementation(async (id: string) => { const row = await detail(id); return id === "weekly_report" ? { ...row, portability: { workflow_id: id, protected: false, extension_owned: false, structure_change_allowed: true } } : row; });
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (id: string) => { const graph = await canvas(id); return id === "weekly_report" ? { ...graph, read_only: false, protected_workflow: false } : graph; });
  mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "weekly_report", protected: false, extension_owned: false, structure_change_allowed: true } as never);
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha&drawer=step&step_id=before_finish" });
  expect(await screen.findByRole("textbox", { name: "Hook name" })).toHaveValue(HOOK.display_name);
  expect(screen.getByRole("textbox", { name: "Hook ID" })).toHaveAttribute("readonly");
  fireEvent.change(screen.getByRole("textbox", { name: "Hook name" }), { target: { value: "Customer review" } });
  expect(screen.getByRole("textbox", { name: "Hook name" })).toHaveValue("Customer review");
  expect(screen.getByRole("button", { name: "Save parameters" })).toBeEnabled();
  expect(postedChangeSets()).toEqual([]);
});

it.each(["production", "development"])("keeps the protected core declaration read-only and existing child filling available in %s", async (mode) => {
  setDeclarationSession(mode); installHookedWorkflow(); await renderHookedBuilder();
  within(frame()).getAllByRole("button", { name: /Before the order is finished/ })[0].focus();
  await userEvent.keyboard("{Enter}");
  expect(await screen.findByTestId("protected-step-view")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Hook name" })).not.toBeInTheDocument();
  fireEvent.click(within(frame()).getByRole("button", { name: "Add step to Before the order is finished" }));
  // Current Hook Add composition still owns the Step/Group/For each submenu.
  fireEvent.click(within(screen.getByRole("dialog", { name: "Add step to Before the order is finished" })).getByRole("button", { name: "StepA step in this hook." }));
  const palette = within(await screen.findByRole("dialog", { name: "Add step" }));
  expect(palette.queryByRole("option", { name: "Hook" })).not.toBeInTheDocument();
  expect(palette.getByRole("option", { name: "Invoke workflow" })).toBeInTheDocument();
  fireEvent.click(palette.getByRole("option", { name: "Wait" }));
  expect(await screen.findByRole("group", { name: "Open step Wait" })).toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
});

it("refuses deleting a child Group whose nested For each step owns a saved disabled Automation rule", async () => {
  installHookedWorkflow();
  addNestedClientContainers();
  // The existing helper draws the nested graph, while this case also needs the
  // stored definition's canonical settings.execution_graph to own that closure.
  const previousDetail = mockedFetchWorkflowDetail.getMockImplementation()!;
  mockedFetchWorkflowDetail.mockImplementation(async (workflowId: string) => {
    const current = await previousDetail(workflowId);
    if (workflowId !== "client_fix") return current;
    const row = current as Record<string, unknown>;
    return { ...row, settings: { ...(row.settings as Record<string, unknown>), execution_graph: row.graph } } as never;
  });
  const previous = fetchSpy.getMockImplementation();
  const reactionRequests: URL[] = [];
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(stringFromUnknownValue(input), "http://localhost");
    if (url.pathname === "/api/workflows/client_fix/changes" && init?.method === "POST"
      && JSON.parse(String(init.body || "{}")).dry_run === true) {
      return json({ workflow_id: "client_fix", revision: 3, valid: false, applied: false, issues: [],
        blocking_issues: [{ code: "workflow.reaction.scope_missing", path: "reactions.scope::step::fix_name::protect_nested", message: "A saved rule protects step fix_name.", severity: "error" }], workflow: {} });
    }
    if (url.pathname.endsWith("/reactions") && (!init?.method || init.method === "GET")) {
      reactionRequests.push(url);
      const scopeKind = url.searchParams.get("scope_kind") || "workflow";
      const scopeId = url.searchParams.get("scope_id") || "";
      const reactions = url.pathname === "/api/workflows/client_fix/reactions"
        && scopeKind === "step" && scopeId === "fix_name"
        ? [{ reaction_key: "protect_nested", display_name: "Guard child", event_type: "job.step_failed", conditions: {}, action: { type: "notify" }, enabled: false, scope_kind: "step", scope_id: "fix_name" }]
        : [];
      const workflowId = url.pathname.split("/")[3] || "";
      return json({ workflow_id: workflowId, project_id: "project_alpha", scope_kind: scopeKind, scope_id: scopeId, reactions, available_trigger_sources: [] });
    }
    return previous ? previous(input, init) : json({});
  });
  await renderHookedBuilder();

  const [groupCard] = await screen.findAllByRole("button", { name: /^Client checks/ });
  fireEvent.click(groupCard);
  const editor = await screen.findByRole("dialog", { name: "Group editor" });
  fireEvent.click(within(editor).getByRole("button", { name: "Delete group" }));

  expect(await screen.findByText(/Guard child/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open Automation" })).toBeInTheDocument();
  expect(reactionRequests.some((url) => url.pathname === "/api/workflows/client_fix/reactions"
    && url.searchParams.get("project_id") === "project_alpha"
    && url.searchParams.get("scope_kind") === "step"
    && url.searchParams.get("scope_id") === "fix_name")).toBe(true);
  expect(reactionRequests.every((url) => url.pathname === "/api/workflows/client_fix/reactions")).toBe(true);
  expect(reactionRequests.some((url) => url.searchParams.get("scope_kind") === "for_each")).toBe(false);
  expect(screen.queryByRole("button", { name: "Apply changes" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Discard changes" })).not.toBeInTheDocument();
  expect(screen.queryByText(/^Structure not applied:/)).not.toBeInTheDocument();
  for (const body of dryRunChangeSets()) {
    expect(body.expected_revision).toBe(3);
    expect(body.dry_run).toBe(true);
    expect(body.operations).toEqual([{ op: "delete_group", group_id: "checks", with_contents: true }]);
  }
  expect(postedChangeSets()).toEqual([]);
  expect(screen.getByRole("dialog", { name: "Group editor" })).toBeInTheDocument();
});

it("offers selected child Step Delete but no protected author Delete, then drops the old selection on hook switch", async () => {
  const user = userEvent.setup();
  installHookedWorkflow();
  addSecondHook();
  await renderHookedBuilder();
  await screen.findByRole("group", { name: "Open step Other: Fix the customer name" }, { timeout: 5_000 });

  fireEvent.click(screen.getByRole("group", { name: "Open step Prepare the order" }));
  expect(await screen.findByTestId("protected-step-view")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "More actions for Prepare the order" })).not.toBeInTheDocument();

  const childCard = screen.getByRole("group", { name: "Open step Fix the customer name" });
  fireEvent.click(childCard);
  const editor = await screen.findByRole("dialog", { name: "Step editor" });
  expect(await within(editor).findByRole("textbox", { name: "Step label" })).toHaveValue("Fix the customer name");
  fireEvent.click(within(editor).getByRole("button", { name: "Close" }));
  await user.hover(childCard);
  fireEvent.click(await screen.findByRole("button", { name: "More actions for Fix the customer name" }));
  expect(screen.getByRole("menuitem", { name: "Delete" })).toBeInTheDocument();

  const other = screen.getByText((_, element) => element?.getAttribute("data-hook-frame") === "after_finish");
  fireEvent.keyDown(within(other).getAllByRole("button", { name: /After the order is finished/ })[0], { key: "Enter" });
  await waitFor(() => expect(within(screen.getByTestId("protected-step-view")).getByText("After the order is finished")).toBeInTheDocument());
  expect(screen.queryByRole("menuitem", { name: "Delete" })).not.toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
  expect(changeUrls).toEqual([]);
});

it("drops a held child Step guard after switching to another Hook without staging either owner", async () => {
  installHookedWorkflow();
  addSecondHook();
  const previous = fetchSpy.getMockImplementation();
  const scopedGets: URL[] = [];
  let releaseChildGet: ((response: Response) => void) | undefined;
  const heldChildGet = new Promise<Response>((resolve) => { releaseChildGet = resolve; });
  let parsedChildGet: (() => void) | undefined;
  const parsed = new Promise<void>((resolve) => { parsedChildGet = resolve; });
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(stringFromUnknownValue(input), "http://localhost");
    if (url.pathname === "/api/workflows/client_fix/reactions" && (!init?.method || init.method === "GET")) {
      scopedGets.push(url);
      return heldChildGet;
    }
    return previous ? previous(input, init) : json({});
  });
  await renderHookedBuilder();
  fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
  const editor = await screen.findByRole("dialog", { name: "Step editor" });
  expect(await within(editor).findByRole("textbox", { name: "Step label" })).toHaveValue("Fix the customer name");
  fireEvent.click(within(editor).getByRole("button", { name: "Delete step" }));
  await waitFor(() => expect(scopedGets.some((url) => url.searchParams.get("scope_kind") === "step"
    && url.searchParams.get("scope_id") === "fix_name" && url.searchParams.get("project_id") === "project_alpha")).toBe(true));
  expect(postedChangeSets()).toEqual([]);
  expect(changeUrls).toEqual([]);
  fireEvent.click(within(editor).getByRole("button", { name: "Close" }));
  const other = screen.getByText((_, element) => element?.getAttribute("data-hook-frame") === "after_finish");
  fireEvent.keyDown(within(other).getAllByRole("button", { name: /After the order is finished/ })[0], { key: "Enter" });
  await waitFor(() => expect(within(screen.getByTestId("protected-step-view")).getByText("After the order is finished")).toBeInTheDocument());
  const response = json({ workflow_id: "client_fix", project_id: "project_alpha", scope_kind: "step",
    scope_id: "fix_name", reactions: [], available_trigger_sources: [] });
  const originalJson = response.json.bind(response);
  response.json = async () => { const body = await originalJson(); parsedChildGet?.(); return body; };
  await act(async () => { releaseChildGet?.(response); await parsed; });
  expect(postedChangeSets()).toEqual([]);
  expect(changeUrls).toEqual([]);
  fireEvent.keyDown(within(frame()).getAllByRole("button", { name: /Before the order is finished/ })[0], { key: "Enter" });
  await waitFor(() => expect(within(within(screen.getByTestId("protected-step-view")).getByTestId("hook-binding-editor")).getByText("Before the order is finished")).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "Apply changes" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Discard changes" })).not.toBeInTheDocument();
  expect(screen.queryByText(/^Structure not applied:/)).not.toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Open step Fix the customer name" })).toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
  expect(changeUrls).toEqual([]);
});

/** Saved-rule and change-set reads for the mounted command cases; no durable writes. */
function allowCanvasCommandDryRuns() {
  const previous = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(stringFromUnknownValue(input), "http://localhost");
    if (url.pathname.endsWith("/reactions") && (!init?.method || init.method === "GET")) {
      return json({
        workflow_id: url.pathname.includes("client_fix") ? "client_fix" : "weekly_report",
        project_id: "project_alpha", scope_kind: url.searchParams.get("scope_kind") || "workflow",
        scope_id: url.searchParams.get("scope_id") || "", reactions: [], available_trigger_sources: [],
      });
    }
    if (url.pathname.endsWith("/changes") && init?.method === "POST") {
      const body = JSON.parse(String(init.body || "{}")) as { dry_run?: boolean; expected_revision?: number };
      if (body.dry_run) return json({
        workflow_id: url.pathname.includes("client_fix") ? "client_fix" : "weekly_report",
        revision: body.expected_revision, applied: false, valid: true,
        issues: [], blocking_issues: [], workflow: {},
      });
    }
    return previous(input, init);
  });
}

/** React Flow's focusable wrapper of a step card (it has no unique accessible name). */
function reactFlowNodeFor(name: string) {
  const node = screen.getAllByRole("group").find((candidate) =>
    candidate.getAttribute("aria-roledescription") === "node" &&
    within(candidate).queryByRole("group", { name: `Open step ${name}` }));
  if (!node) throw new Error(`Missing React Flow node for ${name}`);
  return node;
}

it("undoes and redoes the last child Delete from an empty canvas even when the author is editable", async () => {
  installHookedWorkflow({ authorEditable: true });
  allowCanvasCommandDryRuns();
  await renderHookedBuilder();
  fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
  const drawer = await screen.findByRole("dialog", { name: "Step editor" });
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  // One selected node shows no selection bar; Delete on the canvas acts on it at once.
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  fireEvent.keyDown(reactFlowNodeFor("Fix the customer name"), { key: "Delete", code: "Delete" });
  await waitFor(() => expect(screen.queryByRole("group", { name: "Open step Fix the customer name" })).not.toBeInTheDocument());
  expect(await screen.findByRole("button", { name: "Undo" })).toBeInTheDocument();
  const canvas = screen.getByRole("region", { name: "Workflow canvas" });
  canvas.focus();
  expect(canvas).toHaveFocus();
  fireEvent.keyDown(canvas, { key: "z", code: "KeyZ", ctrlKey: true });
  expect(await screen.findByRole("group", { name: "Open step Fix the customer name" })).toBeInTheDocument();
  fireEvent.keyDown(canvas, { key: "z", code: "KeyZ", ctrlKey: true, shiftKey: true });
  await waitFor(() => expect(screen.queryByRole("group", { name: "Open step Fix the customer name" })).not.toBeInTheDocument());
  expect(postedChangeSets()).toEqual([]);
});

it("keeps an inactive connected child out of empty-selection Undo for the editable author", async () => {
  installHookedWorkflow({ authorEditable: true });
  allowCanvasCommandDryRuns();
  await renderHookedBuilder();
  fireEvent.click(screen.getByRole("group", { name: "Open step Prepare the order" }));
  fireEvent.click(within(await screen.findByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
  fireEvent.keyDown(reactFlowNodeFor("Prepare the order"), { key: "Delete", code: "Delete" });
  await waitFor(() => expect(screen.queryByRole("group", { name: "Open step Prepare the order" })).not.toBeInTheDocument());
  const canvas = screen.getByRole("region", { name: "Workflow canvas" });
  canvas.focus();
  expect(canvas).toHaveFocus();
  fireEvent.keyDown(canvas, { key: "z", code: "KeyZ", ctrlKey: true });
  expect(await screen.findByRole("group", { name: "Open step Prepare the order" })).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Open step Fix the customer name" })).toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
});


describe("contextual Play of a declared hook's existing child step", () => {
  function materializationWrites() {
    return fetchSpy.mock.calls.filter(([input, init]) => stringFromUnknownValue(input).endsWith("/create-and-bind") && init?.method === "POST");
  }

  it("opens the bound child isolated tab without materializing or running, then submits its real child scope only on Run", async () => {
    installHookedWorkflow();
    mockedTestRunWorkflowStep.mockResolvedValue({ workflow_id: "client_fix", step_id: "fix_name", step_kind: "set_variable", status: "completed", final_status: "success" } as never);
    const user = userEvent.setup({ skipHover: true });
    await renderHookedBuilder();
    const card = await screen.findByRole("group", { name: "Open step Fix the customer name" }, { timeout: 5_000 });
    await user.hover(card);
    const play = await within(card).findByRole("button", { name: "Test step" });
    // user-event 14.6.1 omits relatedTarget; preserve the real card-to-child move.
    fireEvent.mouseOut(card, { relatedTarget: play });
    fireEvent.mouseOver(play, { relatedTarget: card });
    expect(play).toBeInTheDocument();
    expect(play).toBeVisible();
    await user.click(play);
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    expect(within(drawer).getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
    // The drawer header names the opened child step by its ID (title + subtitle, no inset card).
    expect(within(drawer).getByText("fix_name", { exact: true })).toBeVisible();
    expect(within(drawer).queryByRole("button", { name: "Run hook test run" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument();
    expect(materializationWrites()).toEqual([]);
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(changeUrls).toEqual([]);
    fireEvent.change(within(drawer).getByRole("textbox", { name: "Step Test Run Summary" }), { target: { value: "Verify only the client name correction." } });
    const run = within(drawer).getByRole("button", { name: "Run step test run" });
    await waitFor(() => expect(run).toBeEnabled());
    await user.click(run);
    await waitFor(() => expect(mockedTestRunWorkflowStep).toHaveBeenCalledTimes(1));
    expect(mockedTestRunWorkflowStep).toHaveBeenCalledWith({ workflowId: "client_fix", expectedRevision: 3, stepId: "fix_name", projectId: "project_alpha", taskSummary: "Verify only the client name correction.", inputs: {}, inputBindings: [] });
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(materializationWrites()).toEqual([]);
    expect(changeUrls).toEqual([]);
    const author = screen.getByRole("group", { name: "Open step Prepare the order" });
    await user.hover(author);
    const authorPlay = within(author).queryByRole("button", { name: "Test step" });
    if (authorPlay) expect(authorPlay).toBeDisabled();
  });

  it("keeps the first child draft and scope when a different hook member's Play navigation is refused", async () => {
    installHookedWorkflow();
    addSecondHook();
    const user = userEvent.setup({ skipHover: true });
    const { router } = renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
    await screen.findByRole("group", { name: "Open step Fix the customer name" }, { timeout: 5_000 });
    const first = await screen.findByRole("group", { name: "Open step Fix the customer name" }, { timeout: 5_000 });
    await user.hover(first);
    const firstPlay = await within(first).findByRole("button", { name: "Test step" });
    fireEvent.mouseOut(first, { relatedTarget: firstPlay });
    fireEvent.mouseOver(firstPlay, { relatedTarget: first });
    expect(firstPlay).toBeInTheDocument();
    expect(firstPlay).toBeVisible();
    await user.click(firstPlay);
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    expect(within(drawer).getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
    await user.click(within(drawer).getByRole("tab", { name: "Smart setup" }));
    const label = await within(drawer).findByRole("textbox", { name: "Step label" });
    fireEvent.change(label, { target: { value: "First hook unfinished draft" } });
    const before = router.state.location;
    const other = await screen.findByRole("group", { name: "Open step Other: Fix the customer name" }, { timeout: 5_000 });
    await user.hover(other);
    const otherPlay = await within(other).findByRole("button", { name: "Test step" });
    fireEvent.mouseOut(other, { relatedTarget: otherPlay });
    fireEvent.mouseOver(otherPlay, { relatedTarget: other });
    expect(otherPlay).toBeInTheDocument();
    expect(otherPlay).toBeVisible();
    await user.click(otherPlay);
    const confirmation = await screen.findByRole("dialog", { name: "Discard unsaved parameters?" });
    await user.click(within(confirmation).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard unsaved parameters?" })).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Step label" })).toBe(label);
    expect(label).toHaveValue("First hook unfinished draft");
    expect(screen.getByRole("tab", { name: "Smart setup" })).toHaveAttribute("aria-selected", "true");
    expect(router.state.location).toEqual(before);
    expect(otherPlay).toHaveFocus();
    expect(materializationWrites()).toEqual([]);
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(changeUrls).toEqual([]);
    expect(postedChangeSets()).toEqual([]);
  });
});


async function insertLocalHookWaitForDiscard() {
  installHookedWorkflow();
  await renderHookedBuilder();
  // The protected parent has no insert controls; this fixture has exactly one
  // ordinary child edge, fix_name -> return, and uses the actual edge action.
  const insert = screen.getAllByRole("button", { name: "Insert a step here" });
  expect(insert).toHaveLength(1);
  fireEvent.click(insert[0]);
  // Add step is named by where it inserts (Canvas › Adding): this connection, in words.
  const palette = await screen.findByRole("dialog", { name: "Insert on Fix the customer name · Next → Return the result" });
  fireEvent.click(within(palette).getByRole("option", { name: "Wait" }));
  await screen.findByRole("group", { name: "Open step Wait" });
  await screen.findByRole("button", { name: "Update new step" });
  await waitFor(() => expect(dryRunChangeSets()).toEqual(expect.arrayContaining([expect.objectContaining({
    operations: expect.arrayContaining([expect.objectContaining({ op: "insert_step_between", source: { kind: "step", id: "fix_name" }, outcome: { name: "success" }, step: expect.objectContaining({ step_id: "wait", step_kind: "wait" }) })]),
  })])));
  expect(screen.getAllByRole("button", { name: "Insert a step here" })).toHaveLength(2);
  expect(postedChangeSets()).toEqual([]);
  expect(changeUrls).toEqual([]);
  expect(fetchSpy.mock.calls.some(([input, init]) => stringFromUnknownValue(input).endsWith("/weekly_report/changes") && init?.method === "POST")).toBe(false);
}

it("retires a discarded local Hook step editor instead of reopening its stale fields as a new root form", async () => {
  await insertLocalHookWaitForDiscard();
  expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Wait");
  fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
  await waitFor(() => expect(screen.queryByRole("group", { name: "Open step Wait" })).not.toBeInTheDocument());
  expect(screen.queryByRole("heading", { name: "Step editor" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Add step to changes" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Update new step" })).not.toBeInTheDocument();
  expect(screen.queryByRole("toolbar", { name: "Selected items" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Discard changes" })).not.toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Insert a step here" })).toHaveLength(1);
  expect(screen.getByRole("group", { name: "Open step Fix the customer name" })).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Open step Return the result" })).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Open step Prepare the order" })).toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
  expect(changeUrls).toEqual([]);
  expect(fetchSpy.mock.calls.some(([input, init]) => stringFromUnknownValue(input).endsWith("/weekly_report/changes") && init?.method === "POST")).toBe(false);
});

it("preserves the persisted child editor when Discard removes a different local Hook step", async () => {
  await insertLocalHookWaitForDiscard();
  fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Fix the customer name"));
  expect(screen.getByRole("button", { name: "Save parameters" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
  await waitFor(() => expect(screen.queryByRole("group", { name: "Open step Wait" })).not.toBeInTheDocument());
  expect(screen.getByRole("heading", { name: "Step editor" })).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Fix the customer name");
  expect(screen.getByRole("button", { name: "Save parameters" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Add step to changes" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Discard changes" })).not.toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
  expect(changeUrls).toEqual([]);
});

it.each(["url", "record"])("does not scan project bindings when the Builder already has %s context", async (context) => {
  installHookedWorkflow();
  if (context === "record") {
    const previous = mockedFetchWorkflowDetail.getMockImplementation()!;
    mockedFetchWorkflowDetail.mockImplementation(async (id) => ({ ...await previous(id), project_id: "project_alpha" }) as never);
  }
  renderPage({ surface: "builder", initialEntry: `/registry/workflows/weekly_report/builder${context === "url" ? "?project_id=project_alpha" : ""}` });
  await screen.findByRole("group", { name: "Open step Fix the customer name" }, { timeout: 5_000 });
  expect(mockedFindWorkflowLinkedProjectId).not.toHaveBeenCalled();
});

it.each([403, 404, "not-linked"] as const)("validates only the selected project and does not reuse another project's membership on %s", async (denial) => {
  const requested: string[] = [];
  mockedFetchWorkflowLinkedProjects.mockResolvedValue([{ id: "p-a" }, { id: "p-b" }] as never);
  fetchSpy.mockImplementation(async (input) => {
    const path = new URL(String(input), "http://localhost").pathname; requested.push(path);
    if (path === "/api/projects/p-a/workflows") return json({ project_id: "p-a", workflow_ids: ["client_fix"] });
    if (path === "/api/projects/p-b/workflows") return denial === "not-linked"
      ? json({ project_id: "p-b", workflow_ids: ["other"] })
      : new Response(JSON.stringify({ error: "Selected project unavailable" }), { status: denial, headers: { "content-type": "application/json" } });
    if (path === "/api/projects/p-a/workflows/client_fix/where-used") return json({ project_id: "p-a", workflow_id: "client_fix", consumers: [] });
    throw new Error(`Unexpected request ${path}`);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (projectId: string) => <QueryClientProvider client={client}><MemoryRouter><WorkflowHookOfLinks projectId={projectId} workflowId="client_fix" /></MemoryRouter></QueryClientProvider>;
  const view = render(content("p-a"));
  try {
    await waitFor(() => expect(requested).toContain("/api/projects/p-a/workflows/client_fix/where-used"));
    expect(requested.filter((path) => path === "/api/projects/p-a/workflows")).toHaveLength(1);
    view.rerender(content("p-b"));
    await waitFor(() => expect(requested).toContain("/api/projects/p-b/workflows"));
    await act(async () => { await Promise.resolve(); });
    expect(requested.filter((path) => path === "/api/projects/p-b/workflows")).toHaveLength(1);
    expect(requested).not.toContain("/api/projects/p-b/workflows/client_fix/where-used");
    expect(requested.some((path) => path.startsWith("/api/v2/projects") || path.startsWith("/api/workflows/"))).toBe(false);
    expect(mockedFetchWorkflowLinkedProjects).not.toHaveBeenCalled();
  } finally { view.unmount(); client.clear(); }
});

it("cancels hook clearing above the inspector and stages one undoable clear only after confirmation", async () => {
  const user = userEvent.setup();
  installHookedWorkflow({ storage: "one-step" });
  allowCanvasCommandDryRuns();
  await renderHookedBuilder();
  fireEvent.keyDown(within(frame()).getAllByRole("button", { name: /Before the order is finished/ })[0], { key: "Enter" });
  const inspector = await screen.findByRole("dialog", { name: "Step editor" });
  const clear = await within(inspector).findByRole("button", { name: "Clear the hook" });
  for (const action of ["Cancel", "Escape"]) {
    await user.click(clear);
    const confirmation = await screen.findByRole("dialog", { name: "Clear the hook?" });
    await waitFor(() => expect(within(confirmation).getByRole("button", { name: "Cancel" })).toHaveFocus());
    expect(screen.getByRole("group", { name: "Open step Fix the customer name" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply changes" })).not.toBeInTheDocument();
    if (action === "Cancel") await user.click(within(confirmation).getByRole("button", { name: "Cancel" }));
    else await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Clear the hook?" })).not.toBeInTheDocument();
    expect(inspector).toBeInTheDocument();
    await waitFor(() => expect(clear).toHaveFocus());
    expect(screen.getByRole("group", { name: "Open step Fix the customer name" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply changes" })).not.toBeInTheDocument();
    expect(postedChangeSets()).toEqual([]);
  }
  await user.click(clear);
  await user.click(within(await screen.findByRole("dialog", { name: "Clear the hook?" })).getByRole("button", { name: "Clear the hook" }));
  await waitFor(() => expect(screen.queryByRole("group", { name: "Open step Fix the customer name" })).not.toBeInTheDocument());
  expect(within(frame()).getByText("Empty")).toBeInTheDocument();
  expect(inspector).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Apply changes" })).toBeInTheDocument();
  screen.getByRole("region", { name: "Workflow canvas" }).focus();
  await user.keyboard("{Control>}z{/Control}");
  expect(await screen.findByRole("group", { name: "Open step Fix the customer name" })).toBeInTheDocument();
  expect(within(frame()).queryByText("Empty")).not.toBeInTheDocument();
  // Exactly one staged batch: a single Undo returns to no pending graph history.
  expect(screen.queryByRole("button", { name: "Apply changes" })).not.toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
  expect(changeUrls).toEqual([]);
});


it("binds real Hook-frame child input and result choices to the selected child contract and current parent mapping", async () => {
  installHookedWorkflow();
  const detail = mockedFetchWorkflowDetail.getMockImplementation()!;
  mockedFetchWorkflowDetail.mockImplementation(async (id: string) => {
    const row = await detail(id);
    if (id !== "client_fix") return row;
    return { ...row, steps: (row.steps || []).map((step) => step.step_id === "fix_name"
      ? { ...step, step_kind: "transform", override: { transform: { input: { source: "state.legacy" }, result_target: "state.legacy_result", fields: [] } } }
      : step) } as never;
  });
  const canvas = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (id: string) => {
    const graph = await canvas(id);
    return id === "client_fix" ? { ...graph, steps: graph.steps.map((step) => step.step_id === "fix_name"
      ? { ...step, kind: "transform" } : step) } : graph;
  });
  mockedFetchWorkflowContract.mockImplementation(async (id: string) => ({ workflow_id: id, interface: id === "client_fix"
    ? { enabled: true, input_mode: "fields", expected_inputs: [{ name: "client_name", label: "Customer name", type: "string" }],
      expected_outputs: [{ name: "display_name", label: "Corrected customer", type: "string" }, { name: "spare", label: "Spare result", type: "string" }] }
    : { enabled: true, input_mode: "fields", expected_inputs: [{ name: "parent_secret", label: "Parent secret", type: "string" }], expected_outputs: [] },
  definition_issues: [] }) as never);
  const original = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    const url = stringFromUnknownValue(input);
    if (url.includes("/api/workflows/client_fix/available-variables")) return json({ state: [], dotted_path: [], prior_outputs: [], loop: [], position: "before" });
    if (url.endsWith("/hooks") || url.endsWith("/hooks/before_finish")) {
      const response = await original(input, init);
      const row = await response.json();
      const bound = (hook: Record<string, unknown>) => ({ ...hook, binding: { ...(hook.binding as Record<string, unknown>),
        input_mapping: { client_name: "customer" }, output_mapping: { display_name: "customer" } } });
      return json(url.endsWith("/hooks") ? { ...row, hooks: row.hooks.map(bound) } : bound(row));
    }
    return original(input, init);
  });
  const user = userEvent.setup();
  await renderHookedBuilder();
  fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
  const drawer = await screen.findByRole("dialog", { name: "Step editor" });
  const source = await within(drawer).findByRole("combobox", { name: "Transform source" });
  await user.clear(source);
  await user.type(source, "Customer name");
  const sourceOptions = screen.getByTestId(source.getAttribute("aria-controls")!);
  const supplied = within(sourceOptions).getByRole("option", { name: /Customer name/ });
  expect(supplied).toHaveTextContent("state.inputs.client_name");
  expect(supplied).toHaveTextContent("Hook input customer");
  expect(sourceOptions).not.toHaveTextContent("Parent secret");
  fireEvent.mouseDown(supplied);
  const target = within(drawer).getByRole("combobox", { name: "Transform result target" });
  await user.click(target);
  const targetOptions = screen.getByTestId(target.getAttribute("aria-controls")!);
  const returned = within(targetOptions).getByRole("option", { name: /Corrected customer/ });
  expect(returned).toHaveTextContent("state.outputs.display_name");
  expect(returned).toHaveTextContent("state.hooks.before_finish.output.customer");
  expect(within(targetOptions).getByRole("option", { name: /Spare result/ })).toHaveTextContent("Child result only");
  fireEvent.mouseDown(returned);
  fireEvent.click(within(drawer).getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  expect(changeUrls).toEqual(["/api/workflows/client_fix/changes"]);
  expect(postedChangeSets()[0]).toMatchObject({ operations: [{ patch: { override: { transform: {
    input: { source: "state.inputs.client_name" }, result_target: "state.outputs.display_name",
  } } } }] });
  expect(fetchSpy.mock.calls.some(([url]) => stringFromUnknownValue(url).includes("/api/workflows/weekly_report/available-variables"))).toBe(false);
});

it("keeps child choices but claims no parent mapping when Hook detail has a stale binding revision", async () => {
  installHookedWorkflow();
  const detail = mockedFetchWorkflowDetail.getMockImplementation()!;
  mockedFetchWorkflowDetail.mockImplementation(async (id: string) => {
    const row = await detail(id);
    return id === "client_fix" ? { ...row, steps: (row.steps || []).map((step) => step.step_id === "fix_name"
      ? { ...step, step_kind: "transform", override: { transform: { input: { source: "" }, result_target: "", fields: [] } } }
      : step) } as never : row;
  });
  const canvas = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (id: string) => {
    const graph = await canvas(id);
    return id === "client_fix" ? { ...graph, steps: graph.steps.map((step) => step.step_id === "fix_name"
      ? { ...step, kind: "transform" } : step) } : graph;
  });
  mockedFetchWorkflowContract.mockImplementation(async (id: string) => ({ workflow_id: id, interface: id === "client_fix"
    ? { enabled: true, input_mode: "fields", expected_inputs: [{ name: "client_name", label: "Customer name", type: "string" }],
      expected_outputs: [{ name: "display_name", label: "Corrected customer", type: "string" }] }
    : { enabled: true, input_mode: "fields", expected_inputs: [{ name: "parent_secret", label: "Parent secret", type: "string" }], expected_outputs: [] },
  definition_issues: [] }) as never);
  const original = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    const url = stringFromUnknownValue(input);
    if (url.includes("/api/workflows/client_fix/available-variables")) return json({ state: [], dotted_path: [], prior_outputs: [], loop: [], position: "before" });
    if (url.endsWith("/hooks/before_finish")) {
      const row = await (await original(input, init)).json();
      return json({ ...row, binding_revision: 99, binding: { ...row.binding,
        input_mapping: { client_name: "customer" }, output_mapping: { display_name: "customer" } } });
    }
    return original(input, init);
  });
  const user = userEvent.setup();
  await renderHookedBuilder();
  fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
  const drawer = await screen.findByRole("dialog", { name: "Step editor" });
  const source = await within(drawer).findByRole("combobox", { name: "Transform source" });
  await user.type(source, "Customer name");
  const sourceOption = within(screen.getByTestId(source.getAttribute("aria-controls")!)).getByRole("option", { name: /Customer name/ });
  await waitFor(() => expect(fetchSpy.mock.calls.some(([url]) => stringFromUnknownValue(url).endsWith("/hooks/before_finish"))).toBe(true));
  expect(sourceOption).toHaveTextContent("state.inputs.client_name");
  expect(sourceOption).not.toHaveTextContent("Hook input customer");
  const target = within(drawer).getByRole("combobox", { name: "Transform result target" });
  await user.click(target);
  const outputOption = within(screen.getByTestId(target.getAttribute("aria-controls")!)).getByRole("option", { name: /Corrected customer/ });
  expect(outputOption).toHaveTextContent("state.outputs.display_name");
  expect(outputOption).not.toHaveTextContent("state.hooks.before_finish.output.customer");
  expect(within(screen.getByTestId(source.getAttribute("aria-controls")!)).queryByText("Parent secret")).not.toBeInTheDocument();
});


it("opens a parent workflow diagnostic in the protected parent settings while its Hook frame is visible", async () => {
  installHookedWorkflow();
  const originalCanvas = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (id: string) => {
    const graph = await originalCanvas(id);
    return id === "weekly_report" ? { ...graph, validation_issues: [{
      scope: "workflow", scope_id: "workflow", authored_id: "workflow", severity: "error",
      code: "workflow.terminal_fail_intent_missing", title: "Declare expected failure intent",
      message: "Declare expected failure intent", field_path: "workflow.settings.traits.expected_failure",
      suggested_action: "Open workflow settings and declare an expected failure.",
      suggested_action_key: "react.workflows.diagnostic_action_expected_failure",
    }] } as never : graph;
  });
  await renderHookedBuilder();
  expect(frame()).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^Diagnostics/ }));
  const panel = await screen.findByRole("region", { name: "Validation issues" });
  fireEvent.click(within(panel).getByRole("button", { name: /Declare expected failure intent/ }));
  const settings = await screen.findByRole("dialog", { name: "Workflow settings" });
  expect(within(settings).getByLabelText("Display name")).toHaveValue("Weekly Report");
  expect(within(settings).getByRole("checkbox", { name: /Declare an expected failure/ })).toBeDisabled();
  expect(changeUrls).toEqual([]);
});


it("withdraws only parent Hook claims after a failed cached detail refetch and restores them on recovery", async () => {
  installHookedWorkflow();
  const detail = mockedFetchWorkflowDetail.getMockImplementation()!;
  mockedFetchWorkflowDetail.mockImplementation(async (id: string) => {
    const row = await detail(id);
    return id === "client_fix" ? { ...row, steps: (row.steps || []).map((step) => step.step_id === "fix_name"
      ? { ...step, step_kind: "transform", override: { transform: { input: { source: "" }, result_target: "", fields: [] } } }
      : step) } as never : row;
  });
  const canvas = mockedFetchWorkflowCanvasGraph.getMockImplementation()!;
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (id: string) => {
    const graph = await canvas(id);
    return id === "client_fix" ? { ...graph, steps: graph.steps.map((step) => step.step_id === "fix_name"
      ? { ...step, kind: "transform" } : step) } : graph;
  });
  mockedFetchWorkflowContract.mockImplementation(async (id: string) => ({ workflow_id: id, interface: id === "client_fix"
    ? { enabled: true, input_mode: "fields", expected_inputs: [{ name: "client_name", label: "Customer name", type: "string" }],
      expected_outputs: [{ name: "display_name", label: "Corrected customer", type: "string" }] }
    : { enabled: true, input_mode: "fields", expected_inputs: [], expected_outputs: [] },
  definition_issues: [] }) as never);
  let failDetail = false;
  let detailRequests = 0;
  const originalFetch = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    const url = stringFromUnknownValue(input);
    if (url.includes("/api/workflows/client_fix/available-variables"))
      return json({ state: [], dotted_path: [], prior_outputs: [], loop: [], position: "before" });
    if (url.endsWith("/hooks/before_finish")) {
      detailRequests += 1;
      if (failDetail) return json({ detail: "Hook detail unavailable" }, 503);
      const row = await (await originalFetch(input, init)).json();
      return json({ ...row, binding: { ...row.binding,
        input_mapping: { client_name: "customer" }, output_mapping: { display_name: "customer" } } });
    }
    return originalFetch(input, init);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const key = ["workflows", "hook", "project_alpha", "weekly_report", "before_finish"];
  const router = createMemoryRouter([{ path: "/registry/workflows/:workflowId/builder",
    element: <WorkflowsDetailPage embedded surface="builder" /> }],
  { initialEntries: ["/registry/workflows/weekly_report/builder?project_id=project_alpha"] });
  const view = render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  try {
    await screen.findByRole("group", { name: "Open step Fix the customer name" }, { timeout: 5_000 });
    fireEvent.click(screen.getByRole("group", { name: "Open step Fix the customer name" }));
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    const source = await within(drawer).findByRole("combobox", { name: "Transform source" });
    const user = userEvent.setup();
    await user.type(source, "Customer name");
    const sourceOptions = screen.getByTestId(source.getAttribute("aria-controls")!);
    const childInput = within(sourceOptions).getByRole("option", { name: /Customer name/ });
    await waitFor(() => expect(childInput).toHaveTextContent("Hook input customer"));
    expect(childInput).toHaveTextContent("state.inputs.client_name");
    fireEvent.mouseDown(childInput);
    const target = within(drawer).getByRole("combobox", { name: "Transform result target" });
    await user.click(target);
    const targetOptions = screen.getByTestId(target.getAttribute("aria-controls")!);
    const childOutput = within(targetOptions).getByRole("option", { name: /Corrected customer/ });
    expect(childOutput).toHaveTextContent("state.outputs.display_name");
    await waitFor(() => expect(childOutput).toHaveTextContent("state.hooks.before_finish.output.customer"));
    expect(detailRequests).toBe(1);

    failDetail = true;
    await act(async () => { await client.refetchQueries({ queryKey: key, type: "active" }); });
    expect(detailRequests).toBe(2);
    expect(client.getQueryState(key)?.status).toBe("error");
    expect(client.getQueryData(key)).toBeDefined(); // A failed refetch retains the successful payload.
    await waitFor(() => {
      const outputDuringFailure = within(screen.getByTestId(target.getAttribute("aria-controls")!))
        .getByRole("option", { name: /Corrected customer/ });
      expect(outputDuringFailure).toHaveTextContent("state.outputs.display_name");
      expect(outputDuringFailure).not.toHaveTextContent("state.hooks.before_finish.output.customer");
    });
    await user.click(source);
    const inputDuringFailure = within(screen.getByTestId(source.getAttribute("aria-controls")!))
      .getByRole("option", { name: /Customer name/ });
    expect(inputDuringFailure).toHaveTextContent("state.inputs.client_name");
    expect(inputDuringFailure).not.toHaveTextContent("Hook input customer");

    failDetail = false;
    await act(async () => { await client.refetchQueries({ queryKey: key, type: "active" }); });
    expect(detailRequests).toBe(3);
    expect(client.getQueryState(key)?.status).toBe("success");
    await waitFor(() => expect(within(screen.getByTestId(source.getAttribute("aria-controls")!))
      .getByRole("option", { name: /Customer name/ })).toHaveTextContent("Hook input customer"));
    await user.click(target);
    await waitFor(() => expect(within(screen.getByTestId(target.getAttribute("aria-controls")!))
      .getByRole("option", { name: /Corrected customer/ })).toHaveTextContent("state.hooks.before_finish.output.customer"));
    expect(changeUrls).toEqual([]);
  } finally {
    view.unmount();
    client.clear();
  }
});


it("names the selected Hook in child root and nested Structure, restoring the ordinary author root after switching", async () => {
  installHookedWorkflow({ authorEditable: true });
  addNestedClientContainers();
  addSecondHook();
  await renderHookedBuilder();

  async function openStructure(canvasStepId: string) {
    const card = await screen.findByText((_, element) => element?.getAttribute("data-workflow-step-measure-id") === canvasStepId);
    const step = screen.getAllByRole("group", { name: /^Open step / }).find((candidate) =>
      within(candidate).queryByText((_, element) => element === card) === card);
    expect(step).toBeDefined();
    fireEvent.contextMenu(step!);
    fireEvent.click(screen.getByRole("menuitem", { name: "Open" }));
    await screen.findByDisplayValue(canvasStepId);
    fireEvent.click(screen.getByRole("tab", { name: "Smart setup" }));
    return screen.findByTestId("step-structure-setup");
  }

  for (const [hookId, hookName] of [["before_finish", "Before the order is finished"], ["after_finish", "After the order is finished"]]) {
    const root = await openStructure(`hook:${hookId}:return`);
    expect(root).toHaveTextContent(`Hook ${hookName}`);
    expect(root).not.toHaveTextContent("Now in the workflow root.");
    const nested = await openStructure(`hook:${hookId}:fix_name`);
    expect(nested).toHaveTextContent(`Hook ${hookName}`);
    expect(nested).toHaveTextContent("Client items");
    fireEvent.click(within(nested).getByRole("combobox", { name: "Move step to" }));
    expect(screen.getByRole("option", { name: `Hook ${hookName}` })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
  }
  const authorRoot = await openStructure("prepare");
  expect(authorRoot).toHaveTextContent("Now in the workflow root.");
  expect(authorRoot).not.toHaveTextContent(/Hook (Before|After) the order is finished/);
  expect(changeUrls).toEqual([]);
  expect(postedChangeSets()).toEqual([]);
});


it("uses the selected Hook ID when its declaration and frame have no display name", async () => {
  installHookedWorkflow();
  addSecondHook({ displayName: "" });
  await renderHookedBuilder();
  const card = await screen.findByText((_, element) => element?.getAttribute("data-workflow-step-measure-id") === "hook:after_finish:fix_name");
  const step = screen.getAllByRole("group", { name: /^Open step / }).find((candidate) =>
    within(candidate).queryByText((_, element) => element === card) === card);
  expect(step).toBeDefined();
  fireEvent.contextMenu(step!);
  fireEvent.click(screen.getByRole("menuitem", { name: "Open" }));
  await screen.findByDisplayValue("hook:after_finish:fix_name");
  fireEvent.click(screen.getByRole("tab", { name: "Smart setup" }));
  expect(await screen.findByTestId("step-structure-setup")).toHaveTextContent("Hook after_finish");
  expect(changeUrls).toEqual([]);
});
