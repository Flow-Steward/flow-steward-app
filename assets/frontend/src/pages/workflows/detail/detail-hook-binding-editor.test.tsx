import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { WorkflowHookOfLinks } from "@/pages/workflows/detail/detail-hook-of-links";
import * as workflowApi from "@/pages/workflows/shared/api";
import * as projectWorkflowsApi from "@/pages/projects/workflows/api";
import { HookBindingEditor, HookTestRunPanel } from "@/pages/workflows/detail/detail-hook-binding-editor";
import * as hookApi from "@/pages/workflows/shared/hook-api";
import type { WorkflowHook } from "@/pages/workflows/shared/hook-api";
import * as simulateApi from "@/pages/workflows/shared/simulate-api";

vi.mock("@/pages/workflows/shared/hook-api", async (importOriginal) => {
  const actual = await importOriginal<typeof hookApi>();
  return {
    ...actual,
    fetchWorkflowHook: vi.fn(),
    previewWorkflowHookBinding: vi.fn(),
    bindWorkflowHook: vi.fn(),
    unbindWorkflowHook: vi.fn(),
    createAndBindWorkflowHook: vi.fn(),
    fetchWorkflowWhereUsed: vi.fn(),
  };
});

vi.mock("@/pages/workflows/shared/simulate-api", async (importOriginal) => {
  const actual = await importOriginal<typeof simulateApi>();
  return { ...actual, testRunWorkflow: vi.fn() };
});

const api = vi.mocked(hookApi);
const simulate = vi.mocked(simulateApi);

function hook(binding: WorkflowHook["binding"] = null, revision = 0): WorkflowHook {
  return {
    project_id: "p-1",
    workflow_id: "invoice_flow",
    hook_id: "before_invoice",
    step_id: "before",
    display_name: "Before creating an invoice",
    description: "Adjust the buyer before the invoice is created.",
    contract_version: 1,
    hook_semantic_digest: "digest-1",
    binding_revision: revision,
    binding,
    contract: {
      hook_id: "before_invoice",
      contract_version: 1,
      display_name: "Before creating an invoice",
      input_schema: { type: "object", properties: { invoice_id: { type: "integer" }, company: { type: "string" } } },
      mutable_fields: ["company"],
      additional_outputs: { skip_request: { schema: { type: "boolean" }, default: false } },
      stage_contract: { summary: "Runs before the invoice is created.", effects_before: [], effects_after: ["invoice.create"] },
    },
  };
}

function mountTestRun(extra: Record<string, unknown> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <HookTestRunPanel {...extra} hookId="before_invoice" projectId="p-1" workflowId="invoice_flow" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function mount(canAuthor = true, extra: Record<string, unknown> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <HookBindingEditor canAuthor={canAuthor} hookId="before_invoice" projectId="p-1" workflowId="invoice_flow" {...extra} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchWorkflowWhereUsed.mockResolvedValue({ project_id: "p-1", workflow_id: "enrich_invoice", consumers: [] });
});

