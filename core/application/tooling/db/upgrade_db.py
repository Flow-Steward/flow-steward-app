"""Release-safe database upgrade command for existing environments."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from core.application.tooling.db import (
    init_db,
    migration_runner,
    upgrade_phases,
    upgrade_preflight,
    vector_lifecycle,
)
from core.infrastructure.config.session_identity import set_session_role
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.schema_contract import (
    enforce_canonical_baseline_contract_connection,
)
from core.infrastructure.db.schema_state import (
    canonical_seed_fingerprint,
    inspect_canonical_schema_state,
    record_canonical_schema_state,
)

logger = logging.getLogger(__name__)

UPGRADE_ADVISORY_LOCK_KEY = migration_runner.MIGRATION_ADVISORY_LOCK_KEY

# db/schema/ became the clean 1.0 baseline on 2026-09-26 and every pre-release
# migration was deleted with it. A ledger row versioned before this cutoff was
# written by a pre-release (pre-1.0) build, while every post-1.0 migration is
# generated after it. Such a database is not upgradable: it is recreated.
PRE_RELEASE_MIGRATION_CUTOFF = "20260926000000"
_PRE_RELEASE_SHOWN = 3

#: Process role under which the upgrade asks for the prepared platform modules.
#: The database upgrade runs from the control plane (ADR 0074).
PLATFORM_MODULE_UPGRADE_ROLE = "control"

#: Returns the prepared platform modules whose migrations the upgrade applies:
#: ``ModuleMigrationSource`` objects or ``(module_id, module_version,
#: migrations_dir)`` tuples.
ModuleSourcesProvider = Callable[[], Iterable[Any]]


def prepared_module_migration_sources() -> tuple[migration_runner.ModuleMigrationSource, ...]:
    """The prepared platform modules (ADR 0082) as migration sources.

    Each module ships its migrations in ``<module dir>/db/migrations``. A Core
    build without platform-module support yields nothing. The modules are verified
    against their list but activated on a copy of ``sys.path``: the upgrade only
    needs their directories and never imports module code.
    """
    try:
        from core.infrastructure.platform_modules.loader import (
            activate_prepared_platform_modules,
        )
    except ImportError:
        return ()
    modules = activate_prepared_platform_modules(
        PLATFORM_MODULE_UPGRADE_ROLE,
        sys_path=list(sys.path),
    )
    return tuple(
        migration_runner.ModuleMigrationSource(
            module_id=str(module.module_id),
            module_version=str(module.version),
            migrations_dir=Path(module.path) / "db" / "migrations",
        )
        for module in modules
    )


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


def _repair_missing_user_memberships(conn) -> tuple[int, int]:
    """Backfill only absent memberships; preserve every existing tenant row.

    Roles are held on the membership (ADR 0085). A person without any membership has
    no role to derive: the roles migration copied every global role into the
    memberships it made and removed the global rows (a check now refuses them), and
    a scoped row only narrows a role. A repaired membership therefore carries no role
    (``NULL``, no access), never a guessed one; the count is returned and logged so
    an administrator gives those people a role on the users page. Returns
    ``(repaired, without a role)``.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.user_account_memberships (user_id, account_id, is_default, role_id)
            SELECT users.id, users.account_id, true, NULL
            FROM public.users AS users
            WHERE NOT EXISTS (
                SELECT 1
                FROM public.user_account_memberships AS membership
                WHERE membership.user_id = users.id
            )
            ON CONFLICT (user_id, account_id) DO NOTHING
            RETURNING role_id IS NULL AS without_role
            """
        )
        rows = list(cur.fetchall() or ())
    without_role = sum(1 for row in rows if bool(row[0]))
    if without_role:
        logger.warning(
            "Database upgrade repaired %s account membership(s) without a role: those "
            "people have no access until an administrator gives them a role (ADR 0085)",
            without_role,
        )
    return len(rows), without_role


def _stamp_verified_canonical_baseline(conn, *, component: str) -> dict[str, Any]:
    enforce_canonical_baseline_contract_connection(
        conn,
        component=component,
        include_data_invariants=False,
    )
    # db/schema is the 1.0 baseline and every db/migrations/ file is a post-1.0
    # change, so a verified baseline applies all of them rather than stamping any.
    post_baseline = migration_runner.apply_pending_migrations(conn, acquire_lock=False)
    # A fresh database runs every data phase too; on an empty database each is a
    # no-op, and the runtime contract then finds them all recorded (ADR 0056).
    phases = upgrade_phases.run_pending_phases(conn)
    return {
        **post_baseline,
        "stamped": [],
        "pending": [],
        "upgrade_phases": phases,
    }


def _module_sources(provider: ModuleSourcesProvider | None) -> tuple[Any, ...]:
    return tuple((provider or prepared_module_migration_sources)())


def _module_migration_mismatch(report: dict[str, dict[str, list[str]]]) -> list[str]:
    """``module: file`` for every module migration not exactly applied (read-only report)."""
    return [
        f"{module_id}: {name}"
        for module_id, entry in sorted(report.items())
        for key in ("pending", "drifted", "unknown")
        for name in entry.get(key, ())
    ]


def inspect_database_upgrade(
    *, dsn: str | None = None, module_sources: ModuleSourcesProvider | None = None
) -> dict[str, Any]:
    dsn = _resolved_dsn(dsn)
    sources = _module_sources(module_sources)
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
        # Read-only: creates neither the module ledger nor a schema (ADR 0082).
        module_state = migration_runner.inspect_module_migrations(conn, sources)
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
    if module_state:
        # A module-only change (same Core release, new module SQL) needs the
        # database step as much as a Core migration does.
        state["module_migrations"] = module_state
        state["needs_upgrade"] = state["needs_upgrade"] or (
            migration_runner.module_migrations_need_upgrade(module_state)
        )
    return state


def preflight_database_upgrade(
    *,
    dsn: str | None = None,
    allow_unlabeled_sessions: bool = False,
    module_sources: ModuleSourcesProvider | None = None,
) -> dict[str, Any]:
    """Read-only check of everything that would stop the upgrade (ADR 0056)."""
    set_session_role("upgrade")
    dsn = _resolved_dsn(dsn)
    sources = _module_sources(module_sources)
    conn = psycopg_runtime.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        return upgrade_preflight.preflight(
            conn,
            allow_unlabeled_sessions=allow_unlabeled_sessions,
            module_sources=sources,
        )
    finally:
        with contextlib.suppress(Exception):
            conn.rollback()
        conn.close()


class DatabaseUpgradeBlockedError(RuntimeError):
    """The read-only preflight found what stops the upgrade; nothing was changed.

    2026-11-11: this was a bare RuntimeError, and `database upgrade` printed only "An
    unexpected internal error occurred" (internal_error, exit 9) while the previous
    release's processes were still connected: the cause was in the log alone. The
    service turns it into its own public error that lists every blocking issue.
    """

    def __init__(self, blocking: list[dict[str, Any]]) -> None:
        self.blocking = [dict(issue) for issue in blocking]
        first = self.blocking[0] if self.blocking else {}
        super().__init__(
            f"Database upgrade preflight is blocked ({len(self.blocking)} issue(s)); "
            f"first: {first.get('code', '')}: {first.get('message', '')} "
            "Run flow-steward database preflight for the full report."
        )


def _refuse_blocked_preflight(
    conn, *, allow_unlabeled_sessions: bool, module_sources: tuple[Any, ...] = ()
) -> None:
    report = upgrade_preflight.preflight(
        conn,
        allow_unlabeled_sessions=allow_unlabeled_sessions,
        module_sources=module_sources,
    )
    if report["blocking"]:
        raise DatabaseUpgradeBlockedError(list(report["blocking"]))


def upgrade_database(
    *,
    dsn: str | None = None,
    author: str = "system",
    maintenance_confirmed: bool = False,
    skip_db_backup: bool = False,
    allow_unlabeled_sessions: bool = False,
    module_sources: ModuleSourcesProvider | None = None,
) -> dict[str, Any]:
    if skip_db_backup and not maintenance_confirmed:
        raise RuntimeError(
            "--skip-db-backup does not replace --maintenance-confirmed; runtime writers must be stopped."
        )
    set_session_role("upgrade")
    dsn = _resolved_dsn(dsn)
    init_db.validate_bootstrap_layout()
    sources = _module_sources(module_sources)
    conn = psycopg_runtime.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (UPGRADE_ADVISORY_LOCK_KEY,))
        row = cur.fetchone()
    if not row or not row[0]:
        conn.close()
        raise RuntimeError(
            "Another database upgrade is running against this database; "
            "wait for it to finish, then run this one again."
        )
    try:
        # Repeated under the lock: what preflight reported may have changed since.
        _refuse_blocked_preflight(
            conn,
            allow_unlabeled_sessions=allow_unlabeled_sessions,
            module_sources=sources,
        )
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
            schema_migrations["upgrade_phases"] = upgrade_phases.run_pending_phases(conn)
        else:
            schema_migrations = _stamp_verified_canonical_baseline(
                conn,
                component="database upgrade baseline",
            )
        # Platform modules migrate after Core, under the same advisory lock, each
        # in its own schema and ledger (ADR 0082); never in the Core ledger.
        module_migrations = migration_runner.apply_platform_module_migrations(
            conn,
            sources,
            acquire_lock=False,
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
        repaired_memberships, repaired_without_role = _repair_missing_user_memberships(conn)
        enforce_canonical_baseline_contract_connection(
            conn,
            component="database upgrade baseline",
        )
        schema_migrations["repaired_user_memberships"] = repaired_memberships
        schema_migrations["repaired_user_memberships_without_role"] = repaired_without_role
        summary["schema_cleanup"] = schema_cleanup
        summary["schema_reconcile"] = schema_reconcile
        summary["schema_migrations"] = schema_migrations
        summary["platform_module_migrations"] = module_migrations
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
                "skip_db_backup": skip_db_backup,
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
    module_sources: ModuleSourcesProvider | None = None,
) -> dict[str, Any]:
    """Create a migration ledger only for a proven canonical clean baseline.

    Prepared platform modules (ADR 0082) are handled like Core: on a fresh
    database their migrations run through the module runner after Core's, never
    stamped; on an existing ledger any module migration that is not exactly
    applied refuses the baseline, as a pending Core migration does.
    """
    del author  # Reserved for the operator audit envelope printed by the caller.
    set_session_role("upgrade")
    dsn = _resolved_dsn(dsn)
    init_db.validate_bootstrap_layout()
    upgrade_preflight.validate_release_artifacts()
    sources = _module_sources(module_sources)
    conn = psycopg_runtime.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (UPGRADE_ADVISORY_LOCK_KEY,))
    try:
        if migration_runner.migrations_table_exists(conn):
            _refuse_pre_release_database(conn)
            state = migration_runner.inspect_migrations(conn)
            unknown = migration_runner.applied_unknown_migrations(conn)
            unknown += upgrade_phases.unknown_recorded_phases(conn)
            if unknown:
                raise RuntimeError(
                    "The database carries migrations this release does not know: "
                    + ", ".join(unknown)
                    + ". A newer release upgraded it, and this older one is not started on "
                    "a schema it has never seen. Restore the backup taken before that "
                    "release's migration, or install the newer release again."
                )
            missing_phases = [
                phase.phase_id
                for phase in upgrade_phases.UPGRADE_PHASES
                if phase.phase_id not in upgrade_phases.recorded_phases(conn)
            ]
            module_mismatch = _module_migration_mismatch(
                migration_runner.inspect_module_migrations(conn, sources)
            )
            if state["pending"] or state["drifted"] or missing_phases or module_mismatch:
                detail = (
                    f" Platform module migrations not applied as shipped: {module_mismatch}."
                    if module_mismatch
                    else ""
                )
                raise RuntimeError(
                    "Migration history already exists but does not match this release; "
                    "use flow-steward database upgrade after verifying a backup." + detail
                )
            return {
                "total": len(migration_runner.discover_migration_files()),
                "stamped": [],
                "skipped": [],
            }
        baseline = _stamp_verified_canonical_baseline(
            conn,
            component="installer database baseline",
        )
        if sources:
            baseline["platform_module_migrations"] = (
                migration_runner.apply_platform_module_migrations(
                    conn,
                    sources,
                    acquire_lock=False,
                )
            )
        return baseline
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
        "--preflight",
        action="store_true",
        help="Report, without changing anything, what would stop the upgrade (JSON).",
    )
    parser.add_argument(
        "--allow-unlabeled-sessions",
        action="store_true",
        help=(
            "Proceed although sessions of this database user carry no Flow Steward "
            "session name; only after confirming they are not runtime processes."
        ),
    )
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help=(
            "Confirm runtime writers are stopped; a verified backup is expected "
            "unless --skip-db-backup explicitly accepts proceeding without it."
        ),
    )
    parser.add_argument(
        "--skip-db-backup",
        action="store_true",
        help="Explicitly accept database migration without a backup; writer safety checks remain mandatory.",
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

    if args.preflight:
        import json

        report = preflight_database_upgrade(
            allow_unlabeled_sessions=bool(args.allow_unlabeled_sessions)
        )
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        raise SystemExit(2 if report["blocking"] else 0)

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
            skip_db_backup=bool(args.skip_db_backup),
            allow_unlabeled_sessions=bool(args.allow_unlabeled_sessions),
        )
    )
    print("Database upgrade complete.")
