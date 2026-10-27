import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { act, configure, getConfig, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  fetchSpy,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowValidation,
  renderPage,
} from "./index.test-helpers";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

/**
 * AUDIT-033 — the Workflow Builder could not populate the Agent selector.
 *
 * `stepAgentOptions` was assembled only from fields already present on the
 * workflow record (participants, agent_catalog, registry_agents, ...). Nothing
 * fetched the agents registry, which is why the audit's request trace showed the
 * builder repeatedly loading Tools but never requesting agents.
 *
 * A workflow with no participants yet therefore showed an empty selector however
 * full the registry was, and saving reported "agent step requires agent_id or
 * participant_key".
 *
 * These tests use a workflow record with **no** agent fields of any kind, so the
 * only possible source is the registry. They fail against the pre-fix
 * controller.
 */

const SAVED_QUERIES = [
  { query_template_id: "qt_active_users", display_name: "Active users", enabled: true },
];

const REGISTRY_AGENTS = [
  { agent_id: "release_writer", display_name: "Release Writer", enabled: true },
  { agent_id: "reviewer", display_name: "Reviewer", enabled: true },
];

/** A workflow carrying no agents, participants or catalog of its own. */
function workflowWithDatasetQueryStep() {
  return {
    workflow_id: "weekly_report",
    display_name: "Weekly Report",
    enabled: true,
    steps: [{
            id: "11111111-1111-1111-1111-111111111011",
            step_id: "step_1",
            display_name: "Step one",
            step_kind: "dataset_query",
            override: {},
          }], groups: [],
  };
}

function workflowWithoutAnyAgentFields() {
  return {
    workflow_id: "weekly_report",
    display_name: "Weekly Report",
    enabled: true,
    steps: [{
            id: "11111111-1111-1111-1111-111111111010",
            step_id: "step_1",
            display_name: "Step one",
            step_kind: "agent",
            override: {},
          }], groups: [],
  };
}

