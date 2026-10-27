import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { useAuth } from "@/lib/auth/useAuth";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider } from "react-router";

import {
  fetchWorkflowCanvasGraph,
  fetchWorkflowDag,
  fetchWorkflowContract,
  fetchWorkflowDetail,
  fetchWorkflowExportPolicy,
  fetchWorkflowTransformActions,
  fetchWorkflowValidation,
  previewWorkflowTransform,
} from "@/pages/workflows/shared/api";
import {
  fetchWorkflowTestRunState,
  submitWorkflowTestRunApproval,
  testRunWorkflow,
  testRunWorkflowStep,
} from "@/pages/workflows/shared/simulate-api";
import { fetchWorkflowLinkedProjects, findWorkflowLinkedProjectId } from "@/pages/workflows/shared/project-context";
import { WorkflowsDetailPage } from "@/pages/workflows/detail";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import type * as WorkflowApi from "@/pages/workflows/shared/api";
import type * as ProjectContext from "@/pages/workflows/shared/project-context";

vi.mock("@/pages/workflows/shared/api", async (importOriginal) => {
  const original = await importOriginal<typeof WorkflowApi>();
  return {
    // Step I/O helpers use the real API with mocked transport.
    fetchWorkflowStepAvailableVariables: original.fetchWorkflowStepAvailableVariables,
    fetchWorkflowStepIOValidation: original.fetchWorkflowStepIOValidation,
    fetchWorkflowCanvasGraph: vi.fn(),
    fetchWorkflowDag: vi.fn(),
    fetchWorkflowContract: vi.fn(),
    fetchWorkflowDetail: vi.fn(),
    fetchWorkflowExportPolicy: vi.fn(),
    fetchWorkflowStepContractPreview: vi.fn(async () => ({
      locked_required: [],
      unresolved_required: [],
      optional: [],
      custom: [],
      outputs: [],
    })),
    fetchWorkflowTransformActions: vi.fn(async () => ({
      actions: [
        {
          action_id: "text.trim",
          category: "text",
          display_name: "Trim",
          value_scope: "scalar",
          input_type: "text",
          output_type: "text",
          params_schema: { type: "object", properties: {}, additionalProperties: false },
          param_sources: {},
          pure: true,
          deterministic: true,
          io_allowed: false,
          transform_safe: true,
        },
        {
          action_id: "text.append",
          category: "text",
          display_name: "Append",
          value_scope: "scalar",
          input_type: "text",
          output_type: "text",
          params_schema: {
            type: "object",
            properties: { append_value: { type: "json" } },
            additionalProperties: false,
          },
          param_sources: {
            append_value: {
              literal: true,
              selector: true,
              current: true,
              types: ["text", "number", "boolean", "json"],
            },
          },
          pure: true,
          deterministic: true,
          io_allowed: false,
          transform_safe: true,
        },
        {
          action_id: "text.length",
          category: "text",
          display_name: "Length",
          value_scope: "scalar",
          input_type: "text",
          output_type: "number",
          params_schema: { type: "object", properties: {}, additionalProperties: false },
          param_sources: {},
          pure: true,
          deterministic: true,
          io_allowed: false,
          transform_safe: true,
        },
      ],
    })),
    previewWorkflowTransform: vi.fn(async () => ({
      ok: true,
      errors: [],
      output: { full_name: "Ada Lovelace" },
      result_target: "state.transform_result",
      state_patch: { transform_result: { full_name: "Ada Lovelace" } },
      detail: "transform step executed",
      preview: true,
    })),
    fetchWorkflowValidation: vi.fn(),
  };
});
vi.mock("@/pages/workflows/shared/project-context", async (importOriginal) => ({
  ...(await importOriginal<typeof ProjectContext>()),
  findWorkflowLinkedProjectId: vi.fn(),
  fetchWorkflowLinkedProjects: vi.fn(),
}));
vi.mock("@/pages/workflows/shared/simulate-api", () => ({
  fetchWorkflowTestRunState: vi.fn(),
  submitWorkflowTestRunApproval: vi.fn(),
  testRunWorkflow: vi.fn(),
  testRunWorkflowStep: vi.fn(),
}));
// Detail-page tests exercise authoring surfaces (manual workflow edit controls);
// run them as an admin so those controls render. RBAC hiding for non-admins is
// covered by dedicated header/guard tests.
vi.mock("@/lib/auth/use-is-admin", () => ({
  useIsAdmin: vi.fn(() => true),
}));
vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: vi.fn(() => ({
    session: {
      authenticated: true,
      roles: ["admin"],
      user: { roles: ["admin"] },
    },
  })),
}));

