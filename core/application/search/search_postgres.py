from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from core.application.search.search_postgres_catalog import _catalog_rows
from core.application.search.search_postgres_common import (
    _column_exists,
    _rollback_quietly,
)
from core.application.search.search_postgres_events import _event_rows, _runtime_message_rows
from core.application.search.search_postgres_jobs import _job_rows
from core.application.search.search_postgres_workflows import _workflow_rows
from core.infrastructure.db import psycopg_runtime as psycopg_runtime

from .search_timeout import SearchQueryTimeoutError

logger = logging.getLogger(__name__)


def search_postgres_sources(
    *,
    bl: Any,
    query: str,
    scope: Any,
    filters: Mapping[str, Any],
    limit: int,
    project_ids: Sequence[str] | None,
    groups: Sequence[str],
    include_totals: bool = False,
) -> dict[str, Any]:
    if bl is None or not hasattr(bl, "conn"):
        if include_totals:
            return {group: {"items": [], "total": 0} for group in groups}
        return {group: [] for group in groups}

    account_id = str(getattr(scope, "account_id", None) or filters.get("account_id") or "").strip()
    actor_name = str(getattr(scope, "actor_name", None) or "").strip() or None
    tenant_id = str(getattr(scope, "tenant_id", None) or "").strip() or None
    jobs_has_deleted_at = _column_exists(bl, "jobs", "deleted_at")
    projects_has_deleted_at = _column_exists(bl, "projects", "deleted_at")
    jobs_has_tenant_id = _column_exists(bl, "jobs", "tenant_id")
    projects_has_tenant_id = _column_exists(bl, "projects", "tenant_id")
    normalized_filters = dict(filters)
    normalized_filters["actor_name"] = actor_name
    normalized_filters["tenant_id"] = tenant_id
    normalized_filters["_jobs_has_deleted_at"] = jobs_has_deleted_at
    normalized_filters["_projects_has_deleted_at"] = projects_has_deleted_at
    normalized_filters["_jobs_has_tenant_id"] = jobs_has_tenant_id
    normalized_filters["_projects_has_tenant_id"] = projects_has_tenant_id

    results = (
        {group: {"items": [], "total": 0} for group in groups}
        if include_totals
        else {group: [] for group in groups}
    )
    for group in groups:
        try:
            if (
                tenant_id is not None
                and group in {"jobs", "events", "runtime_messages"}
                and project_ids is None
                and not jobs_has_tenant_id
                and not projects_has_tenant_id
            ):
                # Fail closed: without tenant columns or a tenant-scoped project
                # set, this query cannot safely isolate tenant data.
                results[group] = {"items": [], "total": 0} if include_totals else []
                continue
            if group == "jobs":
                items, total = _job_rows(
                    bl=bl,
                    query=query,
                    account_id=account_id,
                    project_ids=project_ids,
                    filters=normalized_filters,
                    limit=limit,
                    include_totals=include_totals,
                )
            elif group == "events":
                items, total = _event_rows(
                    bl=bl,
                    query=query,
                    account_id=account_id,
                    project_ids=project_ids,
                    filters=normalized_filters,
                    limit=limit,
                    include_totals=include_totals,
                )
            elif group == "runtime_messages":
                items, total = _runtime_message_rows(
                    bl=bl,
                    query=query,
                    account_id=account_id,
                    project_ids=project_ids,
                    filters=normalized_filters,
                    limit=limit,
                    include_totals=include_totals,
                )
            elif group == "workflows":
                items, total = _workflow_rows(
                    bl=bl,
                    query=query,
                    account_id=account_id,
                    project_ids=project_ids,
                    limit=limit,
                    include_totals=include_totals,
                )
            elif group in {
                "agents",
                "tools",
                "skills",
                "mcp_connectors",
                "projects",
                "extensions",
                "guardrails",
                "data_contracts",
                "fields",
                "published_blocks",
                "datasets",
                "saved_queries",
                "data_connections",
                "model_providers",
                "model_routes",
                "users",
                "workspaces",
            }:
                items, total = _catalog_rows(
                    bl=bl,
                    group=group,
                    query=query,
                    account_id=account_id,
                    tenant_id=tenant_id,
                    workspace_id=str(filters.get("workspace_id") or "").strip() or None,
                    project_ids=project_ids,
                    limit=limit,
                    include_totals=include_totals,
                )
            else:
                items, total = [], 0
            results[group] = {"items": items, "total": int(total or 0)} if include_totals else items
        except SearchQueryTimeoutError:
            raise
        except Exception as exc:
            logger.warning("search_postgres_sources[%s] failed: %s", group, exc)
            _rollback_quietly(bl.conn)
            results[group] = {"items": [], "total": 0} if include_totals else []
    return results


__all__ = ["search_postgres_sources"]
