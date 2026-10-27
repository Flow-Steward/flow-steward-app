import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { MonitoringPage } from "@/pages/monitoring";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import {
  cleanupMonitoringStorageOrphan,
  fetchMonitoringStorage,
  fetchMonitoringSummary,
} from "@/pages/monitoring/api";

vi.mock("@/pages/monitoring/shared/events", () => ({
  EventsAuditTrail: () => <div>Events audit trail</div>,
}));

vi.mock("@/pages/monitoring/shared/processes", () => ({
  ProcessesControlPanel: () => <div>Processes control panel</div>,
}));

vi.mock("@/pages/monitoring/api", () => ({
  cleanupMonitoringStorageOrphan: vi.fn(),
  fetchMonitoringSummary: vi.fn(),
  fetchMonitoringStorage: vi.fn(),
}));

const mockedCleanupMonitoringStorageOrphan = vi.mocked(cleanupMonitoringStorageOrphan);
const mockedFetchMonitoringSummary = vi.mocked(fetchMonitoringSummary);
const mockedFetchMonitoringStorage = vi.mocked(fetchMonitoringStorage);

function renderPage(route = "/monitoring?tab=services", locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  const messages: Record<string, string> = locale === "lt" ? { "react.monitoring.download_diagnostics": "Atsisiųsti diagnostiką" } : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <MonitoringPage />
      </MemoryRouter>
    </QueryClientProvider>
    </I18nContext.Provider>,
  );
}

function storedComponents(serviceType = "image-indexer", freshness: "fresh" | "stale" = "stale") {
  const checked = "2026-10-01T12:00:00Z";
  return {
    installation_id: "test",
    deployment_profile: "scalable",
    available: true,
    channel: { state: "connected", observed_at: checked, age_seconds: 1 },
    rows: [{
      service_type: serviceType,
      instance_id: "9ee9238d-23e9-4f78-847d-d5652e5b521b",
      incarnation_id: "9ee9238d-23e9-4f78-847d-d5652e5b521b",
      source: "self",
      observation_kind: "process",
      observed_at: checked,
      received_at: checked,
      report_age_seconds: freshness === "stale" ? 181 : 1,
      receive_age_seconds: freshness === "stale" ? 181 : 1,
      effective_age_seconds: freshness === "stale" ? 181 : 1,
      freshness,
      source_age_seconds: freshness === "stale" ? 181 : 1,
      source_stale_seconds: 180,
      source_availability: "observed",
      source_freshness: freshness,
      lifecycle: "running",
      readiness: { state: "ready", checked_at: checked, evidence: "probe", stale_seconds: 180, age_seconds: freshness === "stale" ? 181 : 1, freshness },
      health: { state: "healthy", checked_at: checked, evidence: "probe", stale_seconds: 180, age_seconds: freshness === "stale" ? 181 : 1, freshness },
      consumer_state: null,
      conflicting: false,
    }],
    expected_gaps: [],
    counts: { roles_total: 1, roles_observed: 1, instances_total: 1, instances_fresh: freshness === "fresh" ? 1 : 0,
      instances_stale: freshness === "stale" ? 1 : 0, instances_ready: freshness === "fresh" ? 1 : 0,
      instances_evidenced_healthy: freshness === "fresh" ? 1 : 0, dependencies_total: 0 },
    completeness: null,
  };
}

