"""Release-safe database upgrade command for existing environments."""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import Any

from core.application.tooling.db import init_db, migration_runner, vector_lifecycle
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.schema_contract import (
    enforce_canonical_baseline_contract_connection,
)
from core.infrastructure.db.schema_state import (
    canonical_seed_fingerprint,
    inspect_canonical_schema_state,
    record_canonical_schema_state,
)

UPGRADE_ADVISORY_LOCK_KEY = migration_runner.MIGRATION_ADVISORY_LOCK_KEY

# db/schema/ became the clean 1.0 baseline on 2026-09-26 and every pre-release
# migration was deleted with it. A ledger row versioned before this cutoff was
# written by a pre-release (pre-1.0) build, while every post-1.0 migration is
# generated after it. Such a database is not upgradable: it is recreated.
PRE_RELEASE_MIGRATION_CUTOFF = "20260926000000"
_PRE_RELEASE_SHOWN = 3


def pre_release_migrations(applied: list[str]) -> list[str]:
    """Ledger rows written by a pre-1.0 build, which the 1.0 baseline replaced."""
    return sorted(
        name
        for name in applied
        if migration_runner.is_valid_migration_filename(name)
        and name[: len(PRE_RELEASE_MIGRATION_CUTOFF)] < PRE_RELEASE_MIGRATION_CUTOFF
    )


def _refuse_pre_release_database(conn) -> None:
    """Fail closed, before any mutation, on a database a pre-1.0 build migrated."""
    historical = pre_release_migrations(migration_runner.applied_unknown_migrations(conn))
    if not historical:
        return
    shown = ", ".join(historical[:_PRE_RELEASE_SHOWN])
    if len(historical) > _PRE_RELEASE_SHOWN:
        shown += f" and {len(historical) - _PRE_RELEASE_SHOWN} more"
    # One line: the installer reports only the last line of this refusal.
    raise RuntimeError(
        "This is a pre-release (pre-1.0) Flow Steward database: its migration ledger "
        f"records migrations from before the 1.0 schema baseline ({shown}). Flow Steward "
        "1.0 starts from a clean baseline and cannot upgrade a pre-release database, so "
        "it was left unchanged. Recreate it: install 1.0 from a clean checkout with a "
        "distinct Compose project identity by running ./install.sh. The installer "
        "initializes the canonical schema and seed for Compact and Scalable. "
        "After installation, inspect ./flow-steward database status. See "
        "docs/GUIDE.md (Step 1 — Initial Setup) and ADR 0012 for the baseline policy."
    )


def _resolved_dsn(explicit_dsn: str | None = None) -> str:
    dsn = str(explicit_dsn or init_db._resolve_sql_dsn() or "").strip()
    if not dsn:
        raise RuntimeError("No Postgres DSN configured for database upgrade")
    return dsn


def _repair_missing_user_memberships(conn) -> int:
    """Backfill only absent memberships; preserve every existing tenant row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.user_account_memberships (user_id, account_id, is_default)
            SELECT users.id, users.account_id, true
            FROM public.users AS users
            WHERE NOT EXISTS (
                SELECT 1
                FROM public.user_account_memberships AS membership
                WHERE membership.user_id = users.id
            )
            ON CONFLICT (user_id, account_id) DO NOTHING
            """
        )
        rowcount = getattr(cur, "rowcount", 0)
    return max(int(rowcount or 0), 0)


def _stamp_verified_canonical_baseline(conn, *, component: str) -> dict[str, Any]:
    enforce_canonical_baseline_contract_connection(
        conn,
        component=component,
        include_data_invariants=False,
    )
    # db/schema is the 1.0 baseline and every db/migrations/ file is a post-1.0
    # change, so a verified baseline applies all of them rather than stamping any.
    post_baseline = migration_runner.apply_pending_migrations(conn, acquire_lock=False)
    return {
        **post_baseline,
        "stamped": [],
        "pending": [],
    }


