"""Data phases of `database upgrade` (ADR 0054).

A phase is a host-owned step that runs after the SQL migrations it requires. It never
changes DDL, reads only the database, calls the same application functions as
runtime, and commits together with its completion row in
`platform_upgrade_phases`: it either completed or left nothing. The registry below
is the only list. Migrations cannot add to it or call a phase.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.infrastructure.config.version import APP_VERSION

INITIAL_APPROVALS_ACTOR = "system:upgrade:catalog_secret_initial_approvals"


@dataclass(frozen=True)
class UpgradePhase:
    phase_id: str
    requires_migrations: tuple[str, ...]
    run: Callable[[Any], dict[str, Any]]


def _catalog_secret_initial_approvals(cur: Any) -> dict[str, Any]:
    """Approve, once, the current configuration of every connection the catalog move bound.

    Those configurations were in use before the upgrade; the move only changed where
    their credentials are recorded. A connection that already has an approval is left
    alone, and after this phase completes it never runs again, so nothing changed
    later is approved by it.
    """
    from core.application.secrets import repository
    from core.application.secrets.projection import projection_fingerprint
    from core.application.secrets.service import connection_projection_for

    cur.execute(
        """
        SELECT DISTINCT b.account_id::text, b.project_connection_id
          FROM catalog_secret_bindings AS b
         WHERE b.consumer_kind = 'project_connection'
           AND NOT EXISTS (
               SELECT 1 FROM catalog_secret_approvals AS a
                WHERE a.project_connection_id = b.project_connection_id
           )
         ORDER BY 2
        """
    )
    pending = [
        (str(account_id), str(connection_id)) for account_id, connection_id in cur.fetchall() or []
    ]
    approved = 0
    for account_id, connection_id in pending:
        connection = repository.lock_connection(
            cur, account_id=account_id, connection_id=connection_id
        )
        if connection is None:
            continue
        projection = connection_projection_for(cur, connection)
        fingerprint = projection_fingerprint(projection)
        repository.record_connection_approval(
            cur,
            account_id=account_id,
            connection_id=connection_id,
            fingerprint=fingerprint,
            projection=projection,
            actor=INITIAL_APPROVALS_ACTOR,
            actor_kind="upgrade",
        )
        repository.record_audit(
            cur,
            account_id=account_id,
            command_type="secret.approve",
            actor_kind="system",
            actor_id=INITIAL_APPROVALS_ACTOR,
            target={"connection_id": connection_id},
            payload={"fingerprint": fingerprint, "reason": "upgrade_move"},
        )
        approved += 1
    return {"approved_connections": approved}


UPGRADE_PHASES: tuple[UpgradePhase, ...] = (
    UpgradePhase(
        phase_id="catalog_secret_initial_approvals",
        requires_migrations=("20261103090000_secret_catalog.sql",),
        run=_catalog_secret_initial_approvals,
    ),
)


def recorded_phases(conn: Any) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.platform_upgrade_phases') IS NOT NULL")
        row = cur.fetchone()
        if not row or not row[0]:
            return set()
        cur.execute("SELECT phase_id FROM platform_upgrade_phases")
        return {str(phase_id) for (phase_id,) in cur.fetchall() or []}


def unknown_recorded_phases(conn: Any) -> list[str]:
    known = {phase.phase_id for phase in UPGRADE_PHASES}
    return sorted(recorded_phases(conn) - known)


def run_pending_phases(conn: Any, *, applied_migrations: set[str] | None = None) -> dict[str, Any]:
    """Run each phase this database has not completed, each in its own transaction."""
    if applied_migrations is None:
        from core.application.tooling.db import migration_runner

        applied_migrations = set(migration_runner.fetch_applied_migrations(conn))
    completed: dict[str, Any] = {}
    done = recorded_phases(conn)
    for phase in UPGRADE_PHASES:
        if phase.phase_id in done:
            continue
        missing = sorted(set(phase.requires_migrations) - applied_migrations)
        if missing:
            raise RuntimeError(
                f"Upgrade phase {phase.phase_id} requires migrations that are not applied: "
                + ", ".join(missing)
            )
        with conn.transaction(), conn.cursor() as cur:
            summary = phase.run(cur)
            cur.execute(
                """
                INSERT INTO platform_upgrade_phases (phase_id, release_version, summary_jsonb)
                VALUES (%s, %s, %s::jsonb)
                """,
                (phase.phase_id, APP_VERSION, _json(summary)),
            )
        completed[phase.phase_id] = summary
    return completed


def run_pending_phases_dsn(dsn: str) -> dict[str, Any]:
    """Connect and run the pending phases; for paths that applied migrations by DSN."""
    import contextlib

    from core.infrastructure.db import psycopg_runtime

    conn = psycopg_runtime.connect(dsn)
    try:
        conn.autocommit = True
        return run_pending_phases(conn)
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)


__all__ = [
    "UPGRADE_PHASES",
    "UpgradePhase",
    "recorded_phases",
    "run_pending_phases",
    "run_pending_phases_dsn",
    "unknown_recorded_phases",
]
