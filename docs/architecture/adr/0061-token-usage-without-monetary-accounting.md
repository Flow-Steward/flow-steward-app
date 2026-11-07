# ADR 0061: Token usage without monetary accounting

- Status: Accepted
- Date: 2026-11-05

## Context

Provider billing depends on model, request mode, cache behavior, context size,
and provider-specific rules. A maintained table of model prices cannot reliably
represent each installation's actual charges. Showing calculated money as product
usage information gives operators misleading financial data.

## Decision

Flow Steward records token usage, model/provider identity, call counts, and runtime
telemetry. It does not calculate, aggregate, or present monetary LLM costs and does
not maintain a pricing catalog. Provider-reported costs are not normalized into
an internal monetary accounting contract.

Job summaries and agent/model breakdowns use `usage` terminology across REST,
WebSocket, MCP, reports, and the product interface. Monetary dashboard cards and
report sections are removed rather than replaced with zero or unknown amounts.
Token usage and existing observability surfaces are preserved; this change adds
no new OpenTelemetry/OTLP exporter. Operators obtain financial
information from provider billing or a suitably configured external telemetry
service; exporting telemetry alone does not guarantee financial accuracy.

The published database baseline and existing installation databases remain
unchanged. Legacy monetary columns and indexes remain unused; application queries
and writes do not depend on them. This change adds no schema migration and deletes
no stored data. Any later database cleanup must follow ADR 0056. Saved reports of retired types, and saved
snapshots containing the retired accounting fields, are unavailable through report
retrieval. Legacy reports of the affected types without a payload snapshot are also
unavailable because their stored summary and chart may contain monetary values.
Business data inside diagnostic objects is preserved, including fields named
`cost` or `spend`; detection is limited to platform-owned report containers.
Operators can generate a new usage-only report; old rows are preserved.

## Consequences

- Token accounting and model selection remain independent of pricing maintenance.
- Monetary REST/report fields and old cost-named endpoints/tools are removed.
- Operational time, retry, and token limits retain their existing semantics.
- There is no built-in billing replacement or new external billing integration.
