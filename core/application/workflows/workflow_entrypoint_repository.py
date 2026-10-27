"""Persistence helpers for workflow-owned webhook entrypoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.db.psycopg_runtime import sql as psycopg_sql


def _row_to_dict(cursor, row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, Mapping):
        return dict(row.items())
    if isinstance(row, (list, tuple)):
        columns = [str(item[0]) for item in (cursor.description or [])]
        if columns and len(columns) == len(row):
            return {columns[idx]: row[idx] for idx in range(len(columns))}
    return {}


def load_workflow_entrypoint(
    bl, entry_key: str, *, include_trashed: bool = False
) -> dict[str, Any]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                entrypoint_id,
                workflow_id,
                project_id,
                entry_kind,
                entry_key,
                enabled,
                auth_mode,
                auth_config_jsonb,
                source_restrictions_jsonb,
                allowed_content_types_jsonb,
                max_body_bytes,
                max_file_bytes,
                max_files,
                secret_ref,
                idempotency_mode,
                input_mapping_jsonb,
                response_mode,
                description,
                account_id
            FROM workflow_entrypoints
            WHERE entry_key = %s
              AND (%s OR EXISTS (SELECT 1 FROM registry_workflows w
                   WHERE w.workflow_id = workflow_entrypoints.workflow_id AND w.deleted_at IS NULL))
            LIMIT 1
            """,
            (str(entry_key or "").strip(), include_trashed),
        )
        return _row_to_dict(cur, cur.fetchone())


