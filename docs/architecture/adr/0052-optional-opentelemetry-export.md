# ADR 0052: Optional OpenTelemetry Export

- Status: Accepted
- Date: 2026-11-01
- Relates to: [ADR 0031](0031-runtime-heartbeat-monitoring.md) (built-in monitoring stays the default)

## Context

Flow Steward monitors itself: runtime heartbeats, job journals and
Prometheus-format `/metrics`. Operators asked to send traces, metrics and logs to
their own OTLP receiver or OpenTelemetry Collector. The built-in monitoring must
keep working without one, and an unreachable receiver must not affect a
workflow.

Measured on the pinned libraries before deciding (wheels of
`opentelemetry-sdk` 1.45.2, `opentelemetry-instrumentation-*` 0.66b2,
`mcp` 2.2.0, `opentelemetry-instrumentation-genai-langchain` 1.3b0):

- the SDK, exporters and instrumentations read `OTEL_*` variables themselves —
  at import, at construction and again after every fork; an exporter built
  without an endpoint sends to `http://localhost:4318`;
- after a fork the SDK restarts an inherited periodic metric reader and collects
  once more on its shutdown, regenerates `service.instance.id`, and re-reads
  `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` (a `process` detector adds the command
  line and executable path);
- the GenAI LangChain instrumentation interprets usage itself (an unknown token
  count becomes `0`), records a `GraphInterrupt` as an error with the exception
  text as the status description, and emits a GenAI workflow span for every
  LangGraph run;
- the MCP SDK installs a server span middleware by default and injects the
  current context into every outgoing request.

## Decision

**Off unless configured, per signal.** A signal is exported only with its
exporter set to `otlp` *and* an endpoint. There is no default destination. One
receiver per signal; fan-out is the operator's Collector.

**Standard SDK and exporters only.** OTLP/HTTP protobuf exporters, standard
batch processors, periodic reader, parent-based samplers and the W3C trace
context propagator. No own transport, serialization, queue, retry or sampling.

**The process environment is the enforcement point.** Every entrypoint first
imports `core.infrastructure.observability.otel_bootstrap`, which removes every
`OTEL_*` variable outside the documented set, pins
`OTEL_SEMCONV_STABILITY_OPT_IN=http`, and reduces `OTEL_RESOURCE_ATTRIBUTES` to
`deployment.environment.name`. `service.name` comes from the process role.
Settings that matter are also passed to the SDK explicitly (endpoint, timeout,
sampler, batch bounds, span limits). The delivery layers already forward only
the supported names: Compose anchors and the Compact supervisor allowlists.

**Guards in front of the standard exporters, not replacements.**
`PidGuardedMetricExporter` calls the real exporter only from the process that
created it, so a forked agent or tooling child never exports the parent's
metrics. `SanitizingSpanExporter` exports a reduced public `ReadableSpan` copy:
scope allowlist, attribute allowlist per scope, no events, status code without
description, links without attributes. Finished spans are never mutated.

**No automatic GenAI/LangChain instrumentation.** Domain spans and measurements
come from adapters at existing Flow Steward callback boundaries through the
standard API, so usage has one interpretation, interrupts are pauses, and
ordinary workflows are not declared GenAI operations.

**Bounded lifecycle.** Providers do not register their unbounded exit hooks;
one bounded shutdown runs on process stop and at exit. Forked children flush
traces and logs explicitly before they exit.

**MCP.** The MCP SDK middleware API used to remove its server span middleware is
provisional in 2.x; the pinned version is covered by a test. The SDK's own spans
(scope `mcp-python-sdk`) are not exported. Its client span cannot simply be
dropped at export, because the SDK injects that span's context into `_meta` and
fs-mcp would then parent its server span to a span that never arrives. The
tracer provider therefore hands that scope a tracer that creates no span: the
calling workflow step stays current, the SDK injects it, and the server span
nests under the step.

## Consequences

- Without configuration nothing changes: the API stays a no-op and no
  connection is opened.
- Telemetry may be lost when the receiver is slow or down, on buffer overflow
  and when a child is killed. OTLP is not a billing source.
- `opentelemetry-instrumentation-*` packages and the Python Logs SDK are beta or
  experimental; their versions are pinned exactly in every role lock.
- Adding an operator setting means adding it to the supported set, the env
  manifest, the Compose anchor and the Compact telemetry allowlist together.
