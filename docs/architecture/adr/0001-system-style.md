# ADR 0001: System Style

- Status: Proposed
- Date: 2026-04-12

## Context

Flow Steward contains multiple domains, multiple runtimes, and a growing operational surface.
The repository needs a clear architectural baseline so new work does not drift toward
an unstructured monolith or premature microservices split.

## Decision

Flow Steward is a modular monolith.

The system keeps explicit domain boundaries, service boundaries, and runtime ownership
inside one repository and one coordinated deployment stack.

## Consequences

- New work should strengthen boundaries rather than add more cross-cutting coupling.
- Domain-first organization is the target direction for new work and major rewrites.
- Architectural exceptions must be documented rather than introduced informally.

## Alternatives Considered

- Microservices-first split
- Pure layer-based monolith with no explicit domain direction

## Rollout Notes

Use incremental convergence. Do not force large repo-wide rewrites only for symmetry.

## Related Standards and Docs

- `docs/standards/architecture-standard.md`
