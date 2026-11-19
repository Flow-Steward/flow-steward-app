"""Reusable helpers for project settings snapshots and revision history."""

from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime


def get_project_settings_snapshot(
    project_id: str,
    *,
    conn,
    table_exists,
    get_project,
    get_project_workflows,
    get_project_policy,
    get_project_provider_bindings,
    list_project_data_connections,
    as_dict,
    json_clone,
) -> dict[str, Any]:
    """Return a normalized project settings snapshot for history/diff/restore."""
    project_row = get_project(project_id)
    if not isinstance(project_row, dict):
        return {}

    project_workflows = get_project_workflows(project_id, enabled_only=False)
    operator_default_workflow_id = next(
        (
            str(item.get("workflow_id") or "").strip()
            for item in (project_workflows or [])
            if bool(item.get("is_default"))
        ),
        "",
    )
    project_policy = get_project_policy(project_id, project_row=project_row)
    project_providers = get_project_provider_bindings(
        project_id,
        enabled_only=False,
        project_row=project_row,
    )
    project_data_connections = (
        list_project_data_connections(project_id) if callable(list_project_data_connections) else []
    )
    project_secret_keys: list[str] = []
    if table_exists("project_secrets"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT secret_key
                FROM project_secrets
                WHERE project_id = %s
                  AND active = true
                ORDER BY secret_key
                """,
                (str(project_id),),
            )
            project_secret_keys = [
                str((row[0] if row else "") or "").strip() for row in cur.fetchall()
            ]
            project_secret_keys = [key for key in project_secret_keys if key]

    snapshot = {
        "schema_version": 1,
        "project": {
            "id": str(project_row.get("id") or "").strip(),
            "name": str(project_row.get("name") or "").strip(),
            "description": str(project_row.get("description") or "").strip(),
            "data_connection_ids": [
                str(item.get("connection_id") or "").strip()
                for item in (project_data_connections or [])
                if isinstance(item, dict) and str(item.get("connection_id") or "").strip()
            ],
            "agents_config": as_dict(project_row.get("agents_config")),
            "project_rules": str(project_row.get("project_rules") or "").strip(),
        },
        "operator_default_workflow_id": operator_default_workflow_id,
        "project_workflows": [
            {
                "workflow_id": str(item.get("workflow_id") or "").strip(),
                "is_default": bool(item.get("is_default")),
                "enabled": bool(item.get("enabled", True)),
                "priority": int(item.get("priority") or 0),
            }
            for item in (project_workflows or [])
            if str(item.get("workflow_id") or "").strip()
        ],
        "project_policy": as_dict(project_policy),
        "project_providers": [
            {
                "provider_key": str(item.get("provider_key") or "").strip(),
                "enabled": bool(item.get("enabled", True)),
                "binding_config": as_dict(item.get("binding_config")),
            }
            for item in (project_providers or [])
            if str(item.get("provider_key") or "").strip()
        ],
        "project_secret_keys": project_secret_keys,
    }
    return json_clone(snapshot)


def create_project_settings_revision(
    project_id: str,
    *,
    conn,
    table_exists,
    as_dict,
    get_project_settings_snapshot,
    author: str = "system",
    action: str = "save",
    source: str = "ui",
    snapshot: dict[str, Any] | None = None,
) -> str | None:
    if not table_exists("project_settings_revisions"):
        return None
    snapshot_payload = (
        as_dict(snapshot)
        if isinstance(snapshot, dict)
        else get_project_settings_snapshot(project_id)
    )
    if not snapshot_payload:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO project_settings_revisions (
                project_id,
                author,
                action,
                source,
                snapshot_jsonb
            ) VALUES (%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                str(project_id),
                str(author or "system").strip() or "system",
                str(action or "save").strip() or "save",
                str(source or "ui").strip() or "ui",
                json.dumps(snapshot_payload),
            ),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def list_project_settings_revisions(
    project_id: str,
    *,
    conn,
    table_exists,
    column_exists,
    sql_project_id_eq: str,
    sql_deleted_at_is_null: str,
    sql_and: str,
    limit: int = 50,
    include_trashed: bool = False,
) -> list[dict[str, Any]]:
    if not table_exists("project_settings_revisions"):
        return []
    clauses = [sql_project_id_eq]
    if not include_trashed and column_exists("project_settings_revisions", "deleted_at"):
        clauses.append(sql_deleted_at_is_null)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                id,
                project_id,
                author,
                action,
                source,
                created_at
            FROM project_settings_revisions
            WHERE """
            + sql_and.join(clauses)
            + """
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (str(project_id), max(1, min(int(limit or 50), 500))),
        )
        return cur.fetchall()


def get_project_settings_revision(
    project_id: str,
    revision_id: str,
    *,
    conn,
    table_exists,
    column_exists,
    as_dict,
    sql_project_id_eq: str,
    sql_id_eq: str,
    sql_deleted_at_is_null: str,
    sql_and: str,
    include_trashed: bool = False,
) -> dict[str, Any] | None:
    if not table_exists("project_settings_revisions"):
        return None
    clauses = [sql_project_id_eq, sql_id_eq]
    if not include_trashed and column_exists("project_settings_revisions", "deleted_at"):
        clauses.append(sql_deleted_at_is_null)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        try:
            cur.execute(
                """
                SELECT
                    id,
                    project_id,
                    author,
                    action,
                    source,
                    snapshot_jsonb,
                    created_at
                FROM project_settings_revisions
                WHERE """
                + sql_and.join(clauses)
                + """
                LIMIT 1
                """,
                (str(project_id), str(revision_id)),
            )
        except Exception as exc:
            if "invalid input syntax for type uuid" in str(exc).lower():
                return None
            raise
        row = cur.fetchone()
        if not isinstance(row, dict):
            return None
        payload = dict(row)
        payload["snapshot_jsonb"] = as_dict(payload.get("snapshot_jsonb"))
        return payload


def restore_project_settings_revision(
    project_id: str,
    revision_id: str,
    *,
    as_dict,
    as_list,
    normalize_string_list,
    get_project_settings_revision,
    get_project,
    update_project,
    set_project_data_connections,
    get_project_provider_bindings,
    save_project_provider_binding,
    create_project_settings_revision,
    author: str = "system",
) -> dict[str, Any]:
    revision = get_project_settings_revision(project_id, revision_id)
    if not isinstance(revision, dict):
        raise ValueError(f"Project revision '{revision_id}' was not found")
    snapshot = as_dict(revision.get("snapshot_jsonb"))
    project_payload = as_dict(snapshot.get("project"))
    if not project_payload:
        raise ValueError(f"Project revision '{revision_id}' has an invalid snapshot payload")
    current_project = get_project(project_id) or {}
    workspace_id = (
        str(
            project_payload.get("workspace_id") or current_project.get("workspace_id") or ""
        ).strip()
        or None
    )

    workflow_rows = [
        row
        for row in (as_list(snapshot.get("project_workflows")) or [])
        if isinstance(row, dict) and str(row.get("workflow_id") or "").strip()
    ]
    enabled_workflow_ids = [
        str(row.get("workflow_id") or "").strip()
        for row in workflow_rows
        if bool(row.get("enabled", True))
    ]
    operator_default_workflow_id = str(snapshot.get("operator_default_workflow_id") or "").strip()

    update_project(
        str(project_id),
        name=str(project_payload.get("name") or "").strip(),
        description=str(project_payload.get("description") or "").strip() or None,
        agents_config=as_dict(project_payload.get("agents_config")),
        project_rules=str(project_payload.get("project_rules") or "").strip() or None,
        operator_default_workflow_id=operator_default_workflow_id or None,
        project_workflow_ids=enabled_workflow_ids,
        project_policy=as_dict(snapshot.get("project_policy")),
        workspace_id=workspace_id,
    )

    if callable(set_project_data_connections):
        snapshot_connection_ids = normalize_string_list(project_payload.get("data_connection_ids"))
        set_project_data_connections(str(project_id), snapshot_connection_ids)

    snapshot_providers = [
        row
        for row in as_list(snapshot.get("project_providers"))
        if isinstance(row, dict) and str(row.get("provider_key") or "").strip()
    ]
    existing_providers = get_project_provider_bindings(project_id, enabled_only=False)
    existing_provider_keys = {
        str(row.get("provider_key") or "").strip()
        for row in (existing_providers or [])
        if str(row.get("provider_key") or "").strip()
    }
    snapshot_provider_keys = {
        str(row.get("provider_key") or "").strip()
        for row in snapshot_providers
        if str(row.get("provider_key") or "").strip()
    }

    for provider_key in sorted(existing_provider_keys - snapshot_provider_keys):
        save_project_provider_binding(
            str(project_id),
            provider_key,
            config={},
            enabled=False,
        )
    for row in snapshot_providers:
        provider_key = str(row.get("provider_key") or "").strip()
        if not provider_key:
            continue
        save_project_provider_binding(
            str(project_id),
            provider_key,
            config=as_dict(row.get("binding_config")),
            enabled=bool(row.get("enabled", True)),
        )

    new_revision_id = create_project_settings_revision(
        str(project_id),
        author=author,
        action="restore",
        source=f"revision:{str(revision_id).strip()}",
    )
    return {
        "status": "restored",
        "project_id": str(project_id),
        "restored_from_revision_id": str(revision_id).strip(),
        "new_revision_id": str(new_revision_id).strip() if new_revision_id else None,
    }