export const mockedUseIsAdmin = vi.mocked(useIsAdmin);
export const mockedUseAuth = vi.mocked(useAuth);

export const mockedFetchWorkflowCanvasGraph = vi.mocked(fetchWorkflowCanvasGraph);
export const mockedFetchWorkflowDag = vi.mocked(fetchWorkflowDag);
export const mockedFetchWorkflowContract = vi.mocked(fetchWorkflowContract);
export const mockedFetchWorkflowDetail = vi.mocked(fetchWorkflowDetail);
export const mockedFetchWorkflowExportPolicy = vi.mocked(fetchWorkflowExportPolicy);
export const mockedFetchWorkflowTransformActions = vi.mocked(fetchWorkflowTransformActions);
export const mockedFetchWorkflowValidation = vi.mocked(fetchWorkflowValidation);
export const mockedPreviewWorkflowTransform = vi.mocked(previewWorkflowTransform);
export const mockedFindWorkflowLinkedProjectId = vi.mocked(findWorkflowLinkedProjectId);
export const mockedFetchWorkflowLinkedProjects = vi.mocked(fetchWorkflowLinkedProjects);
export const mockedFetchWorkflowTestRunState = vi.mocked(fetchWorkflowTestRunState);
export const mockedSubmitWorkflowTestRunApproval = vi.mocked(submitWorkflowTestRunApproval);
export const mockedTestRunWorkflow = vi.mocked(testRunWorkflow);
export const mockedTestRunWorkflowStep = vi.mocked(testRunWorkflowStep);

// jsdom has no DOMMatrix implementation. ReactFlow reads m22 when a kind
// change replaces a node renderer; the fixture supplies its viewport scale.
class TestDOMMatrixReadOnly {
  readonly m22: number;

  constructor(transform: string) {
    const matrix = /^matrix\(([^)]+)\)$/.exec(transform);
    const scale = /scale\(([^)]+)\)/.exec(transform);
    this.m22 = matrix ? Number(matrix[1].split(",")[3]) : Number(scale?.[1] || 1);
  }
}

class TestResizeObserver {
  disconnect() {
    return undefined;
  }

  observe() {
    return undefined;
  }

  unobserve() {
    return undefined;
  }
}

export function renderPage({
  embedded = true,
  surface = "detail",
  initialEntry = "/registry/workflows/weekly_report/builder",
}: {
  embedded?: boolean;
  initialEntry?: string;
  surface?: "detail" | "builder";
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const router = createMemoryRouter([
    { path: "/registry/workflows/:workflowId", element: <WorkflowsDetailPage embedded={embedded} surface={surface} /> },
    { path: "/registry/workflows/:workflowId/builder", element: <WorkflowsDetailPage embedded={embedded} surface={surface} /> },
    { path: "/previous", element: <h1>Previous page</h1> },
  ], { initialEntries: ["/previous", initialEntry] });
  return {
    ...render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>),
    router,
  };
}

export const fetchSpy = vi.fn<typeof fetch>(
  async () =>
    new Response(
      JSON.stringify({
        required_inputs: [],
        optional_inputs: [],
        custom_inputs: [],
        current_expected_inputs: [],
      }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      },
    ),
);

