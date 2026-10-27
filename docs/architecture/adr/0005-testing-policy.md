# ADR 0005: Testing Policy

- Status: Proposed
- Date: 2026-04-12

## Context

The repository already has broad test coverage, but current evidence shows uneven signal quality,
permissive assertions, and historical CI masking in critical areas. The project needs a clear
testing baseline that optimizes for trust, not raw test count.

## Decision

Testing policy is contract-first and regression-required.

Bug fixes require regression tests.
Important flows require exact assertions rather than “not 500” style checks.
Critical CI exclusions are treated as debt, not as the desired steady state.

## Consequences

- Test quality matters more than broad but weak coverage.
- New changes in weak areas should reduce masking or improve signal where practical.
- UI-critical and security-sensitive paths require targeted verification.

## Alternatives Considered

- Coverage-percentage-first policy
- Smoke-test-first policy with permissive assertions

## Rollout Notes

Apply stricter test expectations first in the most failure-prone areas.

## Related Standards and Docs

- `docs/standards/testing-standard.md`
