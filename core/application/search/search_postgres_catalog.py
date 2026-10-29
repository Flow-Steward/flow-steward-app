from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from core.application.search.search_postgres_common import (
    _execute_rows,
    _execute_total,
    _isoformat,
    _like_pattern,
    _trim,
)

_MESSAGE_CAST_W_ACCOUNT_ID_AS_TEXT_S = "CAST(w.account_id AS TEXT) = %s"
_MESSAGE_CAST_W_TENANT_ID_AS_TEXT_S = "CAST(w.tenant_id AS TEXT) = %s"

_REGISTRY_GROUPS: dict[str, tuple[str, str, str]] = {
    "agents": ("registry_agents", "agent_id", "/registry/agents"),
    "tools": ("registry_tools", "tool_id", "/registry/tools"),
    "skills": ("registry_skills", "skill_id", "/registry/skills"),
}


def _project_scope_predicate(
    column: str, project_ids: Sequence[str] | None
) -> tuple[str, list[str]]:
    if project_ids is None:
        return "", []
    normalized = [str(value).strip() for value in project_ids if str(value).strip()]
    if not normalized:
        return f" AND {column} IS NULL", []
    placeholders = ", ".join(["%s"] * len(normalized))
    return (
        f" AND ({column} IS NULL OR {column} = ANY(ARRAY[{placeholders}]::uuid[]))",
        normalized,
    )


