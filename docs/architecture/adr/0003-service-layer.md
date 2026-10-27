# ADR 0003: Service Layer

- Status: Proposed
- Date: 2026-04-12

## Context

Business logic has historically leaked into transport handlers and mixed-responsibility files.
The project needs a consistent way to keep route, webhook, WebSocket, and CLI behavior aligned.

## Decision

Business logic lives outside transport handlers.

State-changing behavior should flow through command-oriented services.
Read aggregation should flow through query-oriented services where practical.
Transport layers stay thin and focus on boundary concerns.

## Consequences

- Routes, WebSocket handlers, webhook entrypoints, and CLI commands should share service paths when the use case matches.
- Review should treat transport-level business logic as architectural drift.
- New shared workflows should be extracted before parallel implementations diverge.

## Alternatives Considered

- Handler-centric business logic
- Domain logic distributed across UI, API, and MCP surfaces independently

## Rollout Notes

Use incremental extraction in touched areas. Do not widen refactor scope only for stylistic purity.

## Related Standards and Docs

- `docs/standards/architecture-standard.md`
- `docs/standards/backend-python-standard.md`
