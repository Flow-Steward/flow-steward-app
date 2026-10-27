import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { componentRows, normalizeComponents, serviceVerdicts, type RuntimeComponents } from "./monitoring-data";
import { MonitoringOverview, MonitoringServices, MonitoringStatusStrip } from "./monitoring-panels";

type Row = RuntimeComponents["rows"][number];

const NOW = "2026-10-03T06:34:00Z";

function check(state: Row["health"]["state"], evidence = "self_report", freshness: "fresh" | "stale" = "fresh"): Row["health"] {
  return { state, checked_at: NOW, evidence, stale_seconds: 180, age_seconds: freshness === "fresh" ? 1 : 400, freshness };
}

const NOT_OBSERVED: Row["health"] = {
  state: "not_observed", checked_at: null, evidence: null, stale_seconds: null, age_seconds: null, freshness: "not_observed",
};

let sequence = 0;
function row(changes: Partial<Row> = {}): Row {
  sequence += 1;
  const id = `00000000-0000-4000-8000-${String(sequence).padStart(12, "0")}`;
  return {
    service_type: "app", instance_id: id, incarnation_id: id, source: "self",
    observation_kind: "process", observed_at: NOW, received_at: NOW,
    report_age_seconds: 1, receive_age_seconds: 1, effective_age_seconds: 1,
    freshness: "fresh", source_age_seconds: 1, source_stale_seconds: 180,
    source_availability: "observed", source_freshness: "fresh", lifecycle: "running",
    readiness: NOT_OBSERVED, health: check("healthy"), consumer_state: null, conflicting: false,
    started_at: NOW, superseded: false,
    ...changes,
  };
}

function components(rows: Row[], changes: Partial<RuntimeComponents> = {}): RuntimeComponents {
  const processes = rows.filter((item) => item.observation_kind === "process" && !item.superseded);
  const fresh = processes.filter((item) => item.freshness === "fresh" && item.source_freshness === "fresh");
  return {
    installation_id: "fixture", deployment_profile: "scalable", available: true,
    channel: { state: "connected" }, completeness: null, expected_gaps: [],
    counts: {
      roles_total: new Set(processes.map((item) => item.service_type)).size,
      roles_observed: new Set(fresh.map((item) => item.service_type)).size,
      instances_total: processes.length, instances_fresh: fresh.length,
      instances_stale: processes.length - fresh.length, instances_ready: 0,
      instances_evidenced_healthy: fresh.filter((item) => item.health.state === "healthy" && item.health.freshness === "fresh").length,
      dependencies_total: rows.filter((item) => item.observation_kind === "dependency").length,
      instances_superseded: rows.filter((item) => item.superseded).length,
    },
    rows,
    ...changes,
  };
}

function services(value: RuntimeComponents) {
  render(<MemoryRouter><MonitoringServices components={value} checks={[]} loading={false} /></MemoryRouter>);
}