def _catalog_query(
    *,
    group: str,
    query: str,
    account_id: str,
    tenant_id: str | None,
    workspace_id: str | None,
    project_ids: Sequence[str] | None,
) -> tuple[str, list[Any], str]:
    contains = _like_pattern(query)
    if group in _REGISTRY_GROUPS:
        table, id_column, route = _REGISTRY_GROUPS[group]
        project_sql, project_params = _project_scope_predicate("r.project_id", project_ids)
        sql = f"""
            SELECT CAST(r.{id_column} AS TEXT) AS entity_id,
                   r.display_name AS title,
                   COALESCE(r.description, '') AS description,
                   r.updated_at AS search_timestamp,
                   CAST(r.project_id AS TEXT) AS project_id
            FROM {table} r
            WHERE r.enabled = true
              AND r.deleted_at IS NULL
              AND r.visible_in_catalog = true
              AND (CAST(r.account_id AS TEXT) = %s OR
                   (r.account_id IS NULL AND r.scope_kind = 'system'))
              AND (CAST(r.{id_column} AS TEXT) ILIKE %s ESCAPE '\\'
                   OR COALESCE(r.display_name, '') ILIKE %s ESCAPE '\\'
                   OR COALESCE(r.description, '') ILIKE %s ESCAPE '\\')
              {project_sql}
        """  # nosec B608 -- table/id/project clauses come from fixed internal maps.
        return sql, [account_id, contains, contains, contains, *project_params], route

    if group == "mcp_connectors":
        return (
            """
            SELECT CAST(c.connector_id AS TEXT) AS entity_id,
                   c.display_name AS title,
                   COALESCE(c.description, '') AS description,
                   c.updated_at AS search_timestamp,
                   NULL::text AS project_id
            FROM mcp_connectors c
            WHERE c.enabled = true
              AND c.deleted_at IS NULL
              AND CAST(c.account_id AS TEXT) = %s
              AND (CAST(c.connector_id AS TEXT) ILIKE %s ESCAPE '\\'
                   OR COALESCE(c.display_name, '') ILIKE %s ESCAPE '\\'
                   OR COALESCE(c.description, '') ILIKE %s ESCAPE '\\')
            """,
            [account_id, contains, contains, contains],
            "/registry/mcp",
        )

    if group == "projects":
        conditions = ["CAST(p.account_id AS TEXT) = %s", "p.deleted_at IS NULL"]
        params: list[Any] = [account_id]
        if tenant_id:
            conditions.append("CAST(p.tenant_id AS TEXT) = %s")
            params.append(tenant_id)
        if workspace_id:
            conditions.append("CAST(p.workspace_id AS TEXT) = %s")
            params.append(workspace_id)
        project_sql, project_params = _project_scope_predicate("p.id", project_ids)
        conditions.append(
            "(CAST(p.id AS TEXT) ILIKE %s ESCAPE '\\' OR p.name ILIKE %s ESCAPE '\\' "
            "OR COALESCE(p.description, '') ILIKE %s ESCAPE '\\')"
        )
        params.extend([contains, contains, contains, *project_params])
        sql = f"""
            SELECT CAST(p.id AS TEXT) AS entity_id, p.name AS title,
                   COALESCE(p.description, '') AS description,
                   p.created_at AS search_timestamp, CAST(p.id AS TEXT) AS project_id
            FROM projects p
            WHERE {" AND ".join(conditions)} {project_sql}
            """  # nosec B608 -- conditions contain fixed clauses plus placeholders only.
        return sql, params, "/projects"

    if group == "extensions":
        return (
            """
            SELECT ei.extension_id AS entity_id,
                   COALESCE(NULLIF(er.descriptor_jsonb->>'display_name', ''), ei.extension_id) AS title,
                   COALESCE(er.descriptor_jsonb->>'description', '') AS description,
                   ei.installed_at AS search_timestamp,
                   NULL::text AS project_id
              FROM extension_installs ei
         LEFT JOIN extension_releases er ON er.release_id = ei.release_id
             WHERE ei.state = 'active'
               AND (CAST(ei.account_id AS TEXT) = %s OR ei.visibility = 'global')
               AND (ei.extension_id ILIKE %s ESCAPE '\\'
                    OR COALESCE(er.descriptor_jsonb->>'display_name', '') ILIKE %s ESCAPE '\\'
                    OR COALESCE(er.descriptor_jsonb->>'description', '') ILIKE %s ESCAPE '\\')
            """,
            [account_id, contains, contains, contains],
            "/extensions",
        )

    if group == "guardrails":
        project_sql, project_params = _project_scope_predicate("g.project_id", project_ids)
        sql = f"""
            SELECT CAST(g.id AS TEXT) AS entity_id,
                   g.display_name AS title,
                   COALESCE(g.description, '') AS description,
                   g.updated_at AS search_timestamp,
                   CAST(g.project_id AS TEXT) AS project_id
              FROM registry_guardrails g
             WHERE g.enabled = true
               AND g.deleted_at IS NULL
               AND CAST(g.account_id AS TEXT) = %s
               AND (CAST(g.id AS TEXT) ILIKE %s ESCAPE '\\'
                    OR g.display_name ILIKE %s ESCAPE '\\'
                    OR COALESCE(g.description, '') ILIKE %s ESCAPE '\\')
               {project_sql}
        """  # nosec B608 -- the project clause is built from fixed identifiers.
        return (
            sql,
            [account_id, contains, contains, contains, *project_params],
            "/registry/guardrails",
        )

    if group in {"data_contracts", "fields"}:
        if group == "data_contracts":
            table, id_column, timestamp_column, route = (
                "registry_data_contracts",
                "contract_id",
                "last_synced_at",
                "/registry/data-contracts",
            )
        else:
            table, id_column, timestamp_column, route = (
                "registry_fields",
                "field_key",
                "updated_at",
                "/registry/fields",
            )
        sql = f"""
            SELECT CAST(r.{id_column} AS TEXT) AS entity_id,
                   r.display_name AS title,
                   COALESCE(r.description, '') AS description,
                   r.{timestamp_column} AS search_timestamp,
                   NULL::text AS project_id
              FROM {table} r
             WHERE r.enabled = true
               AND r.sync_status = 'active'
               AND (CAST(r.{id_column} AS TEXT) ILIKE %s ESCAPE '\\'
                    OR r.display_name ILIKE %s ESCAPE '\\'
                    OR COALESCE(r.description, '') ILIKE %s ESCAPE '\\')
        """  # nosec B608 -- identifiers come from the fixed group map above.
        if group == "data_contracts":
            sql = sql.replace(
                "WHERE r.enabled = true", "WHERE r.enabled = true AND r.deleted_at IS NULL"
            )
        return sql, [contains, contains, contains], route

    if group == "published_blocks":
        return (
            """
            SELECT b.block_key AS entity_id,
                   b.display_name AS title,
                   COALESCE(b.description, '') AS description,
                   b.updated_at AS search_timestamp,
                   NULL::text AS project_id
              FROM registry_published_block_contracts b
             WHERE b.enabled = true
               AND b.sync_status = 'active'
               AND b.visible_in_catalog = true
               AND b.availability_status = 'available'
               AND b.new_runs_blocked = false
               AND (CAST(b.account_id AS TEXT) = %s OR
                    (b.account_id IS NULL AND b.scope_kind = 'system'))
               AND (b.block_key ILIKE %s ESCAPE '\\'
                    OR b.display_name ILIKE %s ESCAPE '\\'
                    OR COALESCE(b.description, '') ILIKE %s ESCAPE '\\')
            """,
            [account_id, contains, contains, contains],
            "/registry/published-blocks",
        )

    if group == "saved_queries":
        normalized_project_ids = (
            list(dict.fromkeys(str(value).strip() for value in project_ids if str(value).strip()))
            if project_ids is not None
            else None
        )
        project_id_projection = "NULL::text"
        project_id_params: list[str] = []
        if normalized_project_ids is not None and len(normalized_project_ids) == 1:
            # A single scoped project is an exact attribution: the visibility
            # predicate below proves this dataset is available in that project.
            # Keep multi-project searches unassigned instead of guessing.
            project_id_projection = "CAST(%s AS TEXT)"
            project_id_params.append(normalized_project_ids[0])
        conditions = [
            "q.enabled = true",
            "q.deleted_at IS NULL",
            "d.enabled = true",
            "d.deleted_at IS NULL",
            "w.deleted_at IS NULL",
            _MESSAGE_CAST_W_ACCOUNT_ID_AS_TEXT_S,
        ]
        params: list[Any] = [*project_id_params, account_id]
        if tenant_id:
            conditions.append(_MESSAGE_CAST_W_TENANT_ID_AS_TEXT_S)
            params.append(tenant_id)
        if workspace_id:
            conditions.append("CAST(d.workspace_id AS TEXT) = %s")
            params.append(workspace_id)
        if project_ids is not None:
            normalized = normalized_project_ids or []
            if normalized:
                placeholders = ", ".join(["%s"] * len(normalized))
                conditions.append(
                    "((d.availability_mode = 'all_workspace_projects' AND EXISTS ("
                    "SELECT 1 FROM projects p_scope WHERE p_scope.workspace_id = d.workspace_id "
                    f"AND p_scope.id = ANY(ARRAY[{placeholders}]::uuid[]))) OR EXISTS ("  # nosec B608 -- placeholders only.
                    "SELECT 1 FROM project_datasets pd WHERE pd.dataset_id = d.dataset_id "
                    f"AND pd.project_id = ANY(ARRAY[{placeholders}]::uuid[])))"  # nosec B608 -- placeholders only.
                )
                params.extend([*normalized, *normalized])
            else:
                conditions.append("1 = 0")
        conditions.append(
            "(q.query_template_id ILIKE %s ESCAPE '\\' "
            "OR q.display_name ILIKE %s ESCAPE '\\' "
            "OR COALESCE(q.description, '') ILIKE %s ESCAPE '\\')"
        )
        params.extend([contains, contains, contains])
        return (
            f"""
            SELECT q.query_template_id AS entity_id,
                   CAST(d.dataset_id AS TEXT) AS dataset_id,
                   q.display_name AS title,
                   COALESCE(q.description, '') AS description,
                   q.updated_at AS search_timestamp,
                   {project_id_projection} AS project_id
              FROM dataset_query_templates q
              JOIN registry_datasets d ON d.dataset_id = q.dataset_id
              JOIN workspaces w ON w.id = d.workspace_id
             WHERE {" AND ".join(conditions)}
            """,  # nosec B608 -- fixed clauses plus bound placeholders only.
            params,
            "/registry/datasets",
        )

    if group == "model_providers":
        return (
            """
            SELECT CAST(p.id AS TEXT) AS entity_id,
                   p.name AS title,
                   CONCAT_WS(' ', p.provider_type, p.default_model) AS description,
                   p.updated_at AS search_timestamp,
                   NULL::text AS project_id
              FROM model_providers p
             WHERE CAST(p.account_id AS TEXT) = %s
               AND p.enabled = true
               AND p.lifecycle = 'active'
               AND (CAST(p.id AS TEXT) ILIKE %s ESCAPE '\\'
                    OR p.name ILIKE %s ESCAPE '\\'
                    OR COALESCE(p.provider_type, '') ILIKE %s ESCAPE '\\'
                    OR COALESCE(p.default_model, '') ILIKE %s ESCAPE '\\')
            """,
            [account_id, contains, contains, contains, contains],
            "/settings/providers",
        )

    if group == "model_routes":
        return (
            """
            SELECT route.entity_id, route.title, route.description,
                   route.search_timestamp, NULL::text AS project_id
              FROM (
                    SELECT CONCAT('custom:', p.id::text, '::', p.default_model) AS entity_id,
                           CONCAT(p.name, ' / ', p.default_model) AS title,
                           p.provider_type AS description,
                           p.updated_at AS search_timestamp
                      FROM model_providers p
                     WHERE CAST(p.account_id AS TEXT) = %s
                       AND p.enabled = true
                       AND p.lifecycle = 'active'
                       AND p.verification_status = 'passed'
                       AND p.verified_at >= now() - interval '30 days'
                       AND p.verified_at <= now()
                       AND COALESCE(p.default_model, '') <> ''
                       AND p.verified_capabilities_jsonb @> '["chat","tools","structured_output"]'::jsonb
                    UNION ALL
                    SELECT CONCAT(s.provider_key, '::', m.model_id) AS entity_id,
                           CONCAT(COALESCE(NULLIF(s.display_label, ''), s.provider_key), ' / ',
                                  COALESCE(NULLIF(m.display_label, ''), m.model_id)) AS title,
                           s.extension_id AS description,
                           m.updated_at AS search_timestamp
                      FROM llm_provider_models m
                      JOIN llm_provider_sources s
                        ON s.id = m.provider_source_id AND s.account_id = m.account_id
                      JOIN extension_installs ei
                        ON ei.install_id = s.extension_install_id AND ei.account_id = s.account_id
                     WHERE CAST(m.account_id AS TEXT) = %s
                       AND m.enabled = true
                       AND m.supports_structured_output = true
                       AND m.verification_status = 'passed'
                       AND m.lifecycle = 'active'
                       AND m.verified_at >= now() - interval '30 days'
                       AND m.verified_at <= now()
                       AND s.enabled = true
                       AND ei.state = 'active'
                       AND ei.health_status <> 'failed'
                       AND ei.handshake_status <> 'failed'
                       AND ei.runtime_status <> 'incompatible'
              ) route
             WHERE (route.entity_id ILIKE %s ESCAPE '\\'
                    OR route.title ILIKE %s ESCAPE '\\'
                    OR COALESCE(route.description, '') ILIKE %s ESCAPE '\\')
            """,
            [account_id, account_id, contains, contains, contains],
            "/settings/providers",
        )

    if group == "users":
        return (
            """
            SELECT CAST(u.id AS TEXT) AS entity_id,
                   COALESCE(NULLIF(u.display_name, ''), u.username) AS title,
                   u.username AS description,
                   u.updated_at AS search_timestamp,
                   NULL::text AS project_id
              FROM users u
             WHERE CAST(u.account_id AS TEXT) = %s
               AND u.is_active = true
               AND u.deleted_at IS NULL
               AND (CAST(u.id AS TEXT) ILIKE %s ESCAPE '\\'
                    OR u.username ILIKE %s ESCAPE '\\'
                    OR COALESCE(u.display_name, '') ILIKE %s ESCAPE '\\')
            """,
            [account_id, contains, contains, contains],
            "/settings/users",
        )

    if group == "workspaces":
        conditions = [
            _MESSAGE_CAST_W_ACCOUNT_ID_AS_TEXT_S,
            "w.status = 'active'",
            "w.deleted_at IS NULL",
        ]
        params = [account_id]
        if tenant_id:
            conditions.append(_MESSAGE_CAST_W_TENANT_ID_AS_TEXT_S)
            params.append(tenant_id)
        if workspace_id:
            conditions.append("CAST(w.id AS TEXT) = %s")
            params.append(workspace_id)
        conditions.append(
            "(CAST(w.id AS TEXT) ILIKE %s ESCAPE '\\' "
            "OR w.workspace_key ILIKE %s ESCAPE '\\' "
            "OR w.display_name ILIKE %s ESCAPE '\\' "
            "OR COALESCE(w.description, '') ILIKE %s ESCAPE '\\')"
        )
        params.extend([contains, contains, contains, contains])
        return (
            f"""
            SELECT CAST(w.id AS TEXT) AS entity_id,
                   w.display_name AS title,
                   COALESCE(w.description, w.workspace_key) AS description,
                   w.updated_at AS search_timestamp,
                   NULL::text AS project_id
              FROM workspaces w
             WHERE {" AND ".join(conditions)}
            """,  # nosec B608 -- fixed clauses plus bound placeholders only.
            params,
            "/registry/workspaces",
        )

    table = "registry_datasets" if group == "datasets" else "workspace_data_connections"
    alias = "d" if group == "datasets" else "c"
    id_column = "dataset_id" if group == "datasets" else "connection_id"
    conditions = [
        f"{alias}.deleted_at IS NULL",
        f"{alias}.enabled = true",
        _MESSAGE_CAST_W_ACCOUNT_ID_AS_TEXT_S,
    ]
    params = [account_id]
    if tenant_id:
        conditions.append(_MESSAGE_CAST_W_TENANT_ID_AS_TEXT_S)
        params.append(tenant_id)
    if workspace_id:
        conditions.append(f"CAST({alias}.workspace_id AS TEXT) = %s")
        params.append(workspace_id)
    if project_ids is not None:
        normalized = [str(value).strip() for value in project_ids if str(value).strip()]
        if normalized:
            placeholders = ", ".join(["%s"] * len(normalized))
            if group == "datasets":
                conditions.append(
                    "((d.availability_mode = 'all_workspace_projects' AND EXISTS ("
                    "SELECT 1 FROM projects p_scope WHERE p_scope.workspace_id = d.workspace_id "
                    f"AND p_scope.id = ANY(ARRAY[{placeholders}]::uuid[]))) OR EXISTS ("  # nosec B608 -- placeholders only.
                    "SELECT 1 FROM project_datasets pd WHERE pd.dataset_id = d.dataset_id "
                    f"AND pd.project_id = ANY(ARRAY[{placeholders}]::uuid[])))"  # nosec B608 -- placeholders only.
                )
                params.extend([*normalized, *normalized])
            else:
                conditions.append(
                    f"{alias}.workspace_id IN (SELECT p.workspace_id FROM projects p "  # nosec B608 -- alias and placeholders are fixed internally.
                    f"WHERE p.id = ANY(ARRAY[{placeholders}]::uuid[]))"
                )
                params.extend(normalized)
        else:
            conditions.append("1 = 0")
    conditions.append(
        f"(CAST({alias}.{id_column} AS TEXT) ILIKE %s ESCAPE '\\' "
        f"OR COALESCE({alias}.display_name, '') ILIKE %s ESCAPE '\\' "
        f"OR COALESCE({alias}.description, '') ILIKE %s ESCAPE '\\')"
    )
    params.extend([contains, contains, contains])
    route = "/registry/datasets" if group == "datasets" else "/registry/data-connections"
    sql = f"""
        SELECT CAST({alias}.{id_column} AS TEXT) AS entity_id,
               {alias}.display_name AS title,
               COALESCE({alias}.description, '') AS description,
               {alias}.updated_at AS search_timestamp,
               NULL::text AS project_id
        FROM {table} {alias}
        JOIN workspaces w ON w.id = {alias}.workspace_id AND w.deleted_at IS NULL
        WHERE {" AND ".join(conditions)}
        """  # nosec B608 -- table, alias and id column are fixed internal selections.
    return sql, params, route


