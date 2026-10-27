import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { fallbackContext, I18nContext, type I18nContextValue } from "@/lib/i18n/context";
import { useAuth } from "@/lib/auth/useAuth";
import { useJobStream } from "@/lib/realtime/useJobStream";
import { JobsDetailPage } from "@/pages/jobs/detail";
import {
  fetchJobCostBreakdown,
  fetchJobDecisionOverrides,
  fetchJobDefinition,
  fetchJobDetail,
  fetchJobExecutionStepsPage,
  fetchJobApproval,
  fetchJobApprovalHistory,
  fetchJobIncident,
} from "@/pages/jobs/detail/api";

vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: vi.fn(),
}));

const mockJobStreamState = vi.hoisted(() => ({
  latestOptions: null as null | {
    enabled?: boolean;
    onMessage?: (payload: Record<string, unknown>) => void;
  },
}));
export const mockJobStream = mockJobStreamState;

vi.mock("@/lib/realtime/useJobStream", () => ({
  useJobStream: vi.fn((_jobId: string, options: { enabled?: boolean; onMessage?: (payload: Record<string, unknown>) => void }) => {
    mockJobStream.latestOptions = options;
  }),
}));

vi.mock("@/pages/jobs/detail/api", () => ({
  acknowledgeJob: vi.fn(),
  cancelJob: vi.fn(),
  fetchJobApproval: vi.fn(),
  fetchJobApprovalHistory: vi.fn(),
  fetchJobCostBreakdown: vi.fn(),
  fetchJobDecisionOverrides: vi.fn(),
  fetchJobDefinition: vi.fn(),
  fetchJobDetail: vi.fn(),
  fetchJobExecutionStepsPage: vi.fn(),
  fetchJobIncident: vi.fn(),
  restartJob: vi.fn(),
  submitApprovalDecision: vi.fn(),
  unacknowledgeJob: vi.fn(),
}));

export const mockedUseAuth = vi.mocked(useAuth);
export const mockedUseJobStream = vi.mocked(useJobStream);
export const mockedFetchJobDetail = vi.mocked(fetchJobDetail);
export const mockedFetchJobExecutionStepsPage = vi.mocked(fetchJobExecutionStepsPage);
export const mockedFetchJobIncident = vi.mocked(fetchJobIncident);
export const mockedFetchJobDecisionOverrides = vi.mocked(fetchJobDecisionOverrides);
export const mockedFetchJobCostBreakdown = vi.mocked(fetchJobCostBreakdown);
export const mockedFetchJobDefinition = vi.mocked(fetchJobDefinition);
export const mockedFetchJobApproval = vi.mocked(fetchJobApproval);
export const mockedFetchJobApprovalHistory = vi.mocked(fetchJobApprovalHistory);

export const longStepId = `step-${"abcdef1234567890".repeat(10)}`;

export function jobPayload(overrides: Record<string, unknown> = {}) {
  return {
    id: 42,
    status: "in_progress",
    task_summary: "Investigate the migrated job detail page",
    selected_workflow: "wf_weekly_report",
    selected_workflow_display_name: "Weekly Report",
    started_at: "2026-04-25T10:00:00Z",
    finished_at: null,
    current_step_id: "analysis",
    current_agent: "planner",
    cost: { tokens_total: 12 },
    diagnostics: { hidden_diagnostic_marker: "super-raw-json-hidden" },
    execution_steps: [
      {
        id: longStepId,
        title: "Analysis: Requirements",
        status: "completed",
        status_label: "Completed",
        group_id: "analysis",
        agent_label: "Planner",
        tool: "requirements",
        model: "gpt-5",
        attempt_no: 1,
        started_at: "2026-04-25T10:00:00Z",
        finished_at: "2026-04-25T10:02:00Z",
        duration_ms: 120000,
        summary: "Requirements were reviewed.",
        result: "A concise implementation brief is ready.",
        next: "Continue to verification.",
        evidence: [
          {
            title: "Requirements document",
            summary: "Open the generated requirements artifact.",
            href: "/artifacts/requirements.md",
          },
        ],
        raw: { hidden_payload_marker: "super-raw-json-hidden" },
      },
      {
        id: "step-verify",
        title: "Review: Verification",
        status: "in_progress",
        status_label: "In progress",
        group_id: "review",
        agent_label: "Reviewer",
        summary: "Verification is running.",
        result: "Checks are still active.",
      },
    ],
    ...overrides,
  };
}

export function primeApi(detail: Record<string, unknown>) {
  mockedFetchJobDetail.mockResolvedValue(detail as never);
  mockedFetchJobExecutionStepsPage.mockResolvedValue({
    job_id: "42",
    steps: [],
    page: { has_more: false, next_cursor: "" },
  } as never);
  mockedFetchJobIncident.mockResolvedValue({ job_id: 42, related_attempts: [] } as never);
  mockedFetchJobDecisionOverrides.mockResolvedValue({ job_id: 42, overrides: [] } as never);
  mockedFetchJobCostBreakdown.mockResolvedValue({ breakdown: [], total_cost_usd: 0 } as never);
  mockedFetchJobDefinition.mockResolvedValue({
    job_id: "42",
    available: true,
    workflow_id: "wf_demo",
    revision: 3,
    digest: "sha256-revision-three",
    steps: [],
    groups: [],
  } as never);
  mockedFetchJobApproval.mockResolvedValue({ job_id: 42 } as never);
  mockedFetchJobApprovalHistory.mockResolvedValue({ job_id: 42, approvals: [] } as never);
}

export function renderPage(
  props: React.ComponentProps<typeof JobsDetailPage> = {},
  i18n: I18nContextValue = fallbackContext,
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return Object.assign(render(
    <I18nContext.Provider value={i18n}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/jobs/42"]}>
        <Routes>
          <Route element={<JobsDetailPage {...props} />} path="/jobs/:jobId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
    </I18nContext.Provider>,
  ), { queryClient });
}

export function renderEmbeddedPage(jobId: string) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registry/workflows/workflow-alpha/builder"]}>
        <JobsDetailPage
          embedded
          expectedWorkflowId="wf_weekly_report"
          jobId={jobId}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockJobStream.latestOptions = null;
  mockedUseAuth.mockReturnValue({
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    status: "authenticated",
    session: {
      authenticated: true,
      roles: ["admin"],
      timezone: "Europe/Vilnius",
      user: { roles: ["admin"], timezone: "Europe/Vilnius" },
    },
  } as never);
});