def inspect_database_upgrade(*, dsn: str | None = None) -> dict[str, Any]:
    dsn = _resolved_dsn(dsn)
    conn = psycopg_runtime.connect(dsn)
    try:
        state = inspect_canonical_schema_state(conn)
        # The canonical schema fingerprint is frozen at the 1.0 baseline, so it
        # no longer changes when a release only adds db/migrations/*.sql. Fold
        # both unapplied migrations and checksum drift of already-applied
        # migrations into needs_upgrade so the operator release path still takes
        # a backup and reaches the runner (which fails closed on drift) instead
        # of silently treating the database as compatible (ADR 0012).
        migration_state = migration_runner.inspect_migrations(conn)
        pre_release = pre_release_migrations(migration_runner.applied_unknown_migrations(conn))
    finally:
        conn.close()
    pending = migration_state["pending"]
    drifted = migration_state["drifted"]
    state["pending_migrations"] = pending
    state["drifted_migrations"] = drifted
    # A pre-1.0 ledger is never compatible: report it so the upgrade path reaches
    # the refusal that tells the operator to recreate the database.
    state["pre_release_migrations"] = pre_release
    state["needs_upgrade"] = (
        bool(state.get("needs_upgrade")) or bool(pending) or bool(drifted) or bool(pre_release)
    )
    return state


def upgrade_database(
    *,
    dsn: str | None = None,
    author: str = "system",
    maintenance_confirmed: bool = False,
) -> dict[str, Any]:
    dsn = _resolved_dsn(dsn)
    init_db.validate_bootstrap_layout()
    conn = psycopg_runtime.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_lock(%s)",
            (UPGRADE_ADVISORY_LOCK_KEY,),
        )
    try:
        vector_lifecycle.require_pgvector_image(conn)
        tracking_exists = migration_runner.migrations_table_exists(conn)
        if tracking_exists:
            _refuse_pre_release_database(conn)
        protected_before = (
            vector_lifecycle.capture_protected_data_snapshot(conn) if maintenance_confirmed else {}
        )
        if tracking_exists:
            schema_migrations = migration_runner.apply_pending_migrations(
                conn,
                acquire_lock=False,
            )
        else:
            schema_migrations = _stamp_verified_canonical_baseline(
                conn,
                component="database upgrade baseline",
            )
        schema_reconcile = {
            "files": 0,
            "statements": 0,
            "applied": 0,
            "skipped": 0,
            "failed": [],
        }

        init_db.init_platform_observability_schema()
        schema_cleanup = init_db.cleanup_retired_project_schema(dsn=dsn)
        summary = init_db.apply_clean_bootstrap(dsn=dsn, author=author)
        repaired_memberships = _repair_missing_user_memberships(conn)
        enforce_canonical_baseline_contract_connection(
            conn,
            component="database upgrade baseline",
        )
        schema_migrations["repaired_user_memberships"] = repaired_memberships
        summary["schema_cleanup"] = schema_cleanup
        summary["schema_reconcile"] = schema_reconcile
        summary["schema_migrations"] = schema_migrations
        summary["workflow_knowledge_graph"] = init_db.init_workflow_knowledge_graph(dsn=dsn)
        init_db.init_extension_catalog()
        init_db.init_langgraph_schema()

        protected_after = (
            vector_lifecycle.capture_protected_data_snapshot(conn) if maintenance_confirmed else {}
        )
        if maintenance_confirmed:
            vector_lifecycle.assert_protected_data_unchanged(
                protected_before,
                protected_after,
            )
        vector_health = vector_lifecycle.inspect_vector_health(
            conn,
            deep=True,
        )
        if vector_health["status"] != "healthy":
            raise RuntimeError(f"Vector health is not release-ready: {vector_health}")
        state_row = record_canonical_schema_state(
            conn,
            applied_by=author,
            metadata={
                "source": "flow-steward database upgrade",
                "seed_fingerprint": canonical_seed_fingerprint(),
            },
        )
        summary["schema_state"] = {
            "updated": 1,
            "fingerprint": state_row["schema_fingerprint"],
        }
        summary["protected_data_before"] = protected_before
        summary["protected_data_after"] = protected_after
        summary["vector_health"] = vector_health
        return summary
    finally:
        with contextlib.suppress(Exception), conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(%s)",
                (UPGRADE_ADVISORY_LOCK_KEY,),
            )
        conn.close()


