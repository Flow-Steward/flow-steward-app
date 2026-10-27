# ADR 0021: Quality Evidence Trust Boundary

## Status

Accepted — 2026-08-13; amended 2026-09-28 for local Docker verification; evidence reuse scope amended by
[ADR 0036](0036-content-scoped-quality-evidence.md) on 2026-10-14

## Context

Cumulative fast, full, audit, CI, and release commands repeated the same tests
and security checks. A local result also lacked a reliable boundary proving
that it described the immutable candidate being released.

## Decision

Every executable check has one owning tier in the typed quality registry.
Higher tiers execute only their delta and may reuse checksummed evidence whose
check identity, owner tier, content fingerprint, runtime identity, and status
match.

Tiers 1–3 run locally in Docker. GitHub Actions and the fourth tier have been
removed to avoid paid runner usage. Release candidates require complete passing
Tier 2 and Tier 3 evidence for the same exact candidate SHA. The final `--full`
wrapper refreshes or reuses exact-SHA Tier 2, executes the Tier 3 delta, and
validates cumulative evidence. Missing, corrupt, failed, incomplete, or
different-SHA evidence blocks candidate verification. SonarQube remains a local
Docker check.

Candidate-dependent ZAP, integration/E2E, and smoke checks remain externally
owned runtime checks. Two candidate smoke executions are the only intentional
repeat and retain the same SHA, image digests, compose project, and environment.

## Consequences

- Local fix cycles and pre-commit can reuse matching local evidence.
- The local full gate has one Tier 3 owner and one coverage owner.
- No GitHub Actions artifact or remote check status is required.
- Deep checks remain explicitly selectable local commands, outside the mandatory tiers.
- Any candidate or relevant input change invalidates evidence rather than
  silently widening trust.
- Generated metrics below `logs/quality/evidence/` are operational output and
  are not committed.