describe("Monitoring services", () => {
  it("shows one verdict per role with its readable name and the evidence behind it", () => {
    services(components([
      row(),
      row({ service_type: "worker", consumer_state: "active", health: check("healthy", "consumer_state") }),
      row({ service_type: "worker", consumer_state: "active", health: check("healthy", "consumer_state") }),
    ]));

    const table = screen.getByRole("table", { name: "Services" });
    expect(within(table).getByText("Web app")).toBeInTheDocument();
    expect(within(table).getByText("Workers")).toBeInTheDocument();
    expect(within(table).getAllByText("Healthy")).toHaveLength(2);
    expect(table).toHaveTextContent("2 running");
    expect(table).toHaveTextContent("Healthy by its queue consumer");
    expect(table).toHaveTextContent("Healthy by the process's own report");
    expect(within(table).queryByText(/00000000-0000/)).not.toBeInTheDocument();
  });

  it("puts problems first and names them", () => {
    services(components([
      row(),
      row({ service_type: "control", health: check("unhealthy") }),
      row({ service_type: "installer", freshness: "stale", source_freshness: "stale", health: check("healthy", "self_report", "stale") }),
    ], {
      expected_gaps: [
        { service_type: "mcp", desired_count: 1, observed_count: 0, observation_kind: "process", reason: "missing" },
        { service_type: "worker", desired_count: 2, observed_count: 1, observation_kind: "process", reason: "replica_target_unobserved" },
      ],
    }));

    const rows = within(screen.getByRole("table", { name: "Services" })).getAllByRole("row").slice(1);
    const titles = rows.map((item) => item.textContent ?? "");
    expect(titles[0]).toContain("Control plane");
    expect(titles[0]).toContain("Unhealthy");
    const text = titles.join(" | ");
    expect(text).toContain("MCP server");
    expect(text).toContain("Not reporting");
    expect(text).toContain("0 of 2 running");
    expect(text.indexOf("Web app")).toBeGreaterThan(text.indexOf("Not reporting"));
  });

  it("says when a role runs but reports no health check", () => {
    services(components([row({ service_type: "image-indexer", health: NOT_OBSERVED })]));

    const table = screen.getByRole("table", { name: "Services" });
    expect(within(table).getByText("Image Indexer")).toBeInTheDocument();
    expect(within(table).getByText("Running · health not measured")).toBeInTheDocument();
    expect(table).toHaveTextContent("No health check reported");
  });

  it("lists dependencies with their checks, and an unreachable one as a problem", () => {
    services(components([
      row({ service_type: "postgres", source: "control.postgres", observation_kind: "dependency", health: check("healthy", "dependency_probe") }),
      row({ service_type: "rabbitmq", source: "control.rabbitmq", observation_kind: "dependency", health: check("unhealthy", "dependency_probe") }),
    ]));

    const table = screen.getByRole("table", { name: "Dependencies" });
    const rows = within(table).getAllByRole("row").slice(1).map((item) => item.textContent ?? "");
    expect(rows[0]).toContain("RabbitMQ");
    expect(rows[0]).toContain("Unreachable");
    expect(rows[1]).toContain("PostgreSQL");
    expect(rows[1]).toContain("Healthy by a connection probe");
  });

  it("keeps past process starts out of the verdicts, in a collapsed history", () => {
    services(components([
      row(),
      row({ freshness: "stale", source_freshness: "stale", superseded: true, health: check("healthy", "self_report", "stale") }),
      row({ freshness: "stale", source_freshness: "stale", superseded: true, health: check("healthy", "self_report", "stale") }),
    ]));

    const table = screen.getByRole("table", { name: "Services" });
    expect(within(table).getByText("Healthy")).toBeInTheDocument();
    expect(table).toHaveTextContent("1 running");
    expect(screen.getByText("History: past process starts (2)")).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "History" })).getAllByText("Replaced")).toHaveLength(2);
  });

  it("keeps a replica that went silent while another runs as a current problem", () => {
    services(components([
      row({ service_type: "worker", consumer_state: "active", health: check("healthy", "consumer_state") }),
      row({ service_type: "worker", freshness: "stale", source_freshness: "stale", consumer_state: "active", health: check("healthy", "consumer_state", "stale") }),
    ]));

    const table = screen.getByRole("table", { name: "Services" });
    expect(within(table).getByText("1 not reporting")).toBeInTheDocument();
    expect(screen.getByText("History: past process starts (0)")).toBeInTheDocument();
  });

  it("returns no verdicts when observations are unavailable", () => {
    expect(serviceVerdicts(null, "UTC")).toEqual({ services: [], dependencies: [] });
  });
});

describe("Monitoring overview and status strip", () => {
  it("shows healthy running instances out of all running ones", () => {
    const value = components([row(), row({ health: NOT_OBSERVED })]);
    render(<MemoryRouter>
      <MonitoringStatusStrip components={value} meta={{}} ready={true} summaryUnavailable={false} totals={{}} />
      <MonitoringOverview components={value} checks={[]} checksFailed={0} eventCount={0} loading={false} meta={{}} summaryUnavailable={false} />
    </MemoryRouter>);

    expect(screen.getByRole("link", { name: "Healthy instances: 1 of 2. Open services" })).toBeInTheDocument();
    expect(screen.getByText("Running instances: 2")).toBeInTheDocument();
    expect(screen.getByText("Not reporting: 0")).toBeInTheDocument();
  });

  it("says health is not measured only when no running process reported a check", () => {
    const value = components([row({ health: NOT_OBSERVED })]);
    render(<MemoryRouter><MonitoringOverview components={value} checks={[]} checksFailed={0} eventCount={0} loading={false} meta={{}} summaryUnavailable={false} /></MemoryRouter>);

    expect(screen.getByRole("link", { name: "Healthy instances: not measured. Open services" })).toHaveTextContent(/No running process reported a health check/);
  });

  it("accepts observations from a server that does not send start times yet", () => {
    const legacy = components([row()]);
    const rest: Partial<Row> = { ...legacy.rows[0] };
    delete rest.started_at;
    delete rest.superseded;
    const parsed = normalizeComponents({ ...legacy, rows: [rest], counts: { ...legacy.counts, instances_superseded: undefined } });

    expect(parsed).not.toBeNull();
    expect(serviceVerdicts(parsed, "UTC").services[0].status).toBe("Healthy");
  });

  it("formats raw observation rows with readable names and the user's timezone", () => {
    const formatted = componentRows(components([row()]), "Europe/Vilnius")[0];
    expect(formatted.title).toBe("Web app");
    expect(formatted.detail).toContain("9:34");
    expect(formatted.meta).toContain("Instance: 00000000-0000-4000-8000");
  });
});