function serveRegistryAgents() {
  const previous = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    const url = stringFromUnknownValue(input);
    if (url.includes("/api/agents")) {
      return new Response(JSON.stringify(REGISTRY_AGENTS), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/api/datasets/query-templates")) {
      return new Response(JSON.stringify({ query_templates: SAVED_QUERIES }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return previous ? previous(input, init) : new Response("{}", { status: 200 });
  });
}

async function openAgentStepEditor() {
  const user = userEvent.setup();
  const stepButton = await screen.findByRole("button", { name: /Step one/i });
  await user.click(stepButton);
  return user;
}

describe("WorkflowsDetailPage agent catalog (AUDIT-033)", () => {
  it.each(["agent", "dataset_query"])("retains a real %s step label and keyboard focus through the60s catalog refresh", async (kind) => {
    mockedFetchWorkflowDetail.mockResolvedValue((kind === "agent" ? workflowWithoutAnyAgentFields() : workflowWithDatasetQueryStep()) as never);
    const target = kind === "agent" ? "/api/agents" : "/api/datasets/query-templates";
    const payload = kind === "agent" ? REGISTRY_AGENTS : { query_templates: SAVED_QUERIES };
    const previousFetch = fetchSpy.getMockImplementation();
    let hold = false;
    let calls = 0;
    let resolvePoll: ((response: Response) => void) | undefined;
    fetchSpy.mockImplementation(async (input, init) => {
      if (stringFromUnknownValue(input).includes(target)) {
        calls += 1;
        if (hold) return new Promise<Response>((resolve) => { resolvePoll = resolve; });
        return new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } });
      }
      return previousFetch ? previousFetch(input, init) : new Response("{}");
    });
    const view = renderPage();
    const step = await screen.findByRole("button", { name: /Step one/i });
    vi.useFakeTimers();
    const previousWrapper = getConfig().asyncWrapper;
    let indicator: ReturnType<typeof render> | undefined;
    try {
      configure({ asyncWrapper: async (callback) => { const result = await act(async () => callback()); await act(async () => { await vi.advanceTimersByTimeAsync(0); }); return result; } });
      const user = userEvent.setup({ pointerEventsCheck: 0, advanceTimers: (delay) => { vi.advanceTimersByTime(delay); } });
      await user.click(step);
      const label = screen.getByRole("textbox", { name: "Step label" });
      await user.type(label, " draft");
      expect(label).toHaveValue("Step one draft");
      indicator = render(<RequestLoadingIndicator />);
      expect(calls).toBe(1);
      hold = true;
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(calls).toBe(2); expect(resolvePoll).toBeDefined();
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(screen.getByRole("textbox", { name: "Step label" })).toBe(label);
      expect(label).toHaveValue("Step one draft"); expect(label).toHaveFocus();
      await user.keyboard(" survives refresh");
      expect(label).toHaveValue("Step one draft survives refresh");
      await act(async () => { resolvePoll?.(new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } })); await vi.advanceTimersByTimeAsync(0); });
      expect(label).toHaveFocus(); expect(label).toHaveValue("Step one draft survives refresh");
      expect(screen.getByRole("combobox", { name: kind === "agent" ? "Agent" : "Saved query" })).toBeEnabled();
    } finally {
      configure({ asyncWrapper: previousWrapper });
      view.unmount(); indicator?.unmount(); resolvePoll?.(new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } }));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      vi.clearAllTimers(); vi.useRealTimers();
    }
  });

  beforeEach(() => {
    mockedFetchWorkflowDetail.mockResolvedValue(workflowWithoutAnyAgentFields() as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    serveRegistryAgents();
  });

  it("requests the agents registry when an agent step is opened", async () => {
    renderPage();
    await openAgentStepEditor();

    await waitFor(() => {
      const requested = fetchSpy.mock.calls.map((call) => stringFromUnknownValue(call[0]));
      expect(requested.some((url) => url.includes("/api/agents"))).toBe(true);
    });
  });

  it("offers registry agents even when the workflow carries none of its own", async () => {
    renderPage();
    const user = await openAgentStepEditor();

    await waitFor(() => {
      const requested = fetchSpy.mock.calls.map((call) => stringFromUnknownValue(call[0]));
      expect(requested.some((url) => url.includes("/api/agents"))).toBe(true);
    });

    // Registry options live in the shared select's portal while it is open.
    const agentSelect = screen.getByRole("combobox", { name: "Agent" });
    await waitFor(() => expect(agentSelect).toBeEnabled());
    await user.click(agentSelect);
    const agentOptions = screen.getByRole("listbox", { name: "Agent options" });
    await user.click(await within(agentOptions).findByRole("option", { name: "Release Writer" }, { timeout: 4000 }));
    expect(agentSelect).toHaveTextContent("Release Writer");
    await user.click(agentSelect);
    expect(within(screen.getByRole("listbox", { name: "Agent options" }))
      .getByRole("option", { name: "Release Writer" })).toHaveAttribute("aria-selected", "true");
  });

  it("requests saved queries for a dataset_query step without requiring a project", async () => {
    // The builder often has no project in context. `fetchDatasetQueryTemplates`
    // treats projectId as optional, so gating the query on it only produced a
    // permanent "No saved queries are available".
    mockedFetchWorkflowDetail.mockResolvedValue(workflowWithDatasetQueryStep() as never);
    renderPage();
    await openAgentStepEditor();

    await waitFor(() => {
      const requested = fetchSpy.mock.calls.map((call) => stringFromUnknownValue(call[0]));
      expect(requested.some((url) => url.includes("/api/datasets/query-templates"))).toBe(true);
    });
  });

  it("reports a failed saved-query catalog separately from an empty catalog", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue(workflowWithDatasetQueryStep() as never);
    fetchSpy.mockImplementation(async (input) => {
      if (stringFromUnknownValue(input).includes("/api/datasets/query-templates")) {
        return new Response(JSON.stringify({ detail: "catalog backend unavailable" }), {
          status: 503,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", { status: 200 });
    });

    renderPage();
    await openAgentStepEditor();

    expect(
      await screen.findByText(/saved query catalog could not be loaded/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no saved queries are available/i)).not.toBeInTheDocument();
  });

  it("reports a failed agent catalog instead of an empty selector", async () => {
    fetchSpy.mockImplementation(async (input) => {
      if (stringFromUnknownValue(input).includes("/api/agents")) {
        return new Response(JSON.stringify({ detail: "agent registry unavailable" }), {
          status: 503,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", { status: 200 });
    });

    renderPage();
    await openAgentStepEditor();

    expect(await screen.findByText(/agent catalog could not be loaded/i)).toBeInTheDocument();
  });
});
