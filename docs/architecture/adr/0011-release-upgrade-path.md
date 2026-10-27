# ADR 0011: Canonical Release Upgrade Path

- Status: Superseded by [ADR 0012](0012-post-1.0-sql-migrations.md)
- Date: 2026-05-27

The current schema/upgrade decision is ADR 0012: canonical `db/schema/` baseline,
post-1.0 ordered SQL under `db/migrations/`, and the explicit maintenance-confirmed
upgrade command. See the [deployment runbook](../../operations/compact-scalable-deployment.md)
for operator procedures. This stable identifier is retained for existing
references, without the obsolete code-only compatibility-upgrade design.