def list_workflow_entrypoints_for_workflow(
    bl,
    workflow_id: str,
) -> list[dict[str, Any]]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                entrypoint_id,
                workflow_id,
                project_id,
                entry_kind,
                entry_key,
                enabled,
                auth_mode,
                auth_config_jsonb,
                source_restrictions_jsonb,
                allowed_content_types_jsonb,
                max_body_bytes,
                max_file_bytes,
                max_files,
                secret_ref,
                idempotency_mode,
                input_mapping_jsonb,
                response_mode,
                description,
                account_id,
                created_at,
                updated_at
            FROM workflow_entrypoints
            WHERE workflow_id = %s
            ORDER BY created_at ASC, entry_key ASC
            """,
            (str(workflow_id or "").strip(),),
        )
        return [_row_to_dict(cur, row) for row in (cur.fetchall() or [])]


def list_project_workflow_entrypoints(
    bl,
    project_id: str,
    *,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    conditions = [
        psycopg_sql.SQL("pw.project_id = %s"),
        psycopg_sql.SQL("pw.deleted_at IS NULL"),
        psycopg_sql.SQL(
            "EXISTS (SELECT 1 FROM registry_workflows w WHERE w.workflow_id = we.workflow_id AND w.deleted_at IS NULL)"
        ),
    ]
    if enabled_only:
        conditions.extend(
            [
                psycopg_sql.SQL("pw.enabled = true"),
                psycopg_sql.SQL("we.enabled = true"),
            ]
        )
    where_clause = psycopg_sql.SQL(" AND ").join(conditions)
    with bl.conn.cursor() as cur:
        cur.execute(
            psycopg_sql.SQL(
                """
            SELECT
                we.entrypoint_id,
                we.workflow_id,
                we.project_id,
                we.entry_kind,
                we.entry_key,
                we.enabled,
                we.auth_mode,
                we.auth_config_jsonb,
                we.source_restrictions_jsonb,
                we.allowed_content_types_jsonb,
                we.max_body_bytes,
                we.max_file_bytes,
                we.max_files,
                we.secret_ref,
                we.idempotency_mode,
                we.input_mapping_jsonb,
                we.response_mode,
                we.description,
                we.account_id,
                we.created_at,
                we.updated_at
            FROM workflow_entrypoints we
            JOIN project_workflows pw
              ON pw.workflow_id = we.workflow_id
             AND pw.project_id = we.project_id
            WHERE {where_clause}
            ORDER BY we.entry_key
            """
            ).format(where_clause=where_clause),
            (str(project_id),),
        )
        return [_row_to_dict(cur, row) for row in (cur.fetchall() or [])]


def resolve_project_run_config_for_workflow(
    bl, workflow_id: str
) -> tuple[str | None, dict[str, Any]]:
    clean_workflow_id = str(workflow_id or "").strip()
    if not clean_workflow_id:
        return None, {}
    with bl.conn.cursor() as cur:
        # Join projects and exclude soft-deleted ones: an enabled project_workflows
        # row can outlive its project (soft-deleted), and binding a webhook to a
        # deleted project makes it accept public traffic while every job dies with
        # "Project lookup unavailable".
        cur.execute(
            """
            SELECT pw.project_id
            FROM project_workflows pw
            JOIN projects p
              ON p.id = pw.project_id
             AND p.deleted_at IS NULL
            WHERE pw.workflow_id = %s
              AND pw.enabled = true
              AND pw.deleted_at IS NULL
            ORDER BY pw.is_default DESC, pw.priority DESC, pw.project_id
            LIMIT 1
            """,
            (clean_workflow_id,),
        )
        row = _row_to_dict(cur, cur.fetchone())
    project_id = str(row.get("project_id") or "").strip() if row else ""
    if not project_id:
        return None, {}
    run_cfg = {}
    if hasattr(bl, "get_project_run_config"):
        try:
            run_cfg = dict(bl.get_project_run_config(project_id) or {})
        except Exception:
            run_cfg = {}
    if not run_cfg and hasattr(bl, "get_project"):
        project = bl.get_project(project_id) or {}
        run_cfg = {
            "project_name": str(project.get("name") or "").strip(),
        }
    return project_id, run_cfg


def resolve_project_run_config_for_workflow_project(
    bl,
    workflow_id: str,
    project_id: str,
) -> tuple[str | None, dict[str, Any]]:
    clean_workflow_id = str(workflow_id or "").strip()
    clean_project_id = str(project_id or "").strip()
    if not clean_workflow_id or not clean_project_id:
        return None, {}
    with bl.conn.cursor() as cur:
        # Exclude soft-deleted projects (see resolve_project_run_config_for_workflow):
        # an explicit binding to a deleted project must not resolve.
        cur.execute(
            """
            SELECT pw.project_id
            FROM project_workflows pw
            JOIN projects p
              ON p.id = pw.project_id
             AND p.deleted_at IS NULL
            WHERE pw.workflow_id = %s
              AND pw.project_id = %s
              AND pw.enabled = true
              AND pw.deleted_at IS NULL
            LIMIT 1
            """,
            (clean_workflow_id, clean_project_id),
        )
        row = _row_to_dict(cur, cur.fetchone())
    if str(row.get("project_id") or "").strip() != clean_project_id:
        return None, {}
    run_cfg = {}
    if hasattr(bl, "get_project_run_config"):
        run_cfg = dict(bl.get_project_run_config(clean_project_id) or {})
    if not run_cfg and hasattr(bl, "get_project"):
        project = bl.get_project(clean_project_id) or {}
        run_cfg = {
            "project_name": str(project.get("name") or "").strip(),
        }
    return clean_project_id, run_cfg


def create_workflow_entrypoint(
    bl,
    *,
    workflow_id: str,
    project_id: str,
    entry_kind: str,
    entry_key: str,
    enabled: bool,
    auth_mode: str,
    auth_config_jsonb: dict[str, Any],
    source_restrictions_jsonb: dict[str, Any],
    allowed_content_types_jsonb: list[str],
    max_body_bytes: int,
    max_file_bytes: int,
    max_files: int,
    secret_ref: str,
    idempotency_mode: str,
    input_mapping_jsonb: dict[str, Any],
    response_mode: str,
    description: str,
    account_id: str | None = None,
) -> dict[str, Any]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_entrypoints (
                workflow_id,
                project_id,
                entry_kind,
                entry_key,
                enabled,
                auth_mode,
                auth_config_jsonb,
                source_restrictions_jsonb,
                allowed_content_types_jsonb,
                max_body_bytes,
                max_file_bytes,
                max_files,
                secret_ref,
                idempotency_mode,
                input_mapping_jsonb,
                response_mode,
                description,
                account_id
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                COALESCE(%s::uuid, '00000000-0000-0000-0000-000000000002'::uuid)
            )
            RETURNING
                entrypoint_id,
                workflow_id,
                project_id,
                entry_kind,
                entry_key,
                enabled,
                auth_mode,
                auth_config_jsonb,
                source_restrictions_jsonb,
                allowed_content_types_jsonb,
                max_body_bytes,
                max_file_bytes,
                max_files,
                secret_ref,
                idempotency_mode,
                input_mapping_jsonb,
                response_mode,
                description,
                account_id,
                created_at,
                updated_at
            """,
            (
                str(workflow_id or "").strip(),
                str(project_id or "").strip(),
                str(entry_kind or "webhook").strip() or "webhook",
                str(entry_key or "").strip(),
                bool(enabled),
                str(auth_mode or "bearer_token").strip() or "bearer_token",
                json.dumps(dict(auth_config_jsonb or {})),
                json.dumps(dict(source_restrictions_jsonb or {})),
                json.dumps(list(allowed_content_types_jsonb or [])),
                int(max_body_bytes or 1048576),
                int(max_file_bytes or 10485760),
                int(max_files or 0),
                str(secret_ref or "").strip(),
                str(idempotency_mode or "none").strip() or "none",
                json.dumps(dict(input_mapping_jsonb or {})),
                str(response_mode or "accepted").strip() or "accepted",
                str(description or "").strip(),
                str(account_id or "").strip() or None,
            ),
        )
        return _row_to_dict(cur, cur.fetchone())


