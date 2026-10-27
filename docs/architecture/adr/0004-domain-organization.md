# ADR 0004: Domain Organization

- Status: Proposed
- Date: 2026-04-12

## Context

The repository has a hybrid layout today. Some areas are grouped by layer, while the
project as a whole behaves like a large multi-domain platform. New work needs a
clear direction that improves navigability without forcing disruptive rewrites.

## Decision

The target direction for new work is domain-first organization.

New features and major rewrites should group related transport, service, schema,
and test code around domain responsibility as much as practical.

## Consequences

- New domains should be easier to find, reason about, and delete or refactor.
- Older-layout areas may remain hybrid until they are touched for meaningful work.
- Transport changes follow the canonical `core/entrypoints/` layout in
  [ADR 0007](0007-repository-structure.md). Older folders are transition-only;
  narrow fixes may touch them in place, but new permanent code belongs in the
  canonical layout.
- Domain-first does not justify broad churn without a migration plan.

## Alternatives Considered

- Keep a purely layer-based repository forever
- Force a repo-wide domain reorganization immediately

## Rollout Notes

Adopt incrementally. Use ADRs for any large structural migration.

## Related Standards and Docs

- `docs/standards/architecture-standard.md`
- `docs/standards/backend-python-standard.md`
