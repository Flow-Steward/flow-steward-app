import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import type { ComponentProps } from "react";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { queryKeys } from "@/lib/query/keys";
import type { JobDefinitionPayload, JobDetail, JobExecutionStepsPage } from "./api";
import {
  jobPayload,
  mockedFetchJobDefinition,
  mockedFetchJobDetail,
  mockedFetchJobExecutionStepsPage,
  primeApi,
} from "./index.test-helpers";

// Install the shared helper's API mocks before importing their real consumer.
import { JobsDetailPage, type JobDetailExecutionOwner } from "./index";

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

type Props = ComponentProps<typeof JobsDetailPage>;
const workflowId = "wf_weekly_report";
const projectId = "project-alpha";
function detail(jobId = "42", project = projectId) {
  return jobPayload({
    id: jobId, project_id: project, selected_workflow: workflowId,
    task_summary: `Recorded run ${jobId}`,
    execution_steps: [{ step_id: "prepare", workflow_step_id: "prepare", step_execution_id: `execution-${jobId}`,
      title: "Prepare", status: "in_progress", group_id: "work" }],
  }) as unknown as JobDetail;
}
function definition(jobId = "42", workflow = workflowId): JobDefinitionPayload {
  return { job_id: jobId, available: true, workflow_id: workflow, revision: 3,
    digest: `archive-${jobId}`, steps: [{ step_id: "prepare", step_kind: "transform" }], groups: [] };
}
function history(jobId = "42"): JobExecutionStepsPage {
  return { job_id: jobId, run_terminal: false, steps: [{ step_id: "prepare", step_execution_id: `execution-${jobId}`,
    status: "in_progress", group_id: "work", duration_ms: 0 }],
    page: { has_more: false, next_cursor: "" }, child_invocations: [], child_invocations_truncated: true };
}
function primeOwned(row = detail()) {
  primeApi(row as unknown as Record<string, unknown>);
  mockedFetchJobDefinition.mockResolvedValue(definition());
  mockedFetchJobExecutionStepsPage.mockResolvedValue(history());
}
function proof(jobId = "42", project = projectId, generation = 1, isCurrent = () => true): JobDetailExecutionOwner {
  return { jobId, projectId: project, workflowId, generation, isCurrent };
}
function owned(overrides: Props = {}): Props {
  return { embedded: true, jobId: "42", expectedWorkflowId: workflowId, expectedProjectId: projectId,
    executionOwner: proof(), ...overrides };
}
function mount(initial: Props, queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const tree = (props: Props) => <AuthContext.Provider value={authValue}><I18nContext.Provider value={fallbackContext}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registry/workflows/wf_weekly_report/builder"]}>
        <JobsDetailPage {...props} />
      </MemoryRouter>
    </QueryClientProvider>
  </I18nContext.Provider></AuthContext.Provider>;
  const view = render(tree(initial));
  return { ...view, queryClient, update: (props: Props) => view.rerender(tree(props)) };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("JobsDetailPage selected-owner embedding", () => {
  it.each([
    ["job", { id: "other-job" }],
    ["workflow", { selected_workflow: "other-workflow" }],
    ["project", { project_id: "other-project" }],
    ["recognized nested alias", { job: { projectId: "other-project" } }],
  ])("hides settled %s owner mismatches and publishes neither marks nor null clears", async (_label, patch) => {
    primeOwned({ ...detail(), ...patch });
    const onExecutionSnapshotChange = vi.fn();
    const onRuntimeLocationChange = vi.fn();
    const view = mount(owned({ onExecutionSnapshotChange, onRuntimeLocationChange }));

    // This is the post-response surface, not a negative assertion during loading.
    await screen.findByText("Failed to load job detail.");
    expect(view.queryClient.getQueryState(queryKeys.jobDetail("42"))?.status).toBe("success");
    expect(screen.queryByTestId("job-run-overview")).not.toBeInTheDocument();
    expect(screen.queryByText("Recorded run 42")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open full Job Details" })).not.toBeInTheDocument();
    expect(onExecutionSnapshotChange).not.toHaveBeenCalled();
    expect(onRuntimeLocationChange).not.toHaveBeenCalled();
  });

  it("requires the full parent scope and live proof for an embedded observer", async () => {
    primeOwned();
    const onExecutionSnapshotChange = vi.fn();
    const onRuntimeLocationChange = vi.fn();
    const view = mount({ embedded: true, jobId: "42", expectedWorkflowId: workflowId,
      onExecutionSnapshotChange, onRuntimeLocationChange });
    await screen.findByText("Failed to load job detail.");
    expect(onExecutionSnapshotChange).not.toHaveBeenCalled();
    expect(onRuntimeLocationChange).not.toHaveBeenCalled();
    expect(screen.queryByTestId("job-run-overview")).not.toBeInTheDocument();

    view.update(owned({ onExecutionSnapshotChange, onRuntimeLocationChange, executionOwner: proof("42", projectId, 1, () => false) }));
    expect(screen.queryByTestId("job-run-overview")).not.toBeInTheDocument();
    expect(onExecutionSnapshotChange).not.toHaveBeenCalled();
    expect(onRuntimeLocationChange).not.toHaveBeenCalled();
  });

  it("shares the disabled detail query cache but accepts only its verified owner", async () => {
    primeOwned();
    const onExecutionSnapshotChange = vi.fn();
    const onRuntimeLocationChange = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(queryKeys.jobDetail("42"), detail());
    const view = mount(owned({ detailQueryEnabled: false, onExecutionSnapshotChange, onRuntimeLocationChange }), queryClient);

    await screen.findByTestId("job-run-overview");
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
      jobId: "42", definitionWorkflowId: workflowId, definitionRevision: 3, complete: false,
      steps: [expect.objectContaining({ stepId: "prepare", durationMs: 0 })],
    })));
    expect(onRuntimeLocationChange).toHaveBeenLastCalledWith({
      stepId: "prepare", groupId: "work", definitionWorkflowId: workflowId, definitionRevision: 3,
    });
    expect(queryClient.getQueryCache().findAll({ queryKey: queryKeys.jobDetail("42"), exact: true })).toHaveLength(1);
    expect(mockedFetchJobDetail).not.toHaveBeenCalled();
    // A disabled observer reads cache; that fact alone is not owner authority.
    onExecutionSnapshotChange.mockClear();
    onRuntimeLocationChange.mockClear();
    act(() => { queryClient.setQueryData(queryKeys.jobDetail("42"), detail("42", "other-project")); });
    await screen.findByText("Failed to load job detail.");
    expect(onExecutionSnapshotChange).not.toHaveBeenCalled();
    expect(onRuntimeLocationChange).not.toHaveBeenCalled();
    expect(mockedFetchJobDetail).not.toHaveBeenCalled();
    view.unmount();
  });

  it("keeps an unavailable archive inspectable and clears only the verified current owner", async () => {
    primeOwned();
    mockedFetchJobDefinition.mockResolvedValue({ job_id: "42", available: false, unavailable_reason: "snapshot_missing" });
    const onExecutionSnapshotChange = vi.fn();
    const onRuntimeLocationChange = vi.fn();
    mount(owned({ onExecutionSnapshotChange, onRuntimeLocationChange }));

    await screen.findByTestId("job-run-definition-unavailable");
    expect(screen.getByTestId("job-run-overview")).toBeInTheDocument();
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(null));
    expect(onExecutionSnapshotChange.mock.calls.every(([snapshot]) => snapshot === null)).toBe(true);
    expect(onRuntimeLocationChange.mock.calls.every(([location]) => location === null)).toBe(true);
  });

  it("does not expose a returned foreign archive or publish its runtime owner", async () => {
    primeOwned();
    mockedFetchJobDefinition.mockResolvedValue(definition("42", "other-workflow"));
    const onExecutionSnapshotChange = vi.fn();
    const onRuntimeLocationChange = vi.fn();
    mount(owned({ onExecutionSnapshotChange, onRuntimeLocationChange }));

    await screen.findByText("Failed to load job detail.");
    expect(screen.queryByTestId("job-run-overview")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open full Job Details" })).not.toBeInTheDocument();
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(null));
    expect(onExecutionSnapshotChange.mock.calls.every(([snapshot]) => snapshot === null)).toBe(true);
    expect(onRuntimeLocationChange.mock.calls.every(([location]) => location === null)).toBe(true);
  });

  it("retires old pending snapshot and null publications across selected-owner A to B to A", async () => {
    primeOwned();
    const oldDefinition = deferred<JobDefinitionPayload>();
    const oldHistory = deferred<JobExecutionStepsPage>();
    mockedFetchJobDetail.mockImplementation(async (jobId) => detail(jobId, jobId === "43" ? "project-beta" : projectId));
    mockedFetchJobDefinition.mockImplementation((jobId) => jobId === "42" ? oldDefinition.promise : Promise.resolve(definition(jobId)));
    mockedFetchJobExecutionStepsPage.mockImplementation((jobId) => jobId === "42" ? oldHistory.promise : Promise.resolve(history(jobId)));
    let current = "A1";
    const firstSnapshot = vi.fn();
    const firstRuntime = vi.fn();
    const view = mount(owned({ executionOwner: proof("42", projectId, 1, () => current === "A1"),
      onExecutionSnapshotChange: firstSnapshot, onRuntimeLocationChange: firstRuntime }));
    await screen.findByTestId("job-run-overview");
    await waitFor(() => expect(mockedFetchJobDefinition).toHaveBeenCalledWith("42"));
    await waitFor(() => expect(mockedFetchJobExecutionStepsPage).toHaveBeenCalledWith("42", ""));

    const secondSnapshot = vi.fn();
    const secondRuntime = vi.fn();
    current = "B2";
    view.update(owned({ jobId: "43", expectedProjectId: "project-beta",
      executionOwner: proof("43", "project-beta", 2, () => current === "B2"),
      onExecutionSnapshotChange: secondSnapshot, onRuntimeLocationChange: secondRuntime }));
    await waitFor(() => expect(secondSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ jobId: "43", definitionRevision: 3 })));
    firstSnapshot.mockClear();
    firstRuntime.mockClear();
    await act(async () => { oldDefinition.resolve(definition()); oldHistory.resolve(history()); await Promise.all([oldDefinition.promise, oldHistory.promise]); });
    expect(firstSnapshot).not.toHaveBeenCalled();
    expect(firstRuntime).not.toHaveBeenCalled();
    expect(secondSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ jobId: "43" }));

    const returnedSnapshot = vi.fn();
    const returnedRuntime = vi.fn();
    current = "A3";
    view.update(owned({ executionOwner: proof("42", projectId, 3, () => current === "A3"),
      onExecutionSnapshotChange: returnedSnapshot, onRuntimeLocationChange: returnedRuntime }));
    await waitFor(() => expect(returnedSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ jobId: "42", definitionRevision: 3 })));
    expect(firstSnapshot).not.toHaveBeenCalled();
    expect(firstRuntime).not.toHaveBeenCalled();
    const callsBeforeUnmount = returnedSnapshot.mock.calls.length;
    view.unmount();
    expect(returnedSnapshot).toHaveBeenCalledTimes(callsBeforeUnmount);
    expect(secondSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ jobId: "43" }));
  });
});