describe("a hook in the step drawer (R2-11)", () => {
  it("explains an empty hook in plain words and offers no workflow to choose, map or connect", async () => {
    api.fetchWorkflowHook.mockResolvedValue(hook());
    mount();

    expect(await screen.findByText("Before creating an invoice")).toBeInTheDocument();
    expect(screen.getByText("Empty")).toBeInTheDocument();
    expect(screen.getByText(/the run goes on with the inputs unchanged/)).toBeInTheDocument();
    expect(screen.getByText("invoice.create")).toBeInTheDocument();
    // What it receives, what its steps may change or add.
    for (const label of ["It receives:", "Its steps may change:", "Its steps may add:"]) expect(screen.getByText(label)).toBeInTheDocument();
    // invoice_id is only received; company is received and may change; skip_request may be added.
    expect(screen.getAllByText("invoice_id")).toHaveLength(1);
    expect(screen.getAllByText("company")).toHaveLength(2);
    expect(screen.getAllByText("skip_request")).toHaveLength(1);
    expect(screen.getByText(/inside the hook's frame/)).toBeInTheDocument();
    expect(screen.getByText(/A result no step sets keeps its input value/)).toBeInTheDocument();
    // No selector, mapping, Check, Connect, Disconnect or Create and connect.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open workflow" })).not.toBeInTheDocument();
    expect(api.bindWorkflowHook).not.toHaveBeenCalled();
    expect(api.unbindWorkflowHook).not.toHaveBeenCalled();
    expect(api.createAndBindWorkflowHook).not.toHaveBeenCalled();
  });

  it("counts the hook's steps without naming the workflow that keeps them", async () => {
    api.fetchWorkflowHook.mockResolvedValue(
      hook({ revision_id: "r-1", target_workflow_id: "invoice_flow_before_invoice", input_mapping: {}, output_mapping: {} }, 1),
    );
    mount(true, { stepCount: 2, onClear: vi.fn() });

    expect(await screen.findByText("2 steps")).toBeInTheDocument();
    expect(screen.queryByText("Empty")).not.toBeInTheDocument();
    expect(screen.queryByText(/invoice_flow_before_invoice/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Connected/)).not.toBeInTheDocument();
  });

  it("clears a hook with steps through Clear the hook instead of Disconnect", async () => {
    api.fetchWorkflowHook.mockResolvedValue(
      hook({ revision_id: "r-1", target_workflow_id: "invoice_flow_before_invoice", input_mapping: {}, output_mapping: {} }, 1),
    );
    const onClear = vi.fn();
    mount(true, { stepCount: 1, onClear });

    expect(await screen.findByText("1 step")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear the hook" }));
    await waitFor(() => expect(onClear).toHaveBeenCalledTimes(1));
    expect(api.unbindWorkflowHook).not.toHaveBeenCalled();
  });

  it("offers nothing to clear on an empty hook", async () => {
    api.fetchWorkflowHook.mockResolvedValue(hook());
    mount(true, { stepCount: 0, onClear: vi.fn() });
    expect(await screen.findByText("Empty")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear the hook" })).not.toBeInTheDocument();
  });

  it("is read-only for a viewer without authoring rights", async () => {
    api.fetchWorkflowHook.mockResolvedValue(hook());
    mount(false, { stepCount: 3, onClear: vi.fn() });

    expect(await screen.findByText("Before creating an invoice")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear the hook" })).not.toBeInTheDocument();
  });

  it("asks for a project with the picker it is given, and calls no project API without one", async () => {
    mount(true, { projectId: "", projectPicker: <button type="button">Pick a linked project</button> });

    expect(await screen.findByText("Choose a project")).toBeInTheDocument();
    expect(screen.getByText(/Each project keeps its own steps in a hook/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pick a linked project" })).toBeInTheDocument();
    expect(api.fetchWorkflowHook).not.toHaveBeenCalled();
  });
});

describe("the hook's Test Run", () => {
  it("resets a pending hook test when the selected project changes", async () => {
    api.fetchWorkflowHook.mockResolvedValue(
      hook({ revision_id: "r-1", target_workflow_id: "enrich_invoice", input_mapping: {}, output_mapping: {} }, 1),
    );
    let finish!: (value: Awaited<ReturnType<typeof simulate.testRunWorkflow>>) => void;
    simulate.testRunWorkflow.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const onTestRunStarted = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const panel = (projectId: string) => <QueryClientProvider client={client}><MemoryRouter>
      <HookTestRunPanel hookId="before_invoice" projectId={projectId} workflowId="invoice_flow" {...{ onTestRunStarted }} />
    </MemoryRouter></QueryClientProvider>;
    const view = render(panel("p-1"));
    fireEvent.click(await screen.findByRole("button", { name: "Run hook test run" }));
    expect(screen.getByRole("button", { name: "Run hook test run" })).toBeDisabled();
    view.rerender(panel("p-2"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Run hook test run" })).toBeEnabled());
    finish({ status: "completed", job_id: "old-project-job", needs_user_approval: false, workflow_id: "enrich_invoice" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Run hook test run" })).toBeEnabled());
    expect(onTestRunStarted).not.toHaveBeenCalled();
  });

  it("does not publish an isolated result after its hook drawer was closed", async () => {
    api.fetchWorkflowHook.mockResolvedValue(
      hook({ revision_id: "r-1", target_workflow_id: "enrich_invoice", input_mapping: {}, output_mapping: {} }, 1),
    );
    let finish!: (value: Awaited<ReturnType<typeof simulate.testRunWorkflow>>) => void;
    simulate.testRunWorkflow.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const onTestRunStarted = vi.fn();
    const view = mountTestRun({ onTestRunStarted });
    fireEvent.click(await screen.findByRole("button", { name: "Run hook test run" }));
    await waitFor(() => expect(simulate.testRunWorkflow).toHaveBeenCalledTimes(1));
    view.unmount();
    finish({ status: "completed", job_id: "old-job", needs_user_approval: false, workflow_id: "enrich_invoice" });
    await waitFor(() => expect(screen.queryByTestId("hook-test-result")).not.toBeInTheDocument());
    expect(onTestRunStarted).not.toHaveBeenCalled();
  });

  it("announces the actual isolated job and declaring hook for canvas execution marks", async () => {
    api.fetchWorkflowHook.mockResolvedValue(
      hook({ revision_id: "r-1", target_workflow_id: "enrich_invoice", input_mapping: {}, output_mapping: {} }, 1),
    );
    simulate.testRunWorkflow.mockResolvedValue({ status: "completed", job_id: "isolated-job", needs_user_approval: false, workflow_id: "enrich_invoice", workflow_outputs: { outputs: {} } });
    api.previewWorkflowHookBinding.mockResolvedValue({ valid: true, issues: [], sample_output: {} });
    const onTestRunStarted = vi.fn();
    mountTestRun({ onTestRunStarted });
    fireEvent.click(await screen.findByRole("button", { name: "Run hook test run" }));
    await waitFor(() => expect(onTestRunStarted).toHaveBeenCalledWith("isolated-job", "before_invoice"));
  });
  it("blocks an isolated hook run when edits have not been applied", async () => {
    api.fetchWorkflowHook.mockResolvedValue(
      hook({ revision_id: "r-1", target_workflow_id: "enrich_invoice", input_mapping: {}, output_mapping: {} }, 1),
    );
    const reason = "Apply or discard the workflow changes before starting Test Run.";
    mountTestRun({ blockedReason: reason });
    const run = await screen.findByRole("button", { name: "Run hook test run" });
    expect(run).toBeDisabled();
    expect(screen.getByTestId("step-test-run-blocked")).toHaveTextContent(reason);
    fireEvent.click(run);
    expect(simulate.testRunWorkflow).not.toHaveBeenCalled();
  });
  it("runs the hook's TEST RUN on typed hook inputs and shows what the hook would write back", async () => {
    api.fetchWorkflowHook.mockResolvedValue(
      hook({ revision_id: "r-1", target_workflow_id: "enrich_invoice", input_mapping: { invoice_id: "invoice_id" }, output_mapping: { company: "company" } }, 1),
    );
    simulate.testRunWorkflow.mockResolvedValue({
      status: "completed",
      job_id: "job-7",
      needs_user_approval: false,
      workflow_id: "enrich_invoice",
      workflow_outputs: { ok: true, outputs: { company: "Acme" } },
    });
    api.previewWorkflowHookBinding.mockResolvedValue({
      valid: true,
      issues: [],
      sample_output: { invoice_id: 7, company: "Acme", skip_request: false },
    });
    mountTestRun();

    // B14: the hook's own Test Run, in the drawer's TEST RUN tab.
    fireEvent.change(await screen.findByRole("textbox", { name: "Test value for invoice_id" }), { target: { value: "7" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Test value for company" }), { target: { value: "Example UAB" } });
    fireEvent.click(screen.getByRole("button", { name: "Run hook test run" }));

    // Only the child runs, on the mapped inputs; the author workflow does not.
    await waitFor(() => expect(simulate.testRunWorkflow).toHaveBeenCalledTimes(1));
    expect(simulate.testRunWorkflow.mock.calls[0][0]).toMatchObject({ workflowId: "enrich_invoice", projectId: "p-1", inputs: { invoice_id: 7 } });
    await waitFor(() => expect(api.previewWorkflowHookBinding).toHaveBeenCalledTimes(1));
    expect(api.previewWorkflowHookBinding.mock.calls[0][4]).toEqual({
      inputs: { invoice_id: 7, company: "Example UAB" },
      child_outputs: { company: "Acme" },
    });
    const result = await screen.findByTestId("hook-test-result");
    expect(result).toHaveTextContent("The hook would publish");
    expect(result).toHaveTextContent("Acme");
    expect(screen.getByRole("link", { name: "Open the Test Run" })).toHaveAttribute("href", "/jobs/job-7");
  });

  it("says to add a step first when the hook is empty", async () => {
    api.fetchWorkflowHook.mockResolvedValue(hook());
    mountTestRun();

    expect(await screen.findByTestId("step-test-run-blocked")).toHaveTextContent(
      "This hook is empty. Add a step to it before testing it.",
    );
    expect(screen.queryByRole("button", { name: "Run hook test run" })).not.toBeInTheDocument();
  });

});

describe("hook-of links", () => {
  it("opens the declaring step from one complete where-used projection without caller detail fetches", async () => {
    const callerId = "extension.11111111-1111-4111-8111-111111111111.prepare_invoice";
    const linked = vi.spyOn(projectWorkflowsApi, "fetchProjectWorkflowBindings").mockResolvedValue({
      project_id: "p-1", workflow_ids: ["enrich_invoice"], operator_default_workflow_id: "", project_workflows: [],
    });
    const detail = vi.spyOn(workflowApi, "fetchWorkflowDetail").mockResolvedValue({ workflow_id: callerId, steps: [] } as never);
    api.fetchWorkflowWhereUsed.mockResolvedValue({
      project_id: "p-1", workflow_id: "enrich_invoice",
      consumers: [{ kind: "hook", caller_workflow_id: callerId, project_id: "p-1", hook_id: "adjust_invoice", step_id: "adjust_stage", ...{
        caller_extension_name: "Invoice Showcase", caller_display_name: "Prepare invoice", hook_display_name: "Adjust order",
      } }, { kind: "hook", caller_workflow_id: "extension.22222222-2222-4222-8222-222222222222.preview_invoice", project_id: "p-1", hook_id: "preview_hook", step_id: "preview_stage", ...{
        caller_extension_name: "Invoice Showcase", caller_display_name: "Preview invoice", hook_display_name: "Review preview",
      } }],
    });
    try {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(<QueryClientProvider client={client}><MemoryRouter>
        <WorkflowHookOfLinks projectId="p-1" workflowId="enrich_invoice" />
      </MemoryRouter></QueryClientProvider>);

      const link = await screen.findByRole("link", { name: /Invoice Showcase.*Adjust order/ });
      const href = new URL(link.getAttribute("href")!, "http://localhost");
      expect(href.pathname).toBe(`/registry/workflows/${encodeURIComponent(callerId)}/builder`);
      expect(href.searchParams.get("project_id")).toBe("p-1");
      expect(href.searchParams.get("drawer")).toBe("step");
      expect(href.searchParams.get("step_id")).toBe("adjust_stage");
      expect(within(screen.getByTestId("hook-of-links")).getAllByRole("link")).toHaveLength(2);
      expect(linked).toHaveBeenCalledTimes(1);
      expect(linked).toHaveBeenCalledWith("p-1");
      expect(api.fetchWorkflowWhereUsed).toHaveBeenCalledTimes(1);
      expect(detail).not.toHaveBeenCalled();
    } finally {
      linked.mockRestore();
      detail.mockRestore();
    }
  });
});

describe("Hook Test Run input required semantics", () => {
  function testHook() {
    const result = hook({
      revision_id: "r-1", target_workflow_id: "enrich_invoice",
      input_mapping: { count: "qty", buyer: "customer", shipping: "delivery", note: "note", fallback: "fallback" },
      output_mapping: { company: "company" },
    }, 1);
    result.contract.input_schema = {
      type: "object",
      properties: {
        qty: { type: "integer", default: 0 },
        customer: { type: "object", properties: { name: { type: ["string", "null"] } } },
        delivery: { type: "boolean" },
        note: { type: "string" },
        fallback: { type: "integer", default: 7 },
      },
      required: ["qty", "customer", "delivery"],
    };
    return result;
  }

  it("exposes required contract inputs to native validation and assistive technology, keeping optional inputs optional", async () => {
    api.fetchWorkflowHook.mockResolvedValue(testHook());
    mountTestRun();
    await screen.findByRole("textbox", { name: "Test value for qty" });

    for (const name of ["qty", "customer", "delivery"]) {
      const input = screen.getByRole("textbox", { name: `Test value for ${name}` });
      expect(input).toBeRequired();
      expect({ native: input.getAttribute("required"), aria: input.getAttribute("aria-required") }).toEqual({ native: "", aria: "true" });
    }
    for (const name of ["note", "fallback"]) {
      const input = screen.getByRole("textbox", { name: `Test value for ${name}` });
      expect(input).not.toBeRequired();
      expect({ native: input.getAttribute("required"), aria: input.getAttribute("aria-required") }).toEqual({ native: null, aria: null });
      expect(input).toHaveValue("");
    }
    // Output defaults and backend binding metadata are not test input fields.
    for (const name of ["skip_request", "binding_revision", "hook_semantic_digest"]) {
      expect(screen.queryByRole("textbox", { name: `Test value for ${name}` })).not.toBeInTheDocument();
    }
  });

  it("keeps zero, false, nested null and exact text in the mapped submission without injecting an untouched default", async () => {
    api.fetchWorkflowHook.mockResolvedValue(testHook());
    simulate.testRunWorkflow.mockResolvedValue({
      status: "completed", job_id: "required-input-job", needs_user_approval: false,
      workflow_id: "enrich_invoice", workflow_outputs: { outputs: { company: "" } },
    });
    api.previewWorkflowHookBinding.mockResolvedValue({
      valid: true, issues: [],
      sample_output: { qty: 0, customer: { name: null }, delivery: false, note: "  keep  ", company: "", skip_request: false },
    });
    mountTestRun();
    fireEvent.change(await screen.findByRole("textbox", { name: "Test value for qty" }), { target: { value: "0" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Test value for customer" }), { target: { value: '{"name":null}' } });
    fireEvent.change(screen.getByRole("textbox", { name: "Test value for delivery" }), { target: { value: "false" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Test value for note" }), { target: { value: "  keep  " } });
    fireEvent.click(screen.getByRole("button", { name: "Run hook test run" }));

    await waitFor(() => expect(api.previewWorkflowHookBinding).toHaveBeenCalledTimes(1));
    expect(simulate.testRunWorkflow).toHaveBeenCalledTimes(1);
    expect(simulate.testRunWorkflow.mock.calls[0][0]).toEqual({
      workflowId: "enrich_invoice", projectId: "p-1", taskSummary: "Test of the hook Before creating an invoice",
      inputs: { count: 0, buyer: { name: null }, shipping: false, note: "  keep  " },
    });
    expect(api.previewWorkflowHookBinding.mock.calls[0][4]).toEqual({
      inputs: { qty: 0, customer: { name: null }, delivery: false, note: "  keep  " },
      child_outputs: { company: "" },
    });
    expect(screen.getByRole("textbox", { name: "Test value for fallback" })).toHaveValue("");
    expect(await screen.findByTestId("hook-test-result")).toHaveTextContent("The hook would publish");
  });
});
