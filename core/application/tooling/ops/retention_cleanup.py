#!/usr/bin/env python3
"""Retention cleanup for platform runtime/observability tables.

Usage examples:
  python -m core.application.tooling.ops.retention_cleanup -- --dry-run
  python -m core.application.tooling.ops.retention_cleanup -- --jobs-days 14 --job-logs-days 21 --action-logs-days 30
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.tooling.ops.retention_cleanup_common import (
    psycopg_runtime,
    resolve_observability_dsn,
    system_settings_store,
)
from core.application.tooling.ops.retention_repository import (
    _TERMINAL_JOB_STATUSES,
    _TRASH_ENTITY_TABLES,
    _delete_checkpoint_rows_for_thread,
    _delete_or_count,
    _delete_or_count_action_log,
    _delete_or_count_agent_messages,
    _delete_or_count_idempotency_tombstones,
    _delete_or_count_project_settings_revisions,
    _delete_or_count_registry_changelog,
    _delete_or_count_terminal_job_checkpoints,
    _delete_or_count_unreferenced_definition_snapshots,
    _delete_trashed_row,
    _list_account_ids,
    _list_terminal_job_ids,
    _list_trashed_entity_ids,
    _table_exists,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

try:
    from core.infrastructure.db.job_admin_store import _JOB_CHILD_TABLES
except ModuleNotFoundError:  # pragma: no cover - lightweight unit env without psycopg_runtime
    _JOB_CHILD_TABLES = (
        "platform_events",
        "platform_action_log",
        "platform_agent_messages",
        "job_decision_overrides",
        "job_errors",
        "job_step_replays",
        "job_attachments",
    )
from core.application.jobs.artifact_cleanup_recovery import (
    resolve_artifact_storage_adapter_from_env,
)
from core.application.jobs.artifact_cleanup_runtime import (
    finalize_job_artifact_purge,
    purge_job_artifacts,
    sweep_expired_artifact_storage,
)

# The current public projection stores the marker cursor in the retained job's
# metadata.  Intermediate state markers may expire, but deleting the newest
# marker would make every active SSE reconnect fail with ``cursor_expired``.
EVENT_RETENTION_WHERE = (
    "timestamp < NOW() - (%s * INTERVAL '1 day') "
    "AND NOT ("
    "event_type = 'workflow_run.state' "
    "AND id = ("
    "SELECT marker.id FROM platform_events marker "
    "WHERE marker.job_id = platform_events.job_id "
    "AND marker.event_type = 'workflow_run.state' "
    "ORDER BY COALESCE((marker.detail ->> 'revision')::bigint, 0) DESC, "
    "marker.timestamp DESC LIMIT 1"
    ")"
    ")"
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountRetentionPolicy:
    account_id: str
    job_logs_days: int
    action_logs_days: int
    trash_days: int
    terminal_job_checkpoints_days: int
    revisions_keep: int = 5


@dataclass
class RetentionConfig:
    jobs_days: int
    job_logs_days: int
    action_logs_days: int
    events_days: int
    retry_queue_days: int
    trash_days: int
    terminal_job_checkpoints_days: int
    dry_run: bool
    retention_defaults: dict[str, int]
    revisions_keep_override: int | None = None
    job_logs_days_override: int | None = None
    action_logs_days_override: int | None = None
    trash_days_override: int | None = None
    terminal_job_checkpoints_days_override: int | None = None


def _connect_psycopg_runtime(dsn: str):
    if psycopg_runtime is None:
        raise RuntimeError("psycopg_runtime is required to connect to Postgres")
    return psycopg_runtime.connect(dsn)


def _int_env(name: str, default: int) -> int:
    raw = _runtime_env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(3650, value))


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    return max(1, min(3650, int(value)))


def _delete_trashed_job(cur, *, cache: dict[str, bool], job_id: Any) -> int:
    return _delete_trashed_job_with_artifacts(
        cur, cache=cache, job_id=job_id, artifact_storage=None
    )


def _delete_terminal_job_with_artifacts(
    cur,
    *,
    cache: dict[str, bool],
    job_id: Any,
    days: int,
    artifact_storage: Any,
    artifact_purges_after_commit: list[dict[str, Any]] | None = None,
) -> int:
    cur.execute(
        "SELECT id, project_id, meta FROM jobs WHERE id = %s "
        "AND deleted_at IS NULL AND finished_at IS NOT NULL "
        "AND finished_at < NOW() - (%s * INTERVAL '1 day') "
        "AND status = ANY(%s) FOR UPDATE",
        (job_id, days, _TERMINAL_JOB_STATUSES),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    job_row = {"id": row[0], "project_id": row[1], "meta": row[2]}
    cleanup = purge_job_artifacts(job_row, storage=artifact_storage)
    if int(cleanup.get("failed") or 0) > 0:
        raise RuntimeError(f"Artifact cleanup failed for job {job_id}: {cleanup}")

    cur.execute("SELECT thread_id FROM jobs WHERE id = %s", (job_id,))
    thread_row = cur.fetchone()
    thread_id = str(thread_row[0] or "").strip() if thread_row else ""
    if thread_id:
        _delete_checkpoint_rows_for_thread(cur, cache=cache, thread_id=thread_id)
    for table in _JOB_CHILD_TABLES:
        if _table_exists(cur, cache, table):
            cur.execute(f"DELETE FROM {table} WHERE job_id = %s", (job_id,))
    cur.execute(
        "DELETE FROM jobs WHERE id = %s AND deleted_at IS NULL "
        "AND finished_at IS NOT NULL "
        "AND finished_at < NOW() - (%s * INTERVAL '1 day') "
        "AND status = ANY(%s)",
        (job_id, days, _TERMINAL_JOB_STATUSES),
    )
    deleted = int(cur.rowcount or 0)
    if deleted and artifact_purges_after_commit is not None:
        artifact_purges_after_commit.append(job_row)
    return deleted


def _cleanup_terminal_jobs(
    cur,
    *,
    cache: dict[str, bool],
    policies: list[AccountRetentionPolicy],
    dry_run: bool,
    artifact_storage: Any,
    artifact_purges_after_commit: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    deleted = 0
    failed = 0
    for policy in policies:
        job_ids = _list_terminal_job_ids(
            cur,
            cache=cache,
            account_id=policy.account_id,
            days=policy.job_logs_days,
        )
        if dry_run:
            deleted += len(job_ids)
            continue
        for job_id in job_ids:
            cur.execute("SAVEPOINT retention_terminal_job")
            try:
                rowcount = _delete_terminal_job_with_artifacts(
                    cur,
                    cache=cache,
                    job_id=job_id,
                    days=policy.job_logs_days,
                    artifact_storage=artifact_storage,
                    artifact_purges_after_commit=artifact_purges_after_commit,
                )
                if rowcount > 0:
                    deleted += rowcount
                cur.execute("RELEASE SAVEPOINT retention_terminal_job")
            except Exception:
                failed += 1
                cur.execute("ROLLBACK TO SAVEPOINT retention_terminal_job")
                cur.execute("RELEASE SAVEPOINT retention_terminal_job")
    return deleted, failed


def _delete_trashed_job_with_artifacts(
    cur,
    *,
    cache: dict[str, bool],
    job_id: Any,
    artifact_storage: Any | None,
    artifact_purges_after_commit: list[dict[str, Any]] | None = None,
) -> int:
    purged_job: dict[str, Any] | None = None
    if artifact_storage is not None:
        # Purge artifacts BEFORE hard-deleting the DB record.
        # If purge fails we MUST NOT delete the DB row — that would create
        # permanent orphaned artifacts with no parent record to find them by.
        # Re-raise so the outer savepoint rolls back this entity's deletion.
        cur.execute("SELECT id, project_id, meta FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if row is not None:
            purged_job = (
                {"id": row[0], "project_id": row[1], "meta": row[2]}
                if len(row) >= 3
                else {"meta": row[0]}
            )
            cleanup = purge_job_artifacts(purged_job, storage=artifact_storage)
            if int(cleanup.get("failed") or 0) > 0:
                raise RuntimeError(f"Artifact cleanup failed for job {job_id}: {cleanup}")
    cur.execute("SELECT thread_id FROM jobs WHERE id = %s", (job_id,))
    thread_row = cur.fetchone()
    thread_id = str(thread_row[0] or "").strip() if thread_row else ""
    if thread_id:
        _delete_checkpoint_rows_for_thread(cur, cache=cache, thread_id=thread_id)
    for table in _JOB_CHILD_TABLES:
        if _table_exists(cur, cache, table):
            cur.execute(f"DELETE FROM {table} WHERE job_id = %s", (job_id,))
    cur.execute("DELETE FROM jobs WHERE id = %s AND deleted_at IS NOT NULL", (job_id,))
    deleted = int(cur.rowcount or 0)
    if deleted and purged_job is not None and artifact_purges_after_commit is not None:
        artifact_purges_after_commit.append(purged_job)
    return deleted


def _cleanup_trashed_entities(
    cur,
    *,
    cache: dict[str, bool],
    policies: list[AccountRetentionPolicy],
    dry_run: bool,
    artifact_storage: Any | None = None,
    artifact_purges_after_commit: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    deleted = 0
    failed = 0

    for policy in policies:
        for table, id_col in _TRASH_ENTITY_TABLES:
            entity_ids = _list_trashed_entity_ids(
                cur,
                cache=cache,
                table=table,
                id_col=id_col,
                account_id=policy.account_id,
                days=policy.trash_days,
            )
            if dry_run:
                deleted += len(entity_ids)
                continue

            for entity_id in entity_ids:
                cur.execute("SAVEPOINT retention_trash_item")
                try:
                    if table == "jobs":
                        rowcount = _delete_trashed_job_with_artifacts(
                            cur,
                            cache=cache,
                            job_id=entity_id,
                            artifact_storage=artifact_storage,
                            artifact_purges_after_commit=artifact_purges_after_commit,
                        )
                    else:
                        rowcount = _delete_trashed_row(
                            cur,
                            table=table,
                            id_col=id_col,
                            entity_id=entity_id,
                        )
                    if rowcount > 0:
                        deleted += rowcount
                    else:
                        failed += 1
                    cur.execute("RELEASE SAVEPOINT retention_trash_item")
                except Exception:
                    failed += 1
                    cur.execute("ROLLBACK TO SAVEPOINT retention_trash_item")
                    cur.execute("RELEASE SAVEPOINT retention_trash_item")

    return deleted, failed


def _load_db_retention_settings(
    dsn: str,
    *,
    defaults: dict[str, int],
    account_id: str | None = None,
) -> dict[str, Any]:
    if not dsn:
        payload = system_settings_store.normalize_retention_settings(None, defaults=defaults)
        payload["account_id"] = system_settings_store.normalize_account_id(account_id)
        return payload

    with _connect_psycopg_runtime(dsn) as conn:
        conn.autocommit = True
        cache: dict[str, bool] = {}

        def _exists(table: str) -> bool:
            with conn.cursor() as cur:
                return _table_exists(cur, cache, table)

        return system_settings_store.get_retention_settings(
            conn=conn,
            table_exists=_exists,
            defaults=defaults,
            account_id=account_id,
        )


def _resolve_account_retention_policies(
    conn,
    *,
    cfg: RetentionConfig,
    cache: dict[str, bool],
) -> list[AccountRetentionPolicy]:
    def _exists(table: str) -> bool:
        with conn.cursor() as exists_cur:
            return _table_exists(exists_cur, cache, table)

    with conn.cursor() as cur:
        account_ids = _list_account_ids(cur, cache=cache)

    policies: list[AccountRetentionPolicy] = []
    for account_id in account_ids:
        settings = system_settings_store.get_retention_settings(
            conn=conn,
            table_exists=_exists,
            defaults=cfg.retention_defaults,
            account_id=account_id,
        )
        policies.append(
            AccountRetentionPolicy(
                account_id=account_id,
                job_logs_days=(
                    cfg.job_logs_days_override
                    if cfg.job_logs_days_override is not None
                    else int(settings["job_logs_days"])
                ),
                action_logs_days=(
                    cfg.action_logs_days_override
                    if cfg.action_logs_days_override is not None
                    else int(settings["action_logs_days"])
                ),
                trash_days=(
                    cfg.trash_days_override
                    if cfg.trash_days_override is not None
                    else int(settings["trash_days"])
                ),
                terminal_job_checkpoints_days=(
                    cfg.terminal_job_checkpoints_days_override
                    if cfg.terminal_job_checkpoints_days_override is not None
                    else int(settings["terminal_job_checkpoints_days"])
                ),
                revisions_keep=(
                    cfg.revisions_keep_override
                    if cfg.revisions_keep_override is not None
                    else int(settings["revisions_keep"])
                ),
            )
        )
    return policies


def resolve_retention_config(dsn: str, args: argparse.Namespace) -> RetentionConfig:
    retention_defaults = {
        "action_logs_days": _int_env("FS_RETENTION_LOGS_DAYS", 30),
        "job_logs_days": _int_env("FS_RETENTION_MESSAGES_DAYS", 30),
        "trash_days": _int_env("FS_RETENTION_TRASH_DAYS", 30),
        "terminal_job_checkpoints_days": _int_env(
            "FS_RETENTION_TERMINAL_JOB_CHECKPOINTS_DAYS",
            30,
        ),
        "revisions_keep": _int_env("FS_RETENTION_REVISIONS_KEEP", 5),
    }
    default_account_retention = _load_db_retention_settings(
        dsn,
        defaults=retention_defaults,
        account_id=DEFAULT_ACCOUNT_ID,
    )
    job_logs_days_override = _optional_positive_int(args.job_logs_days)
    action_logs_days_override = _optional_positive_int(args.action_logs_days)
    trash_days_override = _optional_positive_int(args.trash_days)
    terminal_job_checkpoints_days_override = _optional_positive_int(
        args.terminal_job_checkpoints_days
    )
    revisions_keep_override = _optional_positive_int(getattr(args, "revisions_keep", None))

    return RetentionConfig(
        jobs_days=max(
            1,
            int(
                args.jobs_days
                if args.jobs_days is not None
                else _int_env("FS_RETENTION_JOBS_DAYS", 30)
            ),
        ),
        job_logs_days=(
            job_logs_days_override
            if job_logs_days_override is not None
            else int(default_account_retention["job_logs_days"])
        ),
        action_logs_days=(
            action_logs_days_override
            if action_logs_days_override is not None
            else int(default_account_retention["action_logs_days"])
        ),
        events_days=max(
            1,
            int(
                args.events_days
                if args.events_days is not None
                else _int_env("FS_RETENTION_EVENTS_DAYS", 30)
            ),
        ),
        retry_queue_days=max(
            1,
            int(
                args.retry_queue_days
                if args.retry_queue_days is not None
                else _int_env("FS_RETENTION_RETRY_QUEUE_DAYS", 14)
            ),
        ),
        trash_days=(
            trash_days_override
            if trash_days_override is not None
            else int(default_account_retention["trash_days"])
        ),
        terminal_job_checkpoints_days=(
            terminal_job_checkpoints_days_override
            if terminal_job_checkpoints_days_override is not None
            else int(default_account_retention["terminal_job_checkpoints_days"])
        ),
        dry_run=bool(args.dry_run),
        retention_defaults=retention_defaults,
        revisions_keep_override=revisions_keep_override,
        job_logs_days_override=job_logs_days_override,
        action_logs_days_override=action_logs_days_override,
        trash_days_override=trash_days_override,
        terminal_job_checkpoints_days_override=terminal_job_checkpoints_days_override,
    )


def run_cleanup(dsn: str, cfg: RetentionConfig) -> dict[str, int]:
    if not dsn:
        raise RuntimeError("Observability DSN is not configured.")

    stats = {
        "jobs": 0,
        "agent_messages": 0,
        "action_log": 0,
        "events": 0,
        "retry_queue": 0,
        "terminal_job_checkpoints": 0,
        "idempotency_tombstones": 0,
        "registry_changelog": 0,
        "project_settings_revisions": 0,
        "trash": 0,
        "trash_failed": 0,
        "artifacts_expired": 0,
        "datasets_expired": 0,
        "artifact_cleanup_failed": 0,
        "workflow_definition_snapshots": 0,
    }

    artifact_storage = resolve_artifact_storage_adapter_from_env(environ=_runtime_env)
    artifact_cleanup = sweep_expired_artifact_storage(
        storage=artifact_storage,
        dry_run=cfg.dry_run,
    )
    stats["artifacts_expired"] = int(artifact_cleanup.get("artifacts_expired") or 0)
    stats["datasets_expired"] = int(artifact_cleanup.get("datasets_expired") or 0)
    stats["artifact_cleanup_failed"] = int(artifact_cleanup.get("cleanup_failed") or 0)
    if stats["artifact_cleanup_failed"]:
        raise RuntimeError(f"artifact storage cleanup failed: {artifact_cleanup}")

    artifact_purges_after_commit: list[dict[str, Any]] = []
    with _connect_psycopg_runtime(dsn) as conn:
        conn.autocommit = False
        table_exists_cache: dict[str, bool] = {}
        account_policies = _resolve_account_retention_policies(
            conn,
            cfg=cfg,
            cache=table_exists_cache,
        )

        with conn.cursor() as cur:
            for policy in account_policies:
                stats["agent_messages"] += _delete_or_count_agent_messages(
                    cur,
                    cache=table_exists_cache,
                    account_id=policy.account_id,
                    days=policy.job_logs_days,
                    dry_run=cfg.dry_run,
                )
                stats["action_log"] += _delete_or_count_action_log(
                    cur,
                    cache=table_exists_cache,
                    account_id=policy.account_id,
                    days=policy.action_logs_days,
                    dry_run=cfg.dry_run,
                )
                stats["terminal_job_checkpoints"] += _delete_or_count_terminal_job_checkpoints(
                    cur,
                    cache=table_exists_cache,
                    account_id=policy.account_id,
                    days=policy.terminal_job_checkpoints_days,
                    dry_run=cfg.dry_run,
                )
                stats["registry_changelog"] += _delete_or_count_registry_changelog(
                    cur,
                    cache=table_exists_cache,
                    account_id=policy.account_id,
                    keep=policy.revisions_keep,
                    dry_run=cfg.dry_run,
                )
                stats["project_settings_revisions"] += _delete_or_count_project_settings_revisions(
                    cur,
                    cache=table_exists_cache,
                    account_id=policy.account_id,
                    keep=policy.revisions_keep,
                    dry_run=cfg.dry_run,
                )

            stats["events"] = _delete_or_count(
                cur,
                table="platform_events",
                where_sql=EVENT_RETENTION_WHERE,
                params=(cfg.events_days,),
                dry_run=cfg.dry_run,
            )

            stats["retry_queue"] = _delete_or_count(
                cur,
                table="platform_event_retry_queue",
                where_sql=(
                    "(sent_at IS NOT NULL AND sent_at < NOW() - (%s * INTERVAL '1 day')) "
                    "OR (sent_at IS NULL AND enqueued_at < NOW() - (%s * INTERVAL '1 day') "
                    "AND retry_count >= 10)"
                ),
                params=(cfg.retry_queue_days, cfg.retry_queue_days),
                dry_run=cfg.dry_run,
            )

            stats["idempotency_tombstones"] = _delete_or_count_idempotency_tombstones(
                cur,
                cache=table_exists_cache,
                dry_run=cfg.dry_run,
            )

            terminal_jobs_deleted, terminal_jobs_failed = _cleanup_terminal_jobs(
                cur,
                cache=table_exists_cache,
                policies=account_policies,
                dry_run=cfg.dry_run,
                artifact_storage=artifact_storage,
                artifact_purges_after_commit=artifact_purges_after_commit,
            )
            stats["jobs"] = terminal_jobs_deleted
            stats["artifact_cleanup_failed"] += terminal_jobs_failed

            stats["trash"], stats["trash_failed"] = _cleanup_trashed_entities(
                cur,
                cache=table_exists_cache,
                policies=account_policies,
                dry_run=cfg.dry_run,
                artifact_storage=artifact_storage,
                artifact_purges_after_commit=artifact_purges_after_commit,
            )

            # After job deletion, so archive rows those jobs alone held go too.
            stats["workflow_definition_snapshots"] = (
                _delete_or_count_unreferenced_definition_snapshots(
                    cur,
                    cache=table_exists_cache,
                    dry_run=cfg.dry_run,
                )
            )

        if cfg.dry_run:
            conn.rollback()
        else:
            conn.commit()

    for job_row in artifact_purges_after_commit:
        if not finalize_job_artifact_purge(job_row, storage=artifact_storage):
            stats["artifact_cleanup_failed"] += 1
    if stats["artifact_cleanup_failed"]:
        raise RuntimeError("artifact cleanup journal finalization failed after database commit")

    return stats


# Public alias used by integration/contract tests.
run_retention_cleanup = run_cleanup


def run_scheduled_retention(dsn: str = "") -> dict[str, int]:
    """One pass of the control-plane retention loop, with each account's own settings.

    Equivalent to running the module with no overrides; the loop owns the
    schedule, so a host cron is no longer needed for retention.
    """
    resolved_dsn = resolve_observability_dsn(dsn)
    defaults = argparse.Namespace(
        jobs_days=None,
        job_logs_days=None,
        action_logs_days=None,
        events_days=None,
        retry_queue_days=None,
        trash_days=None,
        terminal_job_checkpoints_days=None,
        revisions_keep=None,
        dry_run=False,
    )
    stats = run_cleanup(resolved_dsn, resolve_retention_config(resolved_dsn, defaults))
    logger.info(
        "Retention pass removed %s",
        ", ".join(f"{key}={value}" for key, value in stats.items() if value) or "nothing",
        extra={"retention_stats": stats},
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cleanup old jobs/messages/logs/events by retention policy."
    )
    parser.add_argument("--dsn", default="", help="Postgres DSN override")
    parser.add_argument("--jobs-days", type=int, default=None)
    parser.add_argument(
        "--messages-days",
        "--job-logs-days",
        dest="job_logs_days",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--logs-days",
        "--action-logs-days",
        dest="action_logs_days",
        type=int,
        default=None,
    )
    parser.add_argument("--events-days", type=int, default=None)
    parser.add_argument("--retry-queue-days", type=int, default=None)
    parser.add_argument("--trash-days", type=int, default=None)
    parser.add_argument("--terminal-job-checkpoints-days", type=int, default=None)
    parser.add_argument(
        "--revisions-keep",
        type=int,
        default=None,
        help="Revisions kept per entity, overriding every account's setting",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show deletion counts without committing"
    )
    args = parser.parse_args()

    dsn = resolve_observability_dsn(args.dsn)
    cfg = resolve_retention_config(dsn, args)
    stats = run_cleanup(dsn, cfg)

    mode = "DRY-RUN" if cfg.dry_run else "APPLIED"
    print(f"[retention] mode={mode}")
    print(f"[retention] jobs={stats['jobs']}")
    print(f"[retention] agent_messages={stats['agent_messages']}")
    print(f"[retention] action_log={stats['action_log']}")
    print(f"[retention] events={stats['events']}")
    print(f"[retention] retry_queue={stats['retry_queue']}")
    print(f"[retention] terminal_job_checkpoints={stats['terminal_job_checkpoints']}")
    print(f"[retention] idempotency_tombstones={stats['idempotency_tombstones']}")
    print(f"[retention] registry_changelog={stats['registry_changelog']}")
    print(f"[retention] project_settings_revisions={stats['project_settings_revisions']}")
    print(f"[retention] trash={stats['trash']}")
    print(f"[retention] trash_failed={stats['trash_failed']}")
    print(f"[retention] artifacts_expired={stats['artifacts_expired']}")
    print(f"[retention] datasets_expired={stats['datasets_expired']}")
    print(f"[retention] artifact_cleanup_failed={stats['artifact_cleanup_failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
