"""Reusable helpers for project registration persistence."""

from __future__ import annotations

import json

from core.infrastructure.db.psycopg_runtime import sql

PROJECT_ALREADY_EXISTS = "Project already exists in this workspace."


def add_project(
    *,
    conn,
    table_exists,
    column_exists,
    sync_project_workflows_cur,
    save_project_policy_cur,
    default_account_id,
    default_tenant_id,
    name,
    description=None,
    agents_config=None,
    project_rules=None,
    operator_default_workflow_id=None,
    project_workflow_ids=None,
    project_policy=None,
    config=None,
    extra=None,
    account_id=None,
    tenant_id=None,
    workspace_id=None,
    create_only=False,
):
    resolved_account_id = str(account_id or "").strip() or default_account_id()
    normalized_description = str(description or "").strip()
    resolved_extra = dict(extra) if isinstance(extra, dict) else {}
    has_tenant_id = bool(column_exists("projects", "tenant_id"))
    has_workspace_id = bool(column_exists("projects", "workspace_id"))
    insert_columns = [
        "name",
        "description",
        "agents_config",
        "project_rules",
        "config",
        "extra",
        "account_id",
    ]
    insert_values = [
        name,
        normalized_description,
        json.dumps(agents_config or {}),
        project_rules or None,
        json.dumps(config) if config else None,
        json.dumps(resolved_extra) if resolved_extra else None,
        resolved_account_id,
    ]
    update_columns = ["description"]
    if has_tenant_id:
        insert_columns.append("tenant_id")
        resolved_tenant_id = str(tenant_id or "").strip() or default_tenant_id()
        insert_values.append(resolved_tenant_id)
        update_columns.append("tenant_id")
    if has_workspace_id:
        resolved_workspace_id = str(workspace_id or "").strip()
        if not resolved_workspace_id:
            raise ValueError("workspace_id is required")
        insert_columns.append("workspace_id")
        insert_values.append(resolved_workspace_id)
        update_columns.append("workspace_id")
    update_columns.extend(["agents_config", "project_rules"])
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in insert_columns)
    if create_only:
        conflict_clause = sql.SQL(
            "ON CONFLICT(tenant_id, account_id, workspace_id, name) DO NOTHING"
        )
    else:
        updates = sql.SQL(",\n                ").join(
            sql.SQL("{} = EXCLUDED.{}").format(
                sql.Identifier(column),
                sql.Identifier(column),
            )
            for column in update_columns
        )
        conflict_clause = sql.SQL(
            "ON CONFLICT(tenant_id, account_id, workspace_id, name) DO UPDATE SET\n"
            "                {updates}"
        ).format(updates=updates)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
            INSERT INTO projects ({insert_columns})
            VALUES ({placeholders})
            {conflict_clause}
            RETURNING id
            """
            ).format(
                insert_columns=sql.SQL(", ").join(
                    sql.Identifier(column) for column in insert_columns
                ),
                placeholders=placeholders,
                conflict_clause=conflict_clause,
            ),
            tuple(insert_values),
        )
        inserted = cur.fetchone()
        if not inserted:
            raise ValueError(PROJECT_ALREADY_EXISTS)
        project_id = inserted[0]
        if table_exists("project_workflows") and project_workflow_ids is not None:
            sync_project_workflows_cur(
                cur,
                project_id=project_id,
                project_workflow_ids=list(project_workflow_ids or []),
                operator_default_workflow_id=str(operator_default_workflow_id or "").strip(),
            )
        if table_exists("project_policies") and project_policy is not None:
            save_project_policy_cur(cur, project_id=project_id, policy=project_policy)
    return project_id