def _catalog_rows(
    *,
    bl: Any,
    group: str,
    query: str,
    account_id: str,
    tenant_id: str | None,
    workspace_id: str | None,
    project_ids: Sequence[str] | None,
    limit: int,
    include_totals: bool = False,
) -> tuple[list[dict[str, Any]], int | None]:
    base_sql, params, route = _catalog_query(
        group=group,
        query=query,
        account_id=account_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        project_ids=project_ids,
    )
    total = (
        _execute_total(
            conn=bl.conn,
            sql=f"SELECT COUNT(*) AS total FROM ({base_sql}) matches",  # nosec B608 -- base_sql is built only from fixed identifiers and bound placeholders.
            params=params,
        )
        if include_totals
        else None
    )
    rows = _execute_rows(
        conn=bl.conn,
        sql=f"{base_sql} ORDER BY title, entity_id LIMIT %s",  # nosec B608 -- base_sql is fixed-identifier SQL with bound values.
        params=[*params, max(1, int(limit))],
    )
    items = []
    for row in rows:
        entity_id = str(row.get("entity_id") or "").strip()
        if not entity_id:
            continue
        deep_link = route
        if group not in {
            "projects",
            "data_connections",
            "saved_queries",
            "model_providers",
            "model_routes",
            "users",
        }:
            deep_link = f"{route}/{quote(entity_id, safe='')}"
        elif group == "projects":
            deep_link = f"/projects/{quote(entity_id, safe='')}"
        elif group == "saved_queries":
            dataset_id = str(row.get("dataset_id") or "").strip()
            if dataset_id:
                deep_link = f"{route}/{quote(dataset_id, safe='')}?tab=saved-queries"
        items.append(
            {
                "group": group,
                "source": f"postgres.{group}",
                "id": entity_id,
                "title": _trim(row.get("title") or entity_id, limit=160),
                "snippet": _trim(row.get("description") or entity_id, limit=240),
                "score": 0.9,
                "timestamp": _isoformat(row.get("search_timestamp")),
                "project_id": str(row.get("project_id") or "").strip() or None,
                "deep_link": deep_link,
                "meta": {"entity_type": group, "entity_id": entity_id},
            }
        )
    return items, total


__all__ = ["_catalog_rows"]