def update_workflow_entrypoint(
    bl,
    *,
    entry_key: str,
    project_id: str,
    enabled: bool,
    auth_mode: str,
    auth_config_jsonb: dict[str, Any],
    source_restrictions_jsonb: dict[str, Any],
    allowed_content_types_jsonb: list[str],
    max_body_bytes: int,
    max_file_bytes: int,
    max_files: int,
    secret_ref: str,
    idempotency_mode: str,
    input_mapping_jsonb: dict[str, Any],
    response_mode: str,
    description: str,
) -> dict[str, Any]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_entrypoints
               SET project_id = %s,
                   enabled = %s,
                   auth_mode = %s,
                   auth_config_jsonb = %s,
                   source_restrictions_jsonb = %s,
                   allowed_content_types_jsonb = %s,
                   max_body_bytes = %s,
                   max_file_bytes = %s,
                   max_files = %s,
                   secret_ref = %s,
                   idempotency_mode = %s,
                   input_mapping_jsonb = %s,
                   response_mode = %s,
                   description = %s,
                   updated_at = now()
             WHERE entry_key = %s
         RETURNING
                entrypoint_id,
                workflow_id,
                project_id,
                entry_kind,
                entry_key,
                enabled,
                auth_mode,
                auth_config_jsonb,
                source_restrictions_jsonb,
                allowed_content_types_jsonb,
                max_body_bytes,
                max_file_bytes,
                max_files,
                secret_ref,
                idempotency_mode,
                input_mapping_jsonb,
                response_mode,
                description,
                account_id,
                created_at,
                updated_at
            """,
            (
                str(project_id or "").strip(),
                bool(enabled),
                str(auth_mode or "bearer_token").strip() or "bearer_token",
                json.dumps(dict(auth_config_jsonb or {})),
                json.dumps(dict(source_restrictions_jsonb or {})),
                json.dumps(list(allowed_content_types_jsonb or [])),
                int(max_body_bytes or 1048576),
                int(max_file_bytes or 10485760),
                int(max_files or 0),
                str(secret_ref or "").strip(),
                str(idempotency_mode or "none").strip() or "none",
                json.dumps(dict(input_mapping_jsonb or {})),
                str(response_mode or "accepted").strip() or "accepted",
                str(description or "").strip(),
                str(entry_key or "").strip(),
            ),
        )
        return _row_to_dict(cur, cur.fetchone())


def delete_workflow_entrypoint(bl, *, entry_key: str) -> bool:
    clean_entry_key = str(entry_key or "").strip()
    with transaction_connection(bl.conn) as transaction_conn, transaction_conn.cursor() as cur:
        cur.execute(
            """
                DELETE FROM workflow_entrypoints
                 WHERE entry_key = %s
                """,
            (clean_entry_key,),
        )
        return bool(cur.rowcount)


def regenerate_workflow_entrypoint_key(
    bl,
    *,
    entry_key: str,
    next_entry_key: str,
) -> dict[str, Any]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_entrypoints
               SET entry_key = %s,
                   updated_at = now()
             WHERE entry_key = %s
         RETURNING
                entrypoint_id,
                workflow_id,
                project_id,
                entry_kind,
                entry_key,
                enabled,
                auth_mode,
                auth_config_jsonb,
                source_restrictions_jsonb,
                allowed_content_types_jsonb,
                max_body_bytes,
                max_file_bytes,
                max_files,
                secret_ref,
                idempotency_mode,
                input_mapping_jsonb,
                response_mode,
                description,
                account_id,
                created_at,
                updated_at
            """,
            (str(next_entry_key or "").strip(), str(entry_key or "").strip()),
        )
        return _row_to_dict(cur, cur.fetchone())


def list_workflow_entrypoint_deliveries(
    bl,
    *,
    workflow_id: str,
    entry_key: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                id,
                project_id,
                task_id,
                task_summary,
                "timestamp",
                status,
                payload,
                result,
                error,
                meta
            FROM platform_action_log
            WHERE agent = 'workflow_entrypoint'
              AND action_type = 'workflow_entrypoint.invoke'
              AND COALESCE(payload ->> 'workflow_id', '') = %s
              AND COALESCE(payload ->> 'entry_key', '') = %s
            ORDER BY "timestamp" DESC, id DESC
            LIMIT %s
            """,
            (
                str(workflow_id or "").strip(),
                str(entry_key or "").strip(),
                max(1, min(int(limit or 20), 100)),
            ),
        )
        return [_row_to_dict(cur, row) for row in (cur.fetchall() or [])]