beforeEach(() => {
  mockedUseIsAdmin.mockReturnValue(true);
  mockedUseAuth.mockReturnValue({ session: { authenticated: true, roles: ["admin"], user: { roles: ["admin"] } } } as ReturnType<typeof mockedUseAuth>);
  vi.stubGlobal("ResizeObserver", TestResizeObserver);
  vi.stubGlobal("DOMMatrixReadOnly", TestDOMMatrixReadOnly);
  globalThis.localStorage.clear();
  Object.defineProperty(globalThis, "innerWidth", {
    configurable: true,
    value: 1280,
    writable: true,
  });
  const svgPrototype = SVGElement.prototype as SVGElement & {
    getBBox?: () => { height: number; width: number; x: number; y: number };
  };
  if (!svgPrototype.getBBox) {
    Object.defineProperty(SVGElement.prototype, "getBBox", {
      configurable: true,
      value: () => ({ height: 16, width: 120, x: 0, y: 0 }),
    });
  }
  mockedFetchWorkflowDetail.mockReset();
  mockedFetchWorkflowExportPolicy.mockReset();
  mockedFetchWorkflowTransformActions.mockReset();
  mockedFetchWorkflowContract.mockReset();
  mockedFetchWorkflowCanvasGraph.mockReset();
  mockedPreviewWorkflowTransform.mockReset();
  mockedFetchWorkflowDag.mockReset();
  mockedFetchWorkflowValidation.mockReset();
  mockedFindWorkflowLinkedProjectId.mockReset();
  mockedFetchWorkflowLinkedProjects.mockReset();
  mockedFetchWorkflowTestRunState.mockReset();
  mockedSubmitWorkflowTestRunApproval.mockReset();
  mockedTestRunWorkflow.mockReset();
  mockedTestRunWorkflowStep.mockReset();
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("");
  mockedFetchWorkflowLinkedProjects.mockResolvedValue([]);
  mockedFetchWorkflowTestRunState.mockResolvedValue({
    workflow_id: "weekly_report",
    project_id: "project_alpha",
    status: "completed",
    final_status: "success",
    final_report: "Completed successfully.",
    execution_id: "exec_approved",
  } as never);
  mockedSubmitWorkflowTestRunApproval.mockResolvedValue({
    status: "in_progress",
    thread_id: "thread_123",
  } as never);
  mockedTestRunWorkflow.mockResolvedValue({
    workflow_id: "weekly_report",
    project_id: "project_alpha",
    status: "completed",
    final_status: "success",
    final_report: "Completed successfully.",
    execution_id: "exec_123",
    outputs: { legacy_output: "ignored because workflow_outputs wins" },
    artifacts: { legacy_artifact: { artifact_id: "art_legacy" } },
    workflow_outputs: {
      outputs: {
        channel_summary: "email branch ready for launch",
        content_brief: {
          subject: "Launch email ready",
          preview_text: "Buyers preview ready",
          body: "Generated email body",
          audience: "buyers",
          cta: "Open dashboard",
        },
      },
      artifacts: {
        brief_pdf: {
          filename: "brief.pdf",
          artifact_id: "art_123",
        },
      },
    },
    trace: [
      {
        step_id: "compose_email_connector",
        step_label: "Compose Email",
        status: "success",
        step_kind: "connector",
        summary: "Created email content.",
        source_payload: {
          inputs: {
            channel: "email",
            audience: "buyers",
            request: "Draft launch email for buyers",
          },
        },
        artifacts: {
          result: "Generated email body",
          connector_result: {
            result: {
              subject: "Launch email ready",
              body: "Generated email body",
            },
          },
          content_brief: {
            subject: "Launch email ready",
            preview_text: "Buyers preview ready",
            body: "Generated email body",
          },
        },
      },
      {
        step_id: "set_channel_summary_email",
        step_label: "Set channel summary",
        status: "success",
        step_kind: "set_variable",
        summary: "set_variable wrote 2 top-level key(s)",
        artifacts: {
          assigned: ["state.channel_summary", "state.workflow_data"],
          _state_patch: {
            channel_summary: "email branch ready for launch",
            workflow_data: {
              result: "Generated email body",
            },
          },
        },
      },
      {
        step_id: "end_stop_success",
        step_label: "End workflow",
        status: "pass",
        step_kind: "stop_success",
        summary: "stop_success terminated workflow execution",
        artifacts: {
          workflow_outputs: {
            outputs: {
              channel_summary: "email branch ready for launch",
            },
          },
          workflow_terminal: {
            status: "pass",
          },
        },
      },
    ],
  } as never);
  mockedFetchWorkflowDetail.mockResolvedValue({
    workflow_id: "weekly_report",
    revision: 7,
    display_name: "Weekly Report",
    description: "Workflow-level description",
    enabled: true,
    steps: [],
    groups: [],
    settings: { execution_graph: { schema_version: 1, entry: null, edges: [] } },
  } as never);
  mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
  mockedFetchWorkflowValidation.mockResolvedValue({
    validation: { valid: true, errors: [], warnings: [] },
  } as never);
  mockedFetchWorkflowExportPolicy.mockResolvedValue({
    workflow_id: "weekly_report",
    extension_owned: false,
    protected: false,
    full_export_allowed: true,
    clone_allowed: true,
    structure_change_allowed: true,
  } as never);
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report",
    display_name: "Weekly Report",
    enabled: true,
    interface: {
      enabled: false,
      trigger_channels: [],
      input_mode: "fields",
      strict_inputs: true,
      expected_inputs: [],
      output_mode: "none",
      expected_outputs: [],
      returned_files: [],
    },
    definition_issues: [],
  } as never);
  mockedFetchWorkflowTransformActions.mockResolvedValue({
    actions: [
      {
        action_id: "text.trim",
        category: "text",
        display_name: "Trim",
        value_scope: "scalar",
        input_type: "text",
        output_type: "text",
        params_schema: { type: "object", properties: {}, additionalProperties: false },
        param_sources: {},
        pure: true,
        deterministic: true,
        io_allowed: false,
        transform_safe: true,
      },
      {
        action_id: "text.append",
        category: "text",
        display_name: "Append",
        value_scope: "scalar",
        input_type: "text",
        output_type: "text",
        params_schema: {
          type: "object",
          properties: { append_value: { type: "json" } },
          additionalProperties: false,
        },
        param_sources: {
          append_value: {
            literal: true,
            selector: true,
            current: true,
            types: ["text", "number", "boolean", "json"],
          },
        },
        pure: true,
        deterministic: true,
        io_allowed: false,
        transform_safe: true,
      },
      {
        action_id: "text.length",
        category: "text",
        display_name: "Length",
        value_scope: "scalar",
        input_type: "text",
        output_type: "number",
        params_schema: { type: "object", properties: {}, additionalProperties: false },
        param_sources: {},
        pure: true,
        deterministic: true,
        io_allowed: false,
        transform_safe: true,
      },
    ],
  } as never);
  mockedPreviewWorkflowTransform.mockResolvedValue({
    ok: true,
    errors: [],
    output: { full_name: "Ada Lovelace" },
    result_target: "state.transform_result",
    state_patch: { transform_result: { full_name: "Ada Lovelace" } },
    detail: "transform step executed",
    preview: true,
  } as never);
  mockedFetchWorkflowCanvasGraph.mockResolvedValue({
    workflow: {
      id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      callable_by_parent: false,
    },
    interface_contract: {
      trigger_channels: [],
      input_mode: "fields",
      expected_inputs: [],
      strict_inputs: true,
    },
    output_contract: {
      output_mode: "none",
      expected_outputs: [],
      returned_files: [],
    },
    entry: null,
    groups: [],
    steps: [],
    edges: [],
    validation_issues: [],
    automation_rules: [],
  } as never);
  fetchSpy.mockClear();
  fetchSpy.mockImplementation(async (input) => {
    const url = stringFromUnknownValue(input);
    if (url.includes("/export-policy")) {
      return new Response(
        JSON.stringify({
          workflow_id: "weekly_report",
          extension_owned: false,
          protected: false,
          full_export_allowed: true,
          clone_allowed: true,
          structure_change_allowed: true,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    if (url.includes("/meta/callable-workflows")) {
      return new Response(
        JSON.stringify({
          workflows: [
            {
              workflow_id: "child_workflow",
              display_name: "Child workflow",
              expected_inputs: [{ name: "city", type: "string", required: true }],
              expected_outputs: [{ name: "summary", type: "string", required: true }],
              returned_files: [{ name: "brief_pdf", type: "file", required: false }],
            },
          ],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    if (url.includes("/meta/connector-catalog")) {
      return new Response(
        JSON.stringify({
          connectors: [
            {
              connector_id: "github",
              display_name: "GitHub",
              description: "Issue tracker actions",
              source: "registry_tool",
              tool_id: "github",
              tool_url: "/registry/tools/github",
              operations: [
                {
                  operation_id: "create_issue",
                  display_name: "Create issue",
                  description: "Create a tracked issue",
                  parameters: [
                    {
                      name: "title",
                      type: "string",
                      required: true,
                      description: "Issue title",
                    },
                  ],
                  returns: {
                    name: "issue_url",
                    type: "string",
                    required: true,
                    description: "Created issue URL",
                  },
                },
              ],
              connections: [
                { connection_id: "conn_1", display_name: "Primary GitHub" },
              ],
            },
          ],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify({
        required_inputs: [],
        optional_inputs: [],
        custom_inputs: [],
        current_expected_inputs: [],
      }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      },
    );
  });
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A workflow detail of the steps/groups model, with an explicit execution graph. */
export function workflowDetailFixture({
  edges = [],
  enabled = true,
  entry,
  groups = [],
  revision = 7,
  steps = [],
  ...rest
}: {
  edges?: Array<Record<string, unknown>>;
  enabled?: boolean;
  entry?: Record<string, unknown> | null;
  groups?: Array<Record<string, unknown>>;
  revision?: number;
  steps?: Array<Record<string, unknown>>;
  [key: string]: unknown;
} = {}) {
  const firstRootStep = steps.find((step) => !step.group_id);
  return {
    workflow_id: "weekly_report",
    display_name: "Weekly Report",
    description: "Workflow-level description",
    enabled,
    revision,
    steps,
    groups,
    settings: {
      execution_graph: {
        schema_version: 1,
        entry:
          entry === undefined
            ? firstRootStep
              ? { kind: "step", id: String(firstRootStep.step_id) }
              : null
            : entry,
        edges,
      },
    },
    ...rest,
  } as never;
}

/** The canvas projection the server returns for a fixture definition. */
export function canvasGraphFixture({
  edges = [],
  enabled = true,
  entry = null,
  groups = [],
  steps = [],
  ...rest
}: {
  edges?: Array<Record<string, unknown>>;
  enabled?: boolean;
  entry?: Record<string, unknown> | null;
  groups?: Array<Record<string, unknown>>;
  steps?: Array<Record<string, unknown>>;
  [key: string]: unknown;
} = {}) {
  return {
    workflow: {
      id: "weekly_report",
      display_name: "Weekly Report",
      enabled,
      callable_by_parent: false,
    },
    interface_contract: {
      trigger_channels: [],
      input_mode: "fields",
      expected_inputs: [],
      strict_inputs: true,
    },
    output_contract: {
      output_mode: "none",
      expected_outputs: [],
      returned_files: [],
    },
    entry,
    groups,
    steps,
    edges,
    validation_issues: [],
    automation_rules: [],
    ...rest,
  } as never;
}

function changeSetBodies(): Array<Record<string, unknown>> {
  return fetchSpy.mock.calls
    .filter(([input, init]) => stringFromUnknownValue(input).endsWith("/changes") && init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body || "{}")) as Record<string, unknown>);
}

/** Every `POST /changes` body that writes (applies or saves), in order; dry runs are left out. */
export function postedChangeSets(): Array<Record<string, unknown>> {
  return changeSetBodies().filter((body) => body.dry_run !== true);
}

/** Every dry run of the local change set the page asked the server for, in order. */
export function dryRunChangeSets(): Array<Record<string, unknown>> {
  return changeSetBodies().filter((body) => body.dry_run === true);
}

/** The step bodies the page saved: config patches and staged-then-applied new steps. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- tests read nested saved fields.
export function savedStepBodies(): Array<Record<string, any>> {
  return postedChangeSets().flatMap((changeSet) =>
    ((changeSet.operations || []) as Array<Record<string, unknown>>)
      .map((operation) => {
        if (operation.op === "update_step_config") return operation.patch as Record<string, unknown>;
        if (operation.op === "create_step" || operation.op === "insert_step_between") {
          return operation.step as Record<string, unknown>;
        }
        return null;
      })
      .filter((body): body is Record<string, unknown> => Boolean(body)),
  );
}

/** The last saved step body, shaped like a fetch call so `saveCall?.[1]?.body` keeps reading it. */
export function savedStepCall(): [string, { body: string }] {
  return ["/changes", { body: JSON.stringify(savedStepBodies().at(-1) || {}) }];
}

/** The step body inside a recorded save call: the config patch or the created step. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- tests read nested saved fields.
export function stepBodyFromCall(call: unknown): Record<string, any> {
  const init = Array.isArray(call) ? (call[1] as { body?: unknown } | undefined) : undefined;
  const parsed = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as Record<string, unknown>;
  const operations = parsed.operations;
  if (!Array.isArray(operations)) {
    return parsed;
  }
  const [operation] = operations as Array<Record<string, unknown>>;
  return ((operation?.patch || operation?.step || {}) as Record<string, unknown>);
}