def baseline_migration_history(
    *,
    dsn: str | None = None,
    author: str = "installer",
) -> dict[str, Any]:
    """Create a migration ledger only for a proven canonical clean baseline."""
    del author  # Reserved for the operator audit envelope printed by the caller.
    dsn = _resolved_dsn(dsn)
    init_db.validate_bootstrap_layout()
    conn = psycopg_runtime.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (UPGRADE_ADVISORY_LOCK_KEY,))
    try:
        if migration_runner.migrations_table_exists(conn):
            _refuse_pre_release_database(conn)
            state = migration_runner.inspect_migrations(conn)
            unknown = migration_runner.applied_unknown_migrations(conn)
            if unknown:
                raise RuntimeError(
                    "The database carries migrations this release does not know: "
                    + ", ".join(unknown)
                    + ". A newer release upgraded it, and this older one is not started on "
                    "a schema it has never seen. Restore the backup taken before that "
                    "release's migration, or install the newer release again."
                )
            if state["pending"] or state["drifted"]:
                raise RuntimeError(
                    "Migration history already exists but does not match this release; "
                    "use flow-steward database upgrade after verifying a backup."
                )
            return {
                "total": len(migration_runner.discover_migration_files()),
                "stamped": [],
                "skipped": [],
            }
        return _stamp_verified_canonical_baseline(
            conn,
            component="installer database baseline",
        )
    finally:
        with contextlib.suppress(Exception), conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (UPGRADE_ADVISORY_LOCK_KEY,))
        conn.close()


def _print_summary(summary: dict[str, Any]) -> None:
    for group, counts in summary.items():
        if isinstance(counts, dict):
            rendered = ", ".join(f"{name}={value}" for name, value in sorted(counts.items()))
            print(f"[OK] {group}: {rendered}")
        else:
            print(f"[OK] {group}: {counts}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Upgrade an existing Flow Steward database to the current canonical schema/seed state."
    )
    parser.add_argument(
        "--baseline-migration-history",
        action="store_true",
        help="Create the migration ledger for a proven canonical clean baseline.",
    )
    parser.add_argument(
        "--author",
        default="system",
        help="Audit author recorded for idempotent seed and schema-state updates.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report whether the current database fingerprint matches the checked-in canonical state.",
    )
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help=(
            "Confirm runtime writers are stopped and a verified backup exists "
            "before applying database maintenance."
        ),
    )
    args = parser.parse_args()

    if args.baseline_migration_history:
        print("Verifying canonical baseline and creating migration history...")
        try:
            summary = baseline_migration_history(author=str(args.author or "installer"))
        except RuntimeError as exc:
            # AC17-R160: an operator stopped by this check read a Python traceback. The
            # refusal is the whole message; its last line is what the installer shows,
            # under its own "Error:" (R161 acceptance: it read "Error: Error:").
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from None
        _print_summary(summary)
        print("Migration history baseline complete.")
        raise SystemExit(0)

    if args.check:
        status = inspect_database_upgrade()
        current = status.get("current") or {}
        print(
            "Schema state check: "
            f"needs_upgrade={status['needs_upgrade']} "
            f"current_fingerprint={current.get('schema_fingerprint', '')} "
            f"expected_fingerprint={status['expected_fingerprint']}"
        )
        raise SystemExit(2 if status["needs_upgrade"] else 0)

    print("Applying canonical database upgrade...")
    _print_summary(
        upgrade_database(
            author=str(args.author or "system"),
            maintenance_confirmed=bool(args.maintenance_confirmed),
        )
    )
    print("Database upgrade complete.")
