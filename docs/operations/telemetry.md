# Telemetry and external monitoring

Flow Steward provides its own runtime monitoring, job logs and Prometheus-format
metrics. Normal operation does not require a LangSmith account, an OpenTelemetry
Collector or a hosted monitoring service. External monitoring is optional.

## Supported interfaces

| Interface | Current support |
| --- | --- |
| Runtime heartbeat monitoring | Built in; RabbitMQ transports observations to PostgreSQL for the Monitoring UI. See the [runtime monitoring contract](../contracts/runtime-monitoring.md). |
| Prometheus metrics | Built in; authenticated HTTP pull endpoints using Prometheus text format `0.0.4`. |
| Runtime logs | Local logging supports text and JSON through `FS_LOG_FORMAT`. |
| OpenTelemetry traces, metrics and logs over OTLP | Optional and off by default. OTLP/HTTP Protobuf export, enabled separately for each signal; see [OpenTelemetry export](#opentelemetry-export). |
| LangSmith tracing | No supported first-party configuration or project routing. LangSmith can receive the OTLP traces, which carry no prompts or completions. |
| Sentry | Optional separate integration, enabled by a configured `SENTRY_DSN`; without a DSN it is disabled. It is independent of the OTLP export. |

The accurate capability statement is: **built-in monitoring and
Prometheus-compatible metrics, with optional OpenTelemetry export and no
required external observability service**.

## Collect metrics with Prometheus

Set `FS_METRICS_TOKEN` through the installation's runtime configuration and restart
the affected services. The endpoint accepts either
`Authorization: Bearer <token>` or `X-Metrics-Token: <token>`.
The application route also accepts an authenticated admin/operator; unattended
scrapers should use the metrics token. Process endpoints require a nonempty token.

| Runtime | Endpoint and reachability |
| --- | --- |
| Application | `GET /metrics` on the application HTTP service, available through the installation's public origin. |
| Scalable worker | `GET /metrics` on `FS_WORKER_METRICS_PORT` (Compose default `9101`). |
| Scalable control | `GET /metrics` on `FS_CONTROL_METRICS_PORT` (Compose default `9102`). |
| Compact control/workers | Internal loopback endpoints: control `9102`, workers `9200` upwards. The supervisor assigns these ports; they are not published for an external scraper. Use the application endpoint for external collection. |

For Scalable process exporters, `FS_PROCESS_METRICS_HOST` defaults to `0.0.0.0`;
a port of zero disables a standalone process exporter. Compose exposes worker
and control ports to the container network rather than publishing them on the
host. Run the scraper on a reachable private network. Configure a target for
each replica when collecting per-process metrics.

For example, configure an independently operated Prometheus server to scrape
the application over HTTPS. Replace the example hostname and mount a file
containing the metrics token into the Prometheus container:

```yaml
scrape_configs:
  - job_name: flow-steward-app
    scheme: https
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /run/secrets/fs_metrics_token
    static_configs:
      - targets: ["flow-steward.example.com:443"]
```

The token file is read by Prometheus; it must contain the same value configured
in Flow Steward. Prometheus does not substitute shell variables into this YAML.
Keep tokens out of committed configuration and use TLS outside trusted private
networks. Prometheus and its configuration are not bundled by this example.

Application metrics include `platform_health_ready`, `platform_readiness_check`,
`platform_jobs_total` (current job counts by status), `platform_ready_queue_depth`
and 24-hour SLO gauges. Despite its name, `platform_jobs_total` is a gauge, not a
monotonic event counter. A queue depth of `-1` means unavailable. Process
endpoints include `platform_process_up`, process identity, start time, thread
count and CPU time, with additional worker/control diagnostics. Application and
process metrics have different coverage; a successful scrape is not proof that
every runtime component is healthy. An empty application response can mean its
database logger is unavailable.

## OpenTelemetry export

The app, workers, control plane and `fs-mcp` can export traces, metrics and
logs to one OTLP/HTTP Protobuf receiver per signal, usually a self-hosted
OpenTelemetry Collector. Export is off by default and has no default endpoint.
Flow Steward does not install a Collector. To send a signal to several
destinations, route it in your Collector.

Export never blocks workflows or process shutdown. Buffers and timeouts are
bounded; when the receiver is unavailable, telemetry is dropped.

### Configuration

Enable a signal by setting its exporter to `otlp` and giving it an endpoint:

```bash
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

`OTEL_EXPORTER_OTLP_ENDPOINT` is a base URL: `/v1/traces`, `/v1/metrics` and
`/v1/logs` are appended. `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` overrides it with
a full URL for one signal.

Put the settings in the installation's `.env.prod`; the installer keeps them
across later runs and delivers them to every role. To apply a change:

- Compact: rerun `./install.sh`.
- Scalable: run `./flow-steward stack stop --confirm`, then `./install.sh`. A
  rerun of `./install.sh` alone recreates only the workers, so the app, the
  control plane and `fs-mcp` would keep their previous settings.

| Setting | Meaning |
| --- | --- |
| `OTEL_{TRACES,METRICS,LOGS}_EXPORTER` | `otlp` enables the signal; `none` or unset disables it. |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_ENDPOINT` | `http` or `https` receiver URL. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | Only `http/protobuf`; any other value disables export. |
| `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_HEADERS` | Receiver headers, for example an API key. Treated as secrets. |
| `OTEL_EXPORTER_OTLP_TIMEOUT`, `OTEL_EXPORTER_OTLP_COMPRESSION` | Request timeout (default 10 s) and `none` or `gzip`. |
| `OTEL_EXPORTER_OTLP_CERTIFICATE`, `OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE`, `OTEL_EXPORTER_OTLP_CLIENT_KEY` | TLS trust and client certificate files. |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` (default), `parentbased_always_off` or `parentbased_traceidratio` with a ratio. |
| `OTEL_BSP_*`, `OTEL_BLRP_*` | Span and log batch limits (queue 2048, batch 512, delay 5 s). |
| `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_METRIC_EXPORT_TIMEOUT` | Metric export period (default 60 s) and timeout. |
| `OTEL_RESOURCE_ATTRIBUTES` | Only `deployment.environment.name` is accepted. |
| `OTEL_SDK_DISABLED` | `true` disables every signal. |

Other `OTEL_*` variables are removed from the process environment at start-up,
including the `OTEL_PYTHON_LOG_*` settings, auto-instrumentation and resource
detector settings, and any setting that enables GenAI content capture. The HTTP
semantic-conventions mode is pinned to the stable `http` conventions.

Each role reports `service.name` as `flow-steward-app`, `flow-steward-worker`,
`flow-steward-control` or `flow-steward-mcp`, with `service.version` and an
SDK-generated `service.instance.id`. The CLI, the extension installer and
extension subprocesses are not instrumented.

### Traces

| Span | Where |
| --- | --- |
| HTTP server span | Every API request except WebSocket, health and `/metrics` routes. Named by the route template, for example `GET /api/v2/projects/{project_id}/jobs/{job_id}`; an unmatched request is named by its method alone. |
| `fs.workflow.segment` | One active run of a job: from a start or resume to completion, failure or pause. |
| `fs.workflow.step` | One attempt of a workflow step, including steps of parallel lanes. |
| `chat {model}` | One model call (CLIENT). Carries the provider category, purpose, requested and response model, and token counts. |
| `execute_tool {tool}` | One agent tool call. |
| MCP spans | `fs-mcp` server spans, the MCP SDK client spans of the first-party `fs-mcp` path, and a `mcp {method}` span around each call to an external MCP server. |

A job carries its trace context from where it was created, so the worker's
segment continues the API request, webhook or MCP call that started it, and a
child workflow continues the step that invoked it. The context is stored with
the job, so it survives redelivery and recovery, and every resume of a job
continues the same trace with the same sampling decision. A restart or watchdog
retry is a new job: it starts a new trace with a link to the previous one and
is sampled independently.

Approvals, waits and child workflow invocations end a segment as paused, not as
an error. A failure sets the span status to error and records a bounded
`error.type` (`timeout`, `cancelled`, an HTTP or MCP error code, or `_OTHER`).

Traces never contain prompts, completions, tool arguments or results, files,
artifacts, interrupt payloads, exception messages or stack traces, query
strings, request or response headers, client addresses or user agents. OAuth
callback parameters therefore never leave. Span events and status descriptions
are removed before export.

Flow Steward does not propagate trace context to external MCP servers: requests
to them run with an empty trace context and carry no trace fields in `_meta`.

### Metrics

Operation counts are the `count` of the duration histograms; there are no
separate counters.

| Metric | Type, unit | Attributes | Owner |
| --- | --- | --- | --- |
| `fs.workflow.segment.duration` | histogram, `s` | `fs.outcome`, `error.type` | worker |
| `fs.workflow.step.duration` | histogram, `s` | `fs.step.kind`, `fs.outcome`, `error.type` | worker |
| `gen_ai.client.operation.duration` | histogram, `s` | `gen_ai.operation.name`, `gen_ai.provider.name`, `fs.llm.purpose`, `error.type` | every role that calls models or tools |
| `gen_ai.client.token.usage` | histogram, `{token}` | `gen_ai.operation.name`, `gen_ai.provider.name`, `fs.llm.purpose`, `gen_ai.token.type` | every role that calls models |
| `process.cpu.time` | counter, `s` | none | each process |
| `process.thread.count` | up-down counter, `{thread}` | none | each process |
| `process.uptime` | gauge, `s` | none | each process |
| `fs.installation.projects` | gauge, `{project}` | none | control |
| `fs.installation.jobs` | gauge, `{job}` | `fs.job.status` | control |
| `fs.installation.project_leases.active` | gauge, `{lease}` | none | control |
| `fs.installation.ready_queue.depth` | gauge, `{message}` | none | control |
| `fs.slo.workflow.p95_latency` | gauge, `s` | none | control |
| `fs.slo.event_dispatch.retry_rate` | gauge, `1` | none | control |
| `fs.slo.event_dispatch.dead_letter_rate` | gauge, `1` | none | control |
| `fs.slo.jobs.stuck` | gauge, `{job}` | none | control |

`fs.llm.purpose` is one of `workflow`, `classification`, `summarization`,
`guardrail`, `replay` or `verification`. `gen_ai.provider.name` is `openai`,
`anthropic`, `azure.ai.openai`, `gcp.gemini` or `other`; extension providers and
OpenAI-compatible routes are `other`. Model, agent, tool and workflow names
never appear in metric attributes: there is no per-model breakdown. Cache
token counts appear only on model spans
(`gen_ai.usage.cache_read.input_tokens`,
`gen_ai.usage.cache_creation.input_tokens`).

Correspondence with the application `/metrics` endpoint:

| `/metrics` series | OTLP metric |
| --- | --- |
| `platform_projects_total` | `fs.installation.projects` |
| `platform_jobs_total` | `fs.installation.jobs` (still a gauge per status) |
| `platform_project_leases_active` | `fs.installation.project_leases.active` |
| `platform_ready_queue_depth` | `fs.installation.ready_queue.depth`; `-1` is not exported |
| `platform_slo_p95_latency_seconds` | `fs.slo.workflow.p95_latency` |
| `platform_slo_retry_rate` | `fs.slo.event_dispatch.retry_rate` |
| `platform_slo_dead_letter_rate` | `fs.slo.event_dispatch.dead_letter_rate` |
| `platform_slo_stuck_jobs` | `fs.slo.jobs.stuck` |
| `platform_health_ready`, `platform_readiness_check` | Not exported: readiness stays in health endpoints and `/metrics`. |
| `platform_slo_agent_failure_rate` | Not exported: agent names are not a bounded attribute. |
| `platform_process_cpu_seconds_total`, `platform_process_threads`, `platform_process_start_time_seconds` | `process.cpu.time`, `process.thread.count`, `process.uptime` |
| `platform_process_up`, `platform_process_info` | Not exported; identity is in the resource attributes. |
| Worker, control and MCP diagnostics | Not exported; available on the process endpoints. |

Unknown values (an unavailable queue, an SLO with no data) are not exported,
rather than reported as zero.

If both a Prometheus scrape of `/metrics` and OTLP metrics reach the same
backend, the installation data is collected twice under different names.
Choose one path per backend. To ingest OTLP metrics into Prometheus, enable its
OTLP receiver (`--web.enable-otlp-receiver`); Prometheus translates the names,
for example `fs.workflow.step.duration` becomes
`fs_workflow_step_duration_seconds`.

### Logs

Local logs are unchanged. Only a small set of explicitly marked operational
events is exported, each as a separate record with a fixed message equal to its
event name, the original time and severity, a few bounded fields,
`correlation.id`, and the trace and span ids:

| Event | Fields |
| --- | --- |
| `fs.telemetry.export_enabled` | `fs.role` |
| `fs.job.execution_failed` | `fs.job.id` |
| `fs.job.execution_failure_status` | `fs.job.id`, `fs.job.status` |

The original message, its arguments, exception information and stack traces are
never exported.

### Limits

- OTLP export carries no readiness signal; use the health endpoints.
- Telemetry can be lost when a process is killed, when the receiver is
  unavailable, or when a model call reports its usage after its step has ended.
  A late usage report is still recorded in the built-in job accounting.
- Token usage over OTLP is an observability signal, not guaranteed financial
  accounting. Use the built-in usage records for billing.
- LangSmith receiving these traces shows execution structure, models and token
  counts, but no prompts or completions.
- The export follows OpenTelemetry semantic conventions 1.44.0 with deliberate
  deviations: model names are not metric attributes, and Flow Steward's own
  workflow spans and metrics use the `fs.` namespace. It is OTLP-compatible but
  does not claim full semantic-conventions compliance.

See the upstream documentation for the
[OpenTelemetry Collector](https://opentelemetry.io/docs/collector/),
[LangSmith OpenTelemetry tracing](https://docs.langchain.com/langsmith/trace-with-opentelemetry)
and [Prometheus OpenTelemetry ingestion](https://prometheus.io/docs/guides/opentelemetry/).