describe("MonitoringPage", () => {
  it("localizes the diagnostics download action", async () => {
    renderPage("/monitoring", "lt");
    expect(await screen.findByRole("button", { name: "Atsisiųsti diagnostiką" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download diagnostics" })).not.toBeInTheDocument();
  });
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchMonitoringSummary.mockResolvedValue({
      ready: true,
      summary: {
        checks_total: 2,
        checks_ok: 1,
        checks_failed: 1,
      },
      checks: {
        postgres: { ok: true, latency_ms: 12 },
        rabbitmq: { ok: false, error: "Connection refused" },
      },
      containers: {
        available: true,
        checked_at: "2026-04-27T05:00:00Z",
        docker_socket_available: true,
        docker_cli_available: true,
        running_services: 1,
        service_count: 2,
        services: [
          {
            service: "app-service",
            title: "App",
            category: "application",
            required: true,
            present: true,
            running_instances: 1,
            instance_count: 1,
            state_summary: "running",
            status_summary: "Up 2 hours",
            tone: "green",
          },
          {
            service: "worker-service",
            title: "Workers",
            category: "application",
            required: true,
            present: false,
            running_instances: 0,
            instance_count: 0,
            state_summary: "absent",
            status_summary: "No container detected",
            tone: "red",
          },
        ],
      },
      meta: {
        version: "0.1.0",
        extension_runtime_target: "linux-arm64",
        queue_delivery: {
          state: "retrying_delivery",
          state_label: "Retrying delivery",
          state_message: "RabbitMQ delivery is retrying before execution has safely started.",
          queued_pending_jobs: 3,
          retrying_delivery_jobs: 1,
          dead_lettered_jobs: 0,
          post_claim_execution_failed_jobs: 0,
          requires_intervention: false,
        },
        watchdog_enabled: true,
        watchdog_auto_apply: false,
        ready_queue_depth: 0,
        active_leases: 0,
      },
    });
    mockedCleanupMonitoringStorageOrphan.mockResolvedValue({
      status: "deleted",
      message: "Orphaned storage scope deleted.",
    });
    mockedFetchMonitoringStorage.mockResolvedValue({
      backend_matrix: {
        vector: "qdrant",
      },
      summary: {
        orphan_memory_projects: 1,
      },
      memory: {
        rows: [{ project_id: "p1", project_name: "Demo", project_state: "active", records: 3, entities: 2, edges: 1 }],
        orphan_rows: [
          {
            project_id: "ghost",
            storage_scope: "ghost",
            project_state: "missing",
            records: 2,
            entities: 0,
            edges: 0,
            cleanup_supported: true,
            cleanup_confirm: "DELETE_ORPHAN_STORAGE",
            resolution_hint: "No project row exists for this memory project id. Clean this orphaned scope.",
          },
        ],
      },
      vector: {
        backend: "qdrant",
        count_mode: "estimated_from_embeddings",
        rows: [{ project_id: "p1", project_name: "Demo", project_state: "active", records: 3 }],
        orphan_rows: [],
      },
    });
  });

  it.each([["image-indexer", "Image Indexer"], ["new-generic-runtime", "New Generic Runtime"]])("shows %s by a readable name and its original last heartbeat without service-name branches", async (name, label) => {
    mockedFetchMonitoringSummary.mockResolvedValueOnce({ ready: true, checks: {},
      summary: { checks_total: 0, checks_ok: 0, checks_failed: 0 }, components: storedComponents(name) });
    renderPage();
    const services = await screen.findByRole("table", { name: "Services" });
    expect(await within(services).findByText(label)).toBeInTheDocument();
    expect(within(services).getByText("Not reporting")).toBeInTheDocument();
    const current = screen.getByRole("table", { name: "Current observations" });
    expect(within(current).getByText(/Last heartbeat:/)).toBeInTheDocument();
    expect(screen.queryByText(/Last heartbeat: 2026-10-01T/)).not.toBeInTheDocument();
    expect(within(current).getByText(/Report age: 181s/)).toBeInTheDocument();
    expect(within(current).getByText("Stale")).toBeInTheDocument();
    expect(screen.getByText(/Replica completeness: unknown/)).toBeInTheDocument();
    expect(screen.getByText("Services: 1")).toBeInTheDocument();
    expect(screen.getByText("Not reporting: 1")).toBeInTheDocument();
  });

  it("does not make Docker absence a product-health failure", async () => {
    mockedFetchMonitoringSummary.mockResolvedValueOnce({ ready: true, checks: {},
      summary: { checks_total: 0, checks_ok: 0, checks_failed: 0 }, components: storedComponents("image-indexer", "fresh"),
      containers: { available: false, services: [] } });
    renderPage("/monitoring");
    const metric = await screen.findByRole("link", { name: "Healthy instances: 1 of 1. Open services" });
    expect(metric).toBeInTheDocument();
    expect(screen.getByText("Running instances: 1")).toBeInTheDocument();
    expect(screen.queryByText("Docker socket")).not.toBeInTheDocument();
  });

  it("shows readable heartbeat age and expandable full diagnostics", async () => {
    const components = storedComponents("image-indexer", "fresh");
    components.rows[0].report_age_seconds = 33.751256;
    mockedFetchMonitoringSummary.mockResolvedValueOnce({ components });
    renderPage();
    const current = await screen.findByRole("table", { name: "Current observations" });
    expect(await within(current).findByText(/Report age: 34s/)).toBeInTheDocument();
    expect(screen.queryByText(/33\.751256/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Last heartbeat: 2026-10-01T/)).not.toBeInTheDocument();
    const disclosure = within(current).getByRole("group", { name: "Image Indexer observation diagnostics" });
    expect(disclosure).not.toHaveAttribute("open");
    expect(disclosure).toHaveTextContent("Readiness: Ready");
    expect(disclosure).toHaveTextContent("Health: Healthy");
  });

  it("keeps an expired cached check distinct from a fresh heartbeat", async () => {
    const components = storedComponents("image-indexer", "fresh");
    components.rows[0].health.freshness = "stale";
    components.rows[0].health.age_seconds = 181;
    components.counts.instances_evidenced_healthy = 0;
    mockedFetchMonitoringSummary.mockResolvedValueOnce({ ready: true, components });
    renderPage();
    const current = await screen.findByRole("table", { name: "Current observations" });
    expect(await within(current).findByText("Cached check stale")).toBeInTheDocument();
    expect(within(current).getByRole("group", { name: "Image Indexer observation diagnostics" })).toHaveTextContent(/Health: Healthy \(stale; checked .*; age 181s/);
    // A stale health check is not a verdict: the role runs, its health is unknown.
    expect(within(screen.getByRole("table", { name: "Services" })).getByText("Running · health not measured")).toBeInTheDocument();
    expect(screen.getByText("Failed checks: unknown")).toBeInTheDocument();
  });

  it("shows missing roles and unobserved dependencies separately without fake instances", async () => {
    const components = { ...storedComponents("image-indexer", "fresh"), completeness: false,
      expected_gaps: [
        { service_type: "new-worker-role", desired_count: 2, observed_count: 0, observation_kind: "process", reason: "missing" },
        { service_type: "external-object-store", desired_count: null, observed_count: 0, observation_kind: "dependency", reason: "not_observed" },
      ] };
    components.counts.roles_total = 2;
    components.counts.dependencies_total = 1;
    mockedFetchMonitoringSummary.mockResolvedValueOnce({ components });
    renderPage();
    const services = await screen.findByRole("table", { name: "Services" });
    expect(await within(services).findByText("New Worker Role")).toBeInTheDocument();
    expect(within(services).getByText("Not reporting")).toBeInTheDocument();
    const dependencies = screen.getByRole("table", { name: "Dependencies" });
    expect(within(dependencies).getByText("External Object Store")).toBeInTheDocument();
    expect(dependencies).toHaveTextContent("No check has reported this dependency yet");
    expect(screen.getByText("Running instances: 1")).toBeInTheDocument();
    expect(screen.getByText("Replica completeness: incomplete")).toBeInTheDocument();
  });

  it("renders explicit failures without treating lifecycle running as healthy", async () => {
    const components = storedComponents("image-indexer", "fresh");
    components.rows[0].health.state = "unhealthy";
    components.counts.instances_evidenced_healthy = 0;
    mockedFetchMonitoringSummary.mockResolvedValueOnce({ components });
    renderPage();
    const services = await screen.findByRole("table", { name: "Services" });
    expect(await within(services).findByText("Unhealthy")).toBeInTheDocument();
    const current = screen.getByRole("table", { name: "Current observations" });
    expect(within(current).getByText("Failed")).toBeInTheDocument();
    expect(within(current).getByText(/Lifecycle: Running/)).toBeInTheDocument();
  });

  it.each([{ available: false, counts: null }, { available: true, rows: "malformed", counts: { instances_total: "bad" } }])("renders unavailable or malformed component evidence as unknown", async (components) => {
    mockedFetchMonitoringSummary.mockResolvedValueOnce({ ready: true, checks: {},
      summary: { checks_total: 0, checks_ok: 0, checks_failed: 0 }, components });
    renderPage("/monitoring");
    expect(await screen.findByRole("link", { name: "Healthy instances: unknown. Open services" })).toBeInTheDocument();
    expect(screen.getByText("Services: unknown")).toBeInTheDocument();
    expect(screen.getByText("Running instances: unknown")).toBeInTheDocument();
  });

  it("does not keep old runtime/watchdog tab aliases", async () => {
    renderPage("/monitoring?tab=runtime");

    expect(await screen.findByRole("heading", { name: "System monitoring" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Runtime" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Watchdog" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("renders real service and check rows instead of container metadata as unknown services", async () => {
    renderPage();

    const servicesPanel = await screen.findByRole("heading", { name: "Services" });
    expect(servicesPanel).toBeInTheDocument();
    expect(screen.queryByText("App")).not.toBeInTheDocument();

    expect(screen.queryByRole("heading", { name: "Legacy container diagnostics" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Container discovery:/)).not.toBeInTheDocument();
    expect(screen.queryByText("Container inventory")).not.toBeInTheDocument();
    expect(screen.getByText("Services: unknown")).toBeInTheDocument();
    expect(screen.queryByText("Workers")).not.toBeInTheDocument();
    expect(await screen.findByText("Postgres")).toBeInTheDocument();
    expect(screen.getByText("Rabbitmq")).toBeInTheDocument();
    expect(screen.getByText("Connection refused")).toBeInTheDocument();

    expect(screen.queryByText("available")).not.toBeInTheDocument();
    expect(screen.queryByText("checked_at")).not.toBeInTheDocument();

    await waitFor(() => {
      expect(screen.queryByText("UNKNOWN")).not.toBeInTheDocument();
    });
  });

  it("does not present container discovery as product monitoring", async () => {
    mockedFetchMonitoringSummary.mockResolvedValueOnce({
      ready: true,
      summary: { checks_total: 2, checks_ok: 2, checks_failed: 0 },
      checks: {
        postgres: { ok: true },
        rabbitmq: { ok: true },
      },
      containers: {
        available: false,
        docker_socket_available: false,
        error: "Docker socket is not mounted",
        services: [
          {
            service: "app-service",
            title: "App",
            required: true,
            present: false,
            state_summary: "absent",
            status_summary: "No container detected",
            tone: "red",
          },
        ],
      },
      meta: {},
    });

    renderPage("/monitoring?tab=overview");

    expect(await screen.findByText("Ready: yes")).toBeInTheDocument();
    expect(screen.queryByText(/Container discovery:/)).not.toBeInTheDocument();
    expect(screen.getByText("Healthy instances")).toBeInTheDocument();
    expect(screen.getByText("Unknown")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Services" }));
    expect(screen.queryByText("App")).not.toBeInTheDocument();
    expect(screen.getByText("No service has reported yet.")).toBeInTheDocument();
    expect(screen.queryByText("Missing")).not.toBeInTheDocument();
    expect(screen.queryByText("No container detected")).not.toBeInTheDocument();
  });

  it("shows the extension runtime target beside the platform version", async () => {
    renderPage();

    const status = await screen.findByRole("group", { name: "System status" });
    await within(status).findByText("0.1.0");
    expect(within(status).getByText("Version", { exact: false })).toBeInTheDocument();
    expect(within(status).getByText("Runtime target", { exact: false })).toBeInTheDocument();
    expect(within(status).getByText("linux-arm64")).toBeInTheDocument();
  });

  it("downloads the installation diagnostic report from the page header", async () => {
    const clicked: string[] = [];
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.href);
    });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Download diagnostics" }));

    expect(clicked).toHaveLength(1);
    const url = new URL(clicked[0]);
    expect(url.pathname).toBe("/api/system/diagnostics/report");
    expect(url.searchParams.get("download_token")).toMatch(/^[A-Za-z0-9]{32}$/);
    expect(screen.getByRole("button", { name: "Preparing report…" })).toBeDisabled();
    clickSpy.mockRestore();
  });

  it("renders the storage tab with backend and orphan visibility", async () => {
    renderPage("/monitoring?tab=storage");

    expect(await screen.findByRole("heading", { name: "Storage summary" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Storage" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Backend inventory")).toBeInTheDocument();
    expect(screen.getByText("Orphaned scope data")).toBeInTheDocument();
    expect(await screen.findByText("qdrant")).toBeInTheDocument();
    expect(await screen.findByText("ghost", { selector: "code" })).toBeInTheDocument();
  });

  it("requests orphan storage cleanup for missing scopes", async () => {
    renderPage("/monitoring?tab=storage");

    const cleanupButton = await screen.findByRole("button", { name: "Clean orphan data" });
    fireEvent.click(cleanupButton);
    expect(mockedCleanupMonitoringStorageOrphan).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/Delete orphaned memory storage scope ghost/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete orphan data" }));

    await waitFor(() => {
      expect(mockedCleanupMonitoringStorageOrphan.mock.calls[0]?.[0]).toEqual({
        backend: "memory",
        scope: "ghost",
        confirm: "DELETE_ORPHAN_STORAGE",
      });
    });
    expect(await screen.findByText("Orphaned storage scope deleted.")).toBeInTheDocument();
  });

  it("does not clean orphan storage when the confirmation is cancelled", async () => {
    renderPage("/monitoring?tab=storage");

    fireEvent.click(await screen.findByRole("button", { name: "Clean orphan data" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    expect(mockedCleanupMonitoringStorageOrphan).not.toHaveBeenCalled();
  });

  it("summarizes nested overview metadata instead of dumping recursive runtime details", async () => {
    renderPage("/monitoring");

    expect(await screen.findByText("Queue delivery")).toBeInTheDocument();
    expect(screen.getByText("Retrying delivery")).toBeInTheDocument();
    expect(screen.getByText("RabbitMQ delivery is retrying before execution has safely started.")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("Retrying")).toBeInTheDocument();
    expect(screen.queryByText("queue_delivery")).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /Ready/i })).not.toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });

  it("shows queue delivery jobs that need operator action", async () => {
    const jobId = "35f20236-be77-4124-aad9-f828c383e70c";
    mockedFetchMonitoringSummary.mockResolvedValueOnce({
      ready: true,
      summary: {
        checks_total: 0,
        checks_ok: 0,
        checks_failed: 0,
      },
      checks: {},
      containers: {
        available: false,
        services: [],
      },
      meta: {
        queue_delivery: {
          state: "idle",
          state_label: "Idle",
          state_message:
            "No active delivery backlog is waiting right now, but one or more dead-lettered jobs still require operator intervention.",
          queued_pending_jobs: 0,
          retrying_delivery_jobs: 0,
          dead_lettered_jobs: 2,
          post_claim_execution_failed_jobs: 0,
          requires_intervention: true,
          attention_job_count: 2,
          operator_action:
            "Open a listed job, fix the referenced project or workflow issue, then restart it or acknowledge it when no rerun is needed.",
          attention_jobs: [
            {
              job_id: jobId,
              detail_url: `/jobs/${jobId}`,
              attention_kind: "dead_lettered",
              attention_label: "Dead-lettered / requires intervention",
              project_name: "Nightfire Project Updated c144",
              project_state: "trashed",
              selected_workflow: "sample_webhook_router",
              failure_code: "project_not_found",
              failure_message: "Queue payload references a project that no longer exists.",
              dead_lettered_at: "2026-07-01T10:02:00+00:00",
              retry_count: 2,
              retry_max_attempts: 2,
              resolution_hint:
                "Referenced project is in trash. Restore the project if this job should run, then restart it; acknowledge the job if the deletion was intentional.",
            },
          ],
        },
      },
    });

    renderPage("/monitoring");

    expect(await screen.findByText("Needs operator action")).toBeInTheDocument();
    expect(screen.getByText("Ready: yes")).toBeInTheDocument();
    expect(screen.getByText("Showing 1 of 2 jobs")).toBeInTheDocument();
    expect(screen.getByText("Dead-lettered / requires intervention")).toBeInTheDocument();
    expect(screen.getByText("Job 35f20236...e70c")).toBeInTheDocument();
    expect(screen.getByText("sample_webhook_router")).toBeInTheDocument();
    expect(screen.getByText("Nightfire Project Updated c144")).toBeInTheDocument();
    expect(screen.getByText("project_not_found")).toBeInTheDocument();
    expect(screen.getByText("Queue payload references a project that no longer exists.")).toBeInTheDocument();
    expect(screen.getByText(/Restore the project if this job should run/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: `Open job ${jobId}` })).toHaveAttribute("href", `/jobs/${jobId}`);
  });
});
