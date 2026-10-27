from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.application.search.search_postgres_common import (
    VALUE_AND_SQL,
    _column_exists,
    _execute_rows,
    _execute_total,
    _isoformat,
    _like_pattern,
    _project_scope_sql,
    _trim,
)


def _workflow_rows(
    *,
    bl: Any,
    query: str,
    account_id: str,
    project_ids: Sequence[str] | None,
    limit: int,
    include_totals: bool = False,
) -> tuple[list[dict[str, Any]], int | None]:
    contains = _like_pattern(query)
    query_text = str(query or "").strip()
    workflow_conditions: list[str] = ["w.enabled = true"]
    workflow_params: list[Any] = []
    extension_params: list[Any] = []
    if _column_exists(bl, "registry_workflows", "deleted_at"):
        workflow_conditions.append("w.deleted_at IS NULL")
    if _column_exists(bl, "registry_workflows", "visible_in_catalog"):
        workflow_conditions.append("w.visible_in_catalog = true")
    if _column_exists(bl, "registry_workflows", "account_id"):
        workflow_conditions.append(
            "(CAST(w.account_id AS TEXT) = %s OR "
            "(w.account_id IS NULL AND w.scope_kind = 'system'))"
        )
        workflow_params.append(account_id)

    link_conditions = ["pw.workflow_id IS NOT NULL"]
    if _column_exists(bl, "project_workflows", "enabled"):
        link_conditions.append("pw.enabled = true")
    if _column_exists(bl, "project_workflows", "deleted_at"):
        link_conditions.append("pw.deleted_at IS NULL")
    project_link_sql, project_link_params = _project_scope_sql("pw.project_id", project_ids)

    extension_projection_sql = ""
    if _column_exists(bl, "extension_registry_projections", "projection_jsonb"):
        extension_projection_sql = """
            UNION ALL
            SELECT
                CONCAT(
                    'extension.',
                    REGEXP_REPLACE(TRIM(ep.install_id), '[^A-Za-z0-9_.-]+', '-', 'g'),
                    '.',
                    REGEXP_REPLACE(TRIM(ep.artifact_id), '[^A-Za-z0-9_.-]+', '-', 'g')
                ) AS workflow_id,
                COALESCE(NULLIF(BTRIM(ep.projection_jsonb->>'display_name'), ''), ep.artifact_id)
                    AS display_name,
                COALESCE(ep.projection_jsonb->>'description', '') AS description,
                ep.updated_at,
                NULL::text AS project_id,
                true AS globally_visible
            FROM extension_registry_projections ep
            WHERE ep.artifact_kind = 'workflow'
              AND ep.is_active = true
              AND ep.visible_in_catalog = true
              AND CAST(ep.account_id AS TEXT) = %s
              AND COALESCE((ep.projection_jsonb->>'enabled')::boolean, true) = true
        """
        extension_params.append(account_id)

    scope_filter_sql = ""
    if project_ids is not None:
        scope_filter_sql = "WHERE (w.project_id IS NOT NULL OR w.globally_visible = true)"

    # Bandit false positive: SQL is assembled from fixed internal predicates and DB parameter placeholders only.
    base_cte_sql = f"""
        WITH workflow_links AS (
            SELECT
                pw.workflow_id,
                MIN(CAST(pw.project_id AS TEXT)) AS project_id
            FROM project_workflows pw
            WHERE {VALUE_AND_SQL.join(link_conditions)}
            {project_link_sql}
            GROUP BY pw.workflow_id
        ),
        base_workflows AS (
            SELECT
                w.workflow_id,
                w.display_name,
                COALESCE(w.description, '') AS description,
                w.updated_at,
                wl.project_id,
                true AS globally_visible
            FROM registry_workflows w
            LEFT JOIN workflow_links wl ON wl.workflow_id = w.workflow_id
            WHERE {VALUE_AND_SQL.join(workflow_conditions)}
            {extension_projection_sql}
        ),
        scoped_workflows AS (
            SELECT * FROM base_workflows w
            {scope_filter_sql}
        ),
        workflow_matches AS (
            SELECT
                CONCAT(w.workflow_id, ':workflow') AS id,
                w.workflow_id,
                w.display_name AS workflow_display_name,
                w.description AS workflow_description,
                w.project_id,
                NULL::text AS group_id,
                NULL::text AS group_display_name,
                NULL::text AS step_id,
                NULL::text AS step_display_name,
                NULL::int AS step_ordinal,
                'workflow' AS match_scope,
                COALESCE(NULLIF(BTRIM(w.display_name), ''), w.workflow_id) AS matched_label,
                COALESCE(NULLIF(BTRIM(w.description), ''), w.workflow_id) AS matched_text,
                w.updated_at AS search_timestamp,
                CASE
                    WHEN LOWER(w.workflow_id) = LOWER(%s) THEN 0.99
                    WHEN LOWER(COALESCE(w.display_name, '')) = LOWER(%s) THEN 0.97
                    WHEN COALESCE(w.workflow_id, '') ILIKE %s ESCAPE '\\' THEN 0.93
                    WHEN COALESCE(w.display_name, '') ILIKE %s ESCAPE '\\' THEN 0.9
                    WHEN COALESCE(w.description, '') ILIKE %s ESCAPE '\\' THEN 0.76
                    ELSE 0.5
                END AS relevance
            FROM scoped_workflows w
            WHERE (
                COALESCE(w.workflow_id, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(w.display_name, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(w.description, '') ILIKE %s ESCAPE '\\'
            )

            UNION ALL

            SELECT
                CONCAT(w.workflow_id, ':group:', g.group_id) AS id,
                w.workflow_id,
                w.display_name AS workflow_display_name,
                w.description AS workflow_description,
                w.project_id,
                g.group_id,
                COALESCE(NULLIF(BTRIM(g.display_name), ''), g.group_id) AS group_display_name,
                NULL::text AS step_id,
                NULL::text AS step_display_name,
                NULL::int AS step_ordinal,
                'group' AS match_scope,
                COALESCE(NULLIF(BTRIM(g.display_name), ''), g.group_id) AS matched_label,
                COALESCE(NULLIF(BTRIM(g.description), ''), g.group_id) AS matched_text,
                w.updated_at AS search_timestamp,
                CASE
                    WHEN LOWER(g.group_id) = LOWER(%s) THEN 0.95
                    WHEN LOWER(COALESCE(g.display_name, '')) = LOWER(%s) THEN 0.93
                    WHEN COALESCE(g.group_id, '') ILIKE %s ESCAPE '\\' THEN 0.89
                    WHEN COALESCE(g.display_name, '') ILIKE %s ESCAPE '\\' THEN 0.86
                    WHEN COALESCE(g.description, '') ILIKE %s ESCAPE '\\' THEN 0.74
                    ELSE 0.48
                END AS relevance
            FROM scoped_workflows w
            JOIN registry_workflow_groups g ON g.workflow_id = w.workflow_id
            WHERE (
                COALESCE(g.group_id, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(g.display_name, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(g.description, '') ILIKE %s ESCAPE '\\'
            )

            UNION ALL

            SELECT
                CONCAT(w.workflow_id, ':step:', s.step_id) AS id,
                w.workflow_id,
                w.display_name AS workflow_display_name,
                w.description AS workflow_description,
                w.project_id,
                g.group_id,
                COALESCE(NULLIF(BTRIM(g.display_name), ''), g.group_id) AS group_display_name,
                NULLIF(BTRIM(s.step_id), '') AS step_id,
                COALESCE(
                    NULLIF(BTRIM(s.display_name), ''),
                    NULLIF(BTRIM(s.step_id), ''),
                    CONCAT('Step ', s.sort_order + 1)
                ) AS step_display_name,
                s.sort_order + 1 AS step_ordinal,
                'step' AS match_scope,
                COALESCE(
                    NULLIF(BTRIM(s.display_name), ''),
                    NULLIF(BTRIM(s.step_id), ''),
                    CONCAT('Step ', s.sort_order + 1)
                ) AS matched_label,
                COALESCE(
                    NULLIF(BTRIM(s.display_name), ''),
                    NULLIF(BTRIM(s.step_id), ''),
                    NULLIF(BTRIM(s.block_key), ''),
                    NULLIF(BTRIM(s.agent_id), ''),
                    NULLIF(BTRIM(s.connector_id), ''),
                    NULLIF(BTRIM(s.workflow_id), ''),
                    NULLIF(BTRIM(s.override::text), ''),
                    CONCAT('Step ', s.sort_order + 1)
                ) AS matched_text,
                w.updated_at AS search_timestamp,
                CASE
                    WHEN LOWER(COALESCE(s.step_id, '')) = LOWER(%s) THEN 0.94
                    WHEN LOWER(COALESCE(s.display_name, '')) = LOWER(%s) THEN 0.92
                    WHEN COALESCE(s.step_id, '') ILIKE %s ESCAPE '\\' THEN 0.88
                    WHEN COALESCE(s.display_name, '') ILIKE %s ESCAPE '\\' THEN 0.85
                    WHEN COALESCE(s.block_key, '') ILIKE %s ESCAPE '\\' THEN 0.8
                    WHEN COALESCE(s.agent_id, '') ILIKE %s ESCAPE '\\' THEN 0.74
                    WHEN COALESCE(s.connector_id, '') ILIKE %s ESCAPE '\\' THEN 0.72
                    WHEN COALESCE(s.workflow_id, '') ILIKE %s ESCAPE '\\' THEN 0.7
                    WHEN COALESCE(s.override::text, '') ILIKE %s ESCAPE '\\' THEN 0.64
                    ELSE 0.44
                END AS relevance
            FROM scoped_workflows w
            -- A step belongs to its workflow directly; the group join only supplies the
            -- business group id and stays empty for root steps.
            JOIN registry_workflow_steps s ON s.owner_workflow_id = w.workflow_id
            LEFT JOIN registry_workflow_groups g ON g.id = s.group_id
            WHERE (
                COALESCE(s.step_id, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(s.display_name, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(s.block_key, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(s.agent_id, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(s.connector_id, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(s.workflow_id, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(s.override::text, '') ILIKE %s ESCAPE '\\'
            )
        )
    """

    # Bandit false positive: wraps the fixed internal CTE above without interpolating user values directly.
    count_sql = f"""
        {base_cte_sql}
        SELECT COUNT(DISTINCT workflow_id) AS total
        FROM workflow_matches
    """
    # Bandit false positive: wraps the fixed internal CTE above without interpolating user values directly.
    rows_sql = f"""
        {base_cte_sql}
        SELECT * FROM (
            SELECT DISTINCT ON (workflow_id) *
            FROM workflow_matches
            ORDER BY workflow_id, relevance DESC, search_timestamp DESC NULLS LAST, id ASC
        ) deduplicated_workflows
        ORDER BY relevance DESC, search_timestamp DESC NULLS LAST, id ASC
        LIMIT %s
    """
    match_params = [
        query_text,
        query_text,
        f"{query_text}%",
        contains,
        contains,
        contains,
        contains,
        contains,
        query_text,
        query_text,
        f"{query_text}%",
        contains,
        contains,
        contains,
        contains,
        contains,
        query_text,
        query_text,
        f"{query_text}%",
        contains,
        contains,
        contains,
        contains,
        contains,
        contains,
        contains,
        contains,
        contains,
        contains,
        contains,
        contains,
        contains,
    ]
    sql_params = [
        *project_link_params,
        *workflow_params,
        *extension_params,
        *match_params,
    ]
    total_count = (
        _execute_total(conn=bl.conn, sql=count_sql, params=sql_params) if include_totals else None
    )
    rows = _execute_rows(
        conn=bl.conn,
        sql=rows_sql,
        params=[*sql_params, max(1, int(limit))],
    )

    items: list[dict[str, Any]] = []
    seen_workflow_ids: set[str] = set()
    for row in rows:
        workflow_id = str(row.get("workflow_id") or "").strip()
        if not workflow_id or workflow_id in seen_workflow_ids:
            continue
        seen_workflow_ids.add(workflow_id)
        group_id = str(row.get("group_id") or "").strip()
        step_id = str(row.get("step_id") or "").strip()
        match_scope = str(row.get("match_scope") or "").strip() or "workflow"
        step_ordinal = row.get("step_ordinal")
        try:
            normalized_step_ordinal = int(step_ordinal) if step_ordinal is not None else None
        except Exception:
            normalized_step_ordinal = None
        # step_id is unique within its workflow, so a step link needs no group.
        if match_scope == "step" and step_id:
            deep_link = f"/registry/workflows/{workflow_id}/builder?drawer=step&step_id={step_id}"
        elif match_scope == "group" and group_id:
            deep_link = (
                f"/registry/workflows/{workflow_id}/builder?drawer=group&group_id={group_id}"
            )
        else:
            deep_link = (
                f"/registry/workflows/{workflow_id}/builder"
                if workflow_id
                else "/registry/workflows"
            )

        matched_text = _trim(row.get("matched_text"), limit=180)
        group_display = _trim(row.get("group_display_name"), limit=80)
        step_display = _trim(row.get("step_display_name") or step_id, limit=80)
        if match_scope == "step":
            snippet = _trim(
                " · ".join(
                    part
                    for part in (
                        f"Step: {step_display}" if step_display else "",
                        f"Group: {group_display or group_id}"
                        if (group_display or group_id)
                        else "",
                        matched_text,
                    )
                    if part
                ),
                limit=240,
            )
        elif match_scope == "group":
            snippet = _trim(
                " · ".join(
                    part
                    for part in (
                        f"Group: {group_display or group_id}"
                        if (group_display or group_id)
                        else "",
                        matched_text,
                    )
                    if part
                ),
                limit=240,
            )
        else:
            snippet = _trim(
                matched_text or row.get("workflow_description") or workflow_id,
                limit=240,
            )

        items.append(
            {
                "group": "workflows",
                "source": "postgres.workflows",
                "id": str(row.get("id") or workflow_id),
                "title": _trim(
                    row.get("workflow_display_name") or workflow_id or "Workflow",
                    limit=160,
                ),
                "snippet": snippet,
                "score": float(row.get("relevance") or 0.0),
                "timestamp": _isoformat(row.get("search_timestamp")),
                "project_id": str(row.get("project_id") or "").strip() or None,
                "workflow_id": workflow_id or None,
                "deep_link": deep_link,
                "meta": {
                    "match_scope": match_scope,
                    "group_id": group_id or None,
                    "step_id": step_id or None,
                    "step_ordinal": normalized_step_ordinal,
                },
            }
        )

    return items, total_count
