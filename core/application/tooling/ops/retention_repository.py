"""Retention queries using the caller-owned cursor, transaction and schema cache."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.tooling.ops.retention_cleanup_common import (
    psycopg_runtime,
    system_settings_store,
)

_TERMINAL_JOB_STATUSES = [
    "completed",
    "failed",
    "max_iterations",
    "cancelled",
]


TIMESTAMP_RETENTION_WHERE = "timestamp < NOW() - (%s * INTERVAL '1 day')"


_TRASH_ENTITY_TABLES: tuple[tuple[str, str], ...] = (
    ("project_settings_revisions", "id"),
    ("jobs", "id"),
    ("projects", "id"),
    ("users", "id"),
    ("registry_workflows", "workflow_id"),
    ("registry_guardrails", "id"),
    ("registry_skills", "skill_id"),
    ("registry_tools", "tool_id"),
    ("registry_agents", "agent_id"),
    ("registry_data_contracts", "contract_id"),
)


_TRASH_GLOBAL_ONLY_TABLES = frozenset(
    {
        "registry_workflows",
        "registry_guardrails",
        "registry_skills",
        "registry_tools",
        "registry_agents",
        "registry_data_contracts",
    }
)


# Rows that are not content revisions: lifecycle markers, and the dataset
# project-assignment records that restoring a deleted project reads back.
_NON_REVISION_CHANGELOG_SQL = (
    "action NOT IN ('trash', 'restore', 'hard_delete') AND action NOT LIKE 'project_assignment%%'"
)


def _count(cur, sql: str, params: tuple[Any, ...]) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    return int(row[0] if row else 0)


def _delete_or_count(
    cur,
    *,
    table: str,
    where_sql: str,
    params: tuple[Any, ...],
    dry_run: bool,
) -> int:
    if dry_run:
        count_query = "".join(["SELECT COUNT(*) FROM ", table, " WHERE ", where_sql])
        return _count(cur, count_query, params)
    delete_query = "".join(["DELETE FROM ", table, " WHERE ", where_sql])
    cur.execute(delete_query, params)
    return int(cur.rowcount or 0)


def _table_exists(cur, cache: dict[str, bool], table: str) -> bool:
    key = "".join(["table:", table])
    if key in cache:
        return cache[key]
    cur.execute("SELECT to_regclass(%s)", (str(table or "").strip(),))
    row = cur.fetchone()
    exists = bool(row and row[0])
    cache[key] = exists
    return exists


def _column_exists(cur, cache: dict[str, bool], *, table: str, column: str) -> bool:
    key = "".join(["column:", table, ".", column])
    if key in cache:
        return cache[key]
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (str(table or "").strip(), str(column or "").strip()),
    )
    exists = cur.fetchone() is not None
    cache[key] = exists
    return exists


def _delete_or_count_idempotency_tombstones(
    cur,
    *,
    cache: dict[str, bool],
    dry_run: bool,
) -> int:
    table = "request_idempotency"
    if not _table_exists(cur, cache, table) or not _column_exists(
        cur,
        cache,
        table=table,
        column="tombstone_expires_at",
    ):
        return 0
    return _delete_or_count(
        cur,
        table=table,
        where_sql=(
            "tombstone_expires_at <= NOW() "
            "AND (job_id IS NULL OR NOT EXISTS (SELECT 1 FROM jobs "
            "WHERE jobs.id = request_idempotency.job_id))"
        ),
        params=(),
        dry_run=dry_run,
    )


def _delete_or_count_beyond_newest(
    cur,
    *,
    table: str,
    partition_sql: str,
    scope_sql: str,
    params: tuple[Any, ...],
    keep: int,
    dry_run: bool,
) -> int:
    ranked = f"""
        SELECT ranked.id FROM (
            SELECT candidate.id,
                   row_number() OVER (
                       PARTITION BY {partition_sql}
                       ORDER BY candidate.created_at DESC, candidate.id DESC
                   ) AS position
            FROM {table} AS candidate
            WHERE {scope_sql}
        ) AS ranked
        WHERE ranked.position > %s
    """  # nosec B608 - fixed table, partition and scope fragments; values are bound.
    if dry_run:
        cur.execute(f"SELECT COUNT(*) FROM ({ranked}) AS expired", (*params, keep))  # nosec B608
        row = cur.fetchone()
        return int(row[0] if row else 0)
    cur.execute(f"DELETE FROM {table} WHERE id IN ({ranked})", (*params, keep))  # nosec B608
    return int(cur.rowcount or 0)


def _delete_or_count_registry_changelog(
    cur,
    *,
    cache: dict[str, bool],
    account_id: str,
    keep: int,
    dry_run: bool,
) -> int:
    """Keep the newest ``keep`` content revisions of each registry entity in one account."""
    if not _table_exists(cur, cache, "registry_changelog"):
        return 0
    return _delete_or_count_beyond_newest(
        cur,
        table="registry_changelog",
        partition_sql="candidate.entity_type, candidate.entity_id",
        scope_sql=(
            "COALESCE(candidate.account_id::text, %s) = %s AND " + _NON_REVISION_CHANGELOG_SQL
        ),
        params=(DEFAULT_ACCOUNT_ID, account_id),
        keep=keep,
        dry_run=dry_run,
    )


def _delete_or_count_project_settings_revisions(
    cur,
    *,
    cache: dict[str, bool],
    account_id: str,
    keep: int,
    dry_run: bool,
) -> int:
    """Keep the newest ``keep`` live settings revisions of each project in one account.

    Trashed revisions are left to the trash policy.
    """
    if not _table_exists(cur, cache, "project_settings_revisions"):
        return 0
    scope_sql = "candidate.deleted_at IS NULL"
    params: tuple[Any, ...] = ()
    if _project_account_scoping_available(cur, cache):
        scope_sql += (
            " AND candidate.project_id IN (SELECT id FROM projects WHERE account_id::text = %s)"
        )
        params = (account_id,)
    elif account_id != DEFAULT_ACCOUNT_ID:
        return 0
    return _delete_or_count_beyond_newest(
        cur,
        table="project_settings_revisions",
        partition_sql="candidate.project_id",
        scope_sql=scope_sql,
        params=params,
        keep=keep,
        dry_run=dry_run,
    )


def _project_account_scoping_available(cur, cache: dict[str, bool]) -> bool:
    return _table_exists(cur, cache, "projects") and _column_exists(
        cur,
        cache,
        table="projects",
        column="account_id",
    )


def _list_account_ids(cur, *, cache: dict[str, bool]) -> list[str]:
    if not _table_exists(cur, cache, "accounts"):
        return [DEFAULT_ACCOUNT_ID]
    account_column = "account_id"
    if not _column_exists(cur, cache, table="accounts", column=account_column):
        account_column = "id"
    cur.execute(f"SELECT {account_column} FROM accounts ORDER BY {account_column} ASC")
    account_ids = [
        system_settings_store.normalize_account_id(row[0])
        for row in (cur.fetchall() or [])
        if row and str(row[0] or "").strip()
    ]
    if DEFAULT_ACCOUNT_ID not in account_ids:
        account_ids.insert(0, DEFAULT_ACCOUNT_ID)
    deduped = list(dict.fromkeys(account_ids))
    return deduped or [DEFAULT_ACCOUNT_ID]


def _list_trashed_entity_ids(
    cur,
    *,
    cache: dict[str, bool],
    table: str,
    id_col: str,
    account_id: str,
    days: int,
) -> list[Any]:
    if not _table_exists(cur, cache, table):
        return []

    resolved_account_id = system_settings_store.normalize_account_id(account_id)
    if table == "project_settings_revisions":
        if _project_account_scoping_available(cur, cache):
            cur.execute(
                """
                SELECT psr.id
                FROM project_settings_revisions AS psr
                JOIN projects AS p ON p.id = psr.project_id
                WHERE p.account_id = %s
                  AND psr.deleted_at IS NOT NULL
                  AND psr.deleted_at < NOW() - (%s * INTERVAL '1 day')
                ORDER BY psr.deleted_at ASC
                """,
                (resolved_account_id, days),
            )
        elif resolved_account_id == DEFAULT_ACCOUNT_ID:
            cur.execute(
                """
                SELECT id
                FROM project_settings_revisions
                WHERE deleted_at IS NOT NULL
                  AND deleted_at < NOW() - (%s * INTERVAL '1 day')
                ORDER BY deleted_at ASC
                """,
                (days,),
            )
        else:
            return []
    elif table == "jobs":
        if _project_account_scoping_available(cur, cache):
            cur.execute(
                """
                SELECT j.id
                FROM jobs AS j
                JOIN projects AS p ON p.id = j.project_id
                WHERE p.account_id = %s
                  AND j.deleted_at IS NOT NULL
                  AND j.deleted_at < NOW() - (%s * INTERVAL '1 day')
                ORDER BY j.deleted_at ASC
                """,
                (resolved_account_id, days),
            )
        elif resolved_account_id == DEFAULT_ACCOUNT_ID:
            cur.execute(
                """
                SELECT id
                FROM jobs
                WHERE deleted_at IS NOT NULL
                  AND deleted_at < NOW() - (%s * INTERVAL '1 day')
                ORDER BY deleted_at ASC
                """,
                (days,),
            )
        else:
            return []
    elif table == "projects":
        if _column_exists(cur, cache, table="projects", column="account_id"):
            cur.execute(
                """
                SELECT id
                FROM projects
                WHERE account_id = %s
                  AND deleted_at IS NOT NULL
                  AND deleted_at < NOW() - (%s * INTERVAL '1 day')
                ORDER BY deleted_at ASC
                """,
                (resolved_account_id, days),
            )
        elif resolved_account_id == DEFAULT_ACCOUNT_ID:
            cur.execute(
                """
                SELECT id
                FROM projects
                WHERE deleted_at IS NOT NULL
                  AND deleted_at < NOW() - (%s * INTERVAL '1 day')
                ORDER BY deleted_at ASC
                """,
                (days,),
            )
        else:
            return []
    elif table == "users":
        if _column_exists(cur, cache, table="users", column="account_id"):
            cur.execute(
                """
                SELECT id
                FROM users
                WHERE account_id = %s
                  AND deleted_at IS NOT NULL
                  AND deleted_at < NOW() - (%s * INTERVAL '1 day')
                ORDER BY deleted_at ASC
                """,
                (resolved_account_id, days),
            )
        elif resolved_account_id == DEFAULT_ACCOUNT_ID:
            cur.execute(
                """
                SELECT id
                FROM users
                WHERE deleted_at IS NOT NULL
                  AND deleted_at < NOW() - (%s * INTERVAL '1 day')
                ORDER BY deleted_at ASC
                """,
                (days,),
            )
        else:
            return []
    elif table in _TRASH_GLOBAL_ONLY_TABLES:
        if resolved_account_id != DEFAULT_ACCOUNT_ID:
            return []
        cur.execute(
            f"""
            SELECT {id_col}
            FROM {table}
            WHERE deleted_at IS NOT NULL
              AND deleted_at < NOW() - (%s * INTERVAL '1 day')
            ORDER BY deleted_at ASC
            """,
            (days,),
        )
    else:
        cur.execute(
            f"""
            SELECT {id_col}
            FROM {table}
            WHERE deleted_at IS NOT NULL
              AND deleted_at < NOW() - (%s * INTERVAL '1 day')
            ORDER BY deleted_at ASC
            """,
            (days,),
        )

    return [row[0] for row in (cur.fetchall() or []) if row and row[0] is not None]


def _invoke_child_held_sql(alias: str) -> str:
    """The job ``alias`` is a child its still-running parent has not consumed.

    A serial ``invoke_workflow`` parent reads its child's outputs, artifacts,
    messages and checkpoint after the child finished; until the parent copied
    the result (``consumed_at`` on its link) or itself ended, nothing of the
    child is removed. Then normal retention applies.
    """
    return (
        "EXISTS (SELECT 1 FROM jobs AS hold_parent "
        f"WHERE hold_parent.id::text = {alias}.meta->>'invoke_parent_job_id' "
        "AND hold_parent.status NOT IN ('completed', 'failed', 'cancelled', 'max_iterations') "
        "AND NOT COALESCE("
        f"(hold_parent.meta->'invoke_children'->({alias}.id::text)) ? 'consumed_at', false))"
    )


def _invoke_child_hold_sql(alias: str) -> str:
    return f"AND NOT {_invoke_child_held_sql(alias)}"


def _list_terminal_job_ids(
    cur,
    *,
    cache: dict[str, bool],
    account_id: str,
    days: int,
) -> list[Any]:
    if not _table_exists(cur, cache, "jobs"):
        return []
    resolved_account_id = system_settings_store.normalize_account_id(account_id)
    where_sql = (
        "finished_at IS NOT NULL "
        "AND finished_at < NOW() - (%s * INTERVAL '1 day') "
        "AND status = ANY(%s) "
        "AND deleted_at IS NULL "
        f"{_invoke_child_hold_sql('jobs')}"
    )
    if _column_exists(cur, cache, table="jobs", column="account_id"):
        cur.execute(
            f"SELECT id FROM jobs WHERE account_id = %s AND {where_sql} ORDER BY finished_at ASC",
            (resolved_account_id, days, _TERMINAL_JOB_STATUSES),
        )
    elif _project_account_scoping_available(cur, cache):
        joined_where_sql = (
            "j.finished_at IS NOT NULL "
            "AND j.finished_at < NOW() - (%s * INTERVAL '1 day') "
            "AND j.status = ANY(%s) "
            "AND j.deleted_at IS NULL "
            f"{_invoke_child_hold_sql('j')}"
        )
        cur.execute(
            f"""
            SELECT j.id
            FROM jobs AS j
            JOIN projects AS p ON p.id = j.project_id
            WHERE p.account_id = %s AND {joined_where_sql}
            ORDER BY j.finished_at ASC
            """,
            (resolved_account_id, days, _TERMINAL_JOB_STATUSES),
        )
    elif resolved_account_id == DEFAULT_ACCOUNT_ID:
        cur.execute(
            f"SELECT id FROM jobs WHERE {where_sql} ORDER BY finished_at ASC",
            (days, _TERMINAL_JOB_STATUSES),
        )
    else:
        return []
    return [row[0] for row in (cur.fetchall() or []) if row and row[0] is not None]


def _delete_trashed_row(cur, *, table: str, id_col: str, entity_id: Any) -> int:
    cur.execute(
        f"DELETE FROM {table} WHERE {id_col} = %s AND deleted_at IS NOT NULL",
        (entity_id,),
    )
    return int(cur.rowcount or 0)


def _delete_or_count_agent_messages(
    cur,
    *,
    cache: dict[str, bool],
    account_id: str,
    days: int,
    dry_run: bool,
) -> int:
    if not _project_account_scoping_available(cur, cache):
        if system_settings_store.normalize_account_id(account_id) != DEFAULT_ACCOUNT_ID:
            return 0
        return _delete_or_count(
            cur,
            table="platform_agent_messages",
            where_sql=TIMESTAMP_RETENTION_WHERE,
            params=(days,),
            dry_run=dry_run,
        )

    return _delete_or_count(
        cur,
        table="platform_agent_messages AS m",
        where_sql="""
            m.timestamp < NOW() - (%s * INTERVAL '1 day')
            AND EXISTS (
                SELECT 1
                FROM jobs AS j
                JOIN projects AS p ON p.id = j.project_id
                WHERE j.id = m.job_id
                  AND p.account_id = %s
                  AND NOT """
        + _invoke_child_held_sql("j")
        + """
            )
        """,
        params=(days, system_settings_store.normalize_account_id(account_id)),
        dry_run=dry_run,
    )


def _delete_or_count_action_log(
    cur,
    *,
    cache: dict[str, bool],
    account_id: str,
    days: int,
    dry_run: bool,
) -> int:
    if not _project_account_scoping_available(cur, cache):
        if system_settings_store.normalize_account_id(account_id) != DEFAULT_ACCOUNT_ID:
            return 0
        return _delete_or_count(
            cur,
            table="platform_action_log",
            where_sql=TIMESTAMP_RETENTION_WHERE,
            params=(days,),
            dry_run=dry_run,
        )

    resolved_account_id = system_settings_store.normalize_account_id(account_id)
    params: tuple[Any, ...]
    where_sql: str
    if resolved_account_id == DEFAULT_ACCOUNT_ID:
        # Some early action-log rows may not resolve to an account via job/project.
        # In that case we treat them as default-account data until those tables are
        # fully account-scoped too.
        params = (days, resolved_account_id, resolved_account_id)
        where_sql = """
            al.timestamp < NOW() - (%s * INTERVAL '1 day')
            AND (
                EXISTS (
                    SELECT 1
                    FROM jobs AS j
                    JOIN projects AS p ON p.id = j.project_id
                    WHERE j.id = al.job_id
                      AND p.account_id = %s
                )
                OR (
                    al.job_id IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM projects AS p
                        WHERE p.id = al.project_id
                          AND p.account_id = %s
                    )
                )
                OR (
                    NOT EXISTS (
                        SELECT 1
                        FROM jobs AS j
                        JOIN projects AS p ON p.id = j.project_id
                        WHERE j.id = al.job_id
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM projects AS p
                        WHERE p.id = al.project_id
                    )
                )
            )
        """
    else:
        params = (days, resolved_account_id, resolved_account_id)
        where_sql = """
            al.timestamp < NOW() - (%s * INTERVAL '1 day')
            AND (
                EXISTS (
                    SELECT 1
                    FROM jobs AS j
                    JOIN projects AS p ON p.id = j.project_id
                    WHERE j.id = al.job_id
                      AND p.account_id = %s
                )
                OR (
                    al.job_id IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM projects AS p
                        WHERE p.id = al.project_id
                          AND p.account_id = %s
                    )
                )
            )
        """

    return _delete_or_count(
        cur,
        table="platform_action_log AS al",
        where_sql=where_sql,
        params=params,
        dry_run=dry_run,
    )


def _delete_or_count_unreferenced_definition_snapshots(
    cur,
    *,
    cache: dict[str, bool],
    dry_run: bool,
) -> int:
    """Delete pinned workflow definitions that no job references any more.

    Checkpoint and changelog retention never touch the archive; only this
    branch does, and only for rows without a job reference (soft-deleted jobs
    still reference theirs). A row a launch is pinning right now is locked by
    it and skipped. If a launch commits its reference while this statement
    runs, the RESTRICT foreign key refuses the delete; that pass then removes
    nothing and the next one decides again.
    """
    if not _table_exists(cur, cache, "workflow_definition_snapshots"):
        return 0
    unreferenced = (
        "NOT EXISTS (SELECT 1 FROM jobs j "
        "WHERE j.workflow_definition_snapshot_id = workflow_definition_snapshots.id)"
    )
    if dry_run:
        return _count(
            cur,
            "SELECT COUNT(*) FROM workflow_definition_snapshots WHERE " + unreferenced,
            (),
        )
    cur.execute("SAVEPOINT definition_snapshot_retention")
    try:
        cur.execute(
            "DELETE FROM workflow_definition_snapshots WHERE id IN ("
            "SELECT id FROM workflow_definition_snapshots WHERE "
            + unreferenced
            + " FOR UPDATE SKIP LOCKED)"
        )
        deleted = int(cur.rowcount or 0)
    except Exception as exc:
        sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        if sqlstate != psycopg_runtime.errorcodes.FOREIGN_KEY_VIOLATION:
            raise
        cur.execute("ROLLBACK TO SAVEPOINT definition_snapshot_retention")
        return 0
    cur.execute("RELEASE SAVEPOINT definition_snapshot_retention")
    return deleted


def _delete_checkpoint_rows_for_thread(cur, *, cache: dict[str, bool], thread_id: str) -> int:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return 0
    deleted = 0
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        if _table_exists(cur, cache, table):
            cur.execute(
                f"DELETE FROM {table} WHERE thread_id = %s",
                (normalized_thread_id,),
            )
            deleted += int(cur.rowcount or 0)
    return deleted


def _delete_or_count_terminal_job_checkpoints(
    cur,
    *,
    cache: dict[str, bool],
    account_id: str,
    days: int,
    dry_run: bool,
) -> int:
    if not (
        _table_exists(cur, cache, "jobs")
        and _column_exists(cur, cache, table="jobs", column="thread_id")
    ):
        return 0

    checkpoint_tables = [
        table
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
        if _table_exists(cur, cache, table)
    ]
    if not checkpoint_tables:
        return 0

    resolved_account_id = system_settings_store.normalize_account_id(account_id)
    jobs_where = (
        """
        j.thread_id IS NOT NULL
        AND j.thread_id <> ''
        AND j.finished_at IS NOT NULL
        AND j.finished_at < NOW() - (%s * INTERVAL '1 day')
        AND j.status = ANY(%s)
    """
        + f"""
        {_invoke_child_hold_sql("j")}
    """
    )
    jobs_params: list[Any] = [days, _TERMINAL_JOB_STATUSES]
    if _project_account_scoping_available(cur, cache):
        jobs_where += """
        AND EXISTS (
            SELECT 1
            FROM projects AS p
            WHERE p.id = j.project_id
              AND p.account_id = %s
        )
        """
        jobs_params.append(resolved_account_id)
    elif resolved_account_id != DEFAULT_ACCOUNT_ID:
        return 0

    total = 0
    for table in checkpoint_tables:
        total += _delete_or_count(
            cur,
            table=table,
            where_sql=f"""
                thread_id IN (
                    SELECT j.thread_id
                    FROM jobs AS j
                    WHERE {jobs_where}
                )
            """,
            params=tuple(jobs_params),
            dry_run=dry_run,
        )
    return total
