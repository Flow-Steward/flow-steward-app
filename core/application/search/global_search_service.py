from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from core.application.auth.principal import (
    coerce_optional_project_id,
    principal_type_has_minimum_role,
)
from core.application.management.errors import ServiceUnavailableError, ServiceValidationError
from core.application.management.guards import require_project_access
from core.application.management.scope import ScopeContext, normalize_scope
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

from .global_search_categories import (
    PUBLIC_RELEASE_ENTITY_FAMILIES,
    supported_global_search_result_groups,
)
from .search_postgres import search_postgres_sources
from .search_timeout import SearchQueryTimeoutError, search_timeout_budget

GROUP_ITEM_LIMIT_MUST_BE_AN_INTEGER_VALUE = "group_item_limit must be an integer"
INVALID_SEARCH_CURSOR_VALUE = "Invalid search cursor"
SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE = "Scope account_id is required"


logger = logging.getLogger(__name__)

SUPPORTED_RESULT_GROUPS = supported_global_search_result_groups()
POSTGRES_RESULT_GROUPS = (
    "jobs",
    "events",
    "runtime_messages",
    "workflows",
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
)
PROJECT_OPTIONAL_RESULT_GROUPS = (
    "workflows",
    "agents",
    "tools",
    "skills",
    "mcp_connectors",
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
)
SOURCE_WEIGHTS = {
    "postgres.jobs": 1.0,
    "postgres.platform_events": 0.95,
    "postgres.platform_agent_messages": 0.9,
    "postgres.workflows": 0.94,
}
_SEARCH_CURSOR_VERSION = 1
DEFAULT_SEARCH_QUERY_TIMEOUT_SECONDS = 10.0


def _scope_account_id(scope: Any) -> str:
    if scope is None:
        raise ServiceValidationError(
            SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE,
            payload={"error": SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE},
        )
    if isinstance(scope, ScopeContext):
        account_id = str(scope.account_id or "").strip()
        if not account_id:
            raise ServiceValidationError(
                SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE,
                payload={"error": SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE},
            )
        return account_id
    if isinstance(scope, Mapping):
        if "account_id" not in scope:
            raise ServiceValidationError(
                SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE,
                payload={"error": SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE},
            )
        account_id = str(scope.get("account_id") or "").strip()
        if not account_id:
            raise ServiceValidationError(
                SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE,
                payload={"error": SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE},
            )
        return account_id
    account_id = str(getattr(scope, "account_id", "") or "").strip()
    if not account_id:
        raise ServiceValidationError(
            SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE,
            payload={"error": SCOPE_ACCOUNT_ID_IS_REQUIRED_MESSAGE},
        )
    return account_id


def _scope_tenant_id(scope: Any) -> str | None:
    if isinstance(scope, Mapping):
        return str(scope.get("tenant_id") or "").strip() or None
    return str(getattr(scope, "tenant_id", "") or "").strip() or None


def _resolve_scope(scope: Any) -> ScopeContext:
    account_id = _scope_account_id(scope)
    tenant_id = _scope_tenant_id(scope)
    return normalize_scope(scope, account_id=account_id, tenant_id=tenant_id)


def _get_logger():
    try:
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.db.platform_stores import PlatformProjectStore

        dsn = str(
            CONFIG.get("fs_observability_dsn")
            or CONFIG.get("fs_db_dsn")
            or _runtime_env.get("FS_OBSERVABILITY_DSN", "")
            or _runtime_env.get("FS_DB_DSN", "")
        ).strip()
        if not dsn:
            return None
        return PlatformProjectStore(dsn)
    except Exception:
        return None


def _normalize_query(query: str) -> str:
    normalized = " ".join(str(query or "").split()).strip()
    if not normalized:
        raise ServiceValidationError(
            "Search query is required",
            payload={"error": "Search query is required"},
        )
    return normalized


def _normalize_limit(limit: int) -> int:
    return max(1, min(int(limit or 20), 50))


def _encode_search_cursor(group: str, offset: int) -> str:
    payload = json.dumps(
        {
            "v": _SEARCH_CURSOR_VERSION,
            "group": str(group or "").strip().lower(),
            "offset": max(0, int(offset or 0)),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_search_cursor(
    raw_cursor: Any,
    *,
    groups: Sequence[str],
    grouped_preview: bool,
) -> dict[str, Any] | None:
    text = str(raw_cursor or "").strip()
    if not text:
        return None
    if grouped_preview:
        raise ServiceValidationError(
            "Search cursor is not supported for grouped previews",
            payload={"error": "Search cursor is not supported for grouped previews"},
        )
    if len(groups) != 1:
        raise ServiceValidationError(
            "Search cursor requires a single result group",
            payload={"error": "Search cursor requires a single result group"},
        )
    padded = text + ("=" * (-len(text) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(decoded)
    except Exception as exc:
        raise ServiceValidationError(
            INVALID_SEARCH_CURSOR_VALUE,
            payload={"error": INVALID_SEARCH_CURSOR_VALUE},
        ) from exc

    if not isinstance(payload, Mapping):
        raise ServiceValidationError(
            INVALID_SEARCH_CURSOR_VALUE,
            payload={"error": INVALID_SEARCH_CURSOR_VALUE},
        )

    try:
        version = int(payload.get("v") or 0)
        offset = int(payload.get("offset") or 0)
    except Exception as exc:
        raise ServiceValidationError(
            INVALID_SEARCH_CURSOR_VALUE,
            payload={"error": INVALID_SEARCH_CURSOR_VALUE},
        ) from exc

    group = str(payload.get("group") or "").strip().lower()
    if version != _SEARCH_CURSOR_VERSION or group not in groups or offset < 0:
        raise ServiceValidationError(
            INVALID_SEARCH_CURSOR_VALUE,
            payload={"error": INVALID_SEARCH_CURSOR_VALUE},
        )
    return {"group": group, "offset": offset}


def _normalize_group_item_limit(filters: Mapping[str, Any] | None) -> int | None:
    raw_value = (filters or {}).get("group_item_limit")
    if raw_value in (None, ""):
        return None
    if isinstance(raw_value, bool):
        raise ServiceValidationError(
            GROUP_ITEM_LIMIT_MUST_BE_AN_INTEGER_VALUE,
            payload={"error": GROUP_ITEM_LIMIT_MUST_BE_AN_INTEGER_VALUE},
        )
    try:
        parsed = int(str(raw_value))
    except Exception as exc:
        raise ServiceValidationError(
            GROUP_ITEM_LIMIT_MUST_BE_AN_INTEGER_VALUE,
            payload={"error": GROUP_ITEM_LIMIT_MUST_BE_AN_INTEGER_VALUE},
        ) from exc
    return max(1, min(parsed, 20))


def _normalize_result_groups(filters: Mapping[str, Any] | None) -> tuple[str, ...]:
    requested = (filters or {}).get("result_groups")
    if requested is None or requested == "":
        return SUPPORTED_RESULT_GROUPS
    if isinstance(requested, str):
        values = [part.strip().lower() for part in requested.split(",") if part.strip()]
    elif isinstance(requested, Sequence):
        values = [str(part or "").strip().lower() for part in requested if str(part or "").strip()]
    else:
        raise ServiceValidationError(
            "result_groups must be a string or list",
            payload={"error": "result_groups must be a string or list"},
        )
    unsupported = sorted({value for value in values if value not in SUPPORTED_RESULT_GROUPS})
    if unsupported:
        raise ServiceValidationError(
            "Unsupported result_groups value",
            payload={"error": "Unsupported result_groups value", "unsupported": unsupported},
        )
    unique = []
    seen = set()
    for group in values:
        if group in seen:
            continue
        seen.add(group)
        unique.append(group)
    return tuple(unique or SUPPORTED_RESULT_GROUPS)


def _authorized_result_groups(groups: Sequence[str], scope: ScopeContext) -> tuple[str, ...]:
    principal_type = str(scope.principal_type or "user").strip().lower() or "user"
    return tuple(
        group
        for group in groups
        if (family := PUBLIC_RELEASE_ENTITY_FAMILIES.get(group)) is not None
        and principal_type_has_minimum_role(principal_type, family.minimum_role)
    )


def _normalize_filter_text(filters: Mapping[str, Any] | None, key: str) -> str | None:
    value = str((filters or {}).get(key) or "").strip()
    return value.lower() if value else None


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_temporal_filter(value: Any, *, end_of_day: bool, scope: ScopeContext) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "T" not in text:
        day = date.fromisoformat(text)
        from core.application.settings.timezone_service import (
            day_boundaries_utc,
            resolve_effective_user_tz,
        )

        effective_tz = resolve_effective_user_tz(scope.user_timezone or scope.project_timezone)
        start_dt, end_dt = day_boundaries_utc(day.isoformat(), effective_tz)
        return end_dt if end_of_day else start_dt

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_filters(filters: Mapping[str, Any] | None, scope: ScopeContext) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    normalized["project_id"] = coerce_optional_project_id((filters or {}).get("project_id"))
    normalized["workspace_id"] = str((filters or {}).get("workspace_id") or "").strip() or None

    job_id = (filters or {}).get("job_id")
    normalized["job_id"] = (
        str(job_id).strip() if job_id is not None and str(job_id).strip() else None
    )
    normalized["status"] = _normalize_filter_text(filters, "status")
    normalized["event_type"] = _normalize_filter_text(filters, "event_type")
    normalized["severity"] = _normalize_filter_text(filters, "severity")
    normalized["result_groups"] = _normalize_result_groups(filters)

    try:
        normalized["date_from_ts"] = _parse_temporal_filter(
            (filters or {}).get("date_from"),
            end_of_day=False,
            scope=scope,
        )
        normalized["date_to_ts"] = _parse_temporal_filter(
            (filters or {}).get("date_to"),
            end_of_day=True,
            scope=scope,
        )
    except ValueError as exc:
        raise ServiceValidationError(
            "Invalid date filter. Use YYYY-MM-DD or ISO datetime.",
            payload={"error": "Invalid date filter. Use YYYY-MM-DD or ISO datetime."},
        ) from exc

    if (
        normalized["date_from_ts"] is not None
        and normalized["date_to_ts"] is not None
        and normalized["date_from_ts"] > normalized["date_to_ts"]
    ):
        raise ServiceValidationError(
            "date_from must be <= date_to",
            payload={"error": "date_from must be <= date_to"},
        )

    return normalized


def _project_rows_for_scope(bl: Any, scope: ScopeContext) -> list[dict[str, Any]]:
    tenant_id = str(getattr(scope, "tenant_id", None) or "").strip() or None
    rows = bl.get_projects(account_id=scope.account_id) if hasattr(bl, "get_projects") else []
    scoped_rows: list[dict[str, Any]] = []
    for raw_row in rows or []:
        if not isinstance(raw_row, Mapping):
            continue
        row = dict(raw_row)
        try:
            require_project_access(scope, row)
        except Exception:
            continue
        if tenant_id:
            row_tenant_id = str(row.get("tenant_id") or "").strip() or None
            if row_tenant_id:
                if row_tenant_id != tenant_id:
                    continue
            elif not scope.allowed_project_ids:
                # Fail closed when tenant scope is active but the project row
                # provides no tenant marker and we have no explicit allow-list.
                continue
        scoped_rows.append(row)
    return scoped_rows


def _select_project_rows(
    *,
    scope: ScopeContext,
    filters: Mapping[str, Any],
    project_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    requested_project_id = coerce_optional_project_id(filters.get("project_id"))
    scope_project_id = coerce_optional_project_id(scope.project_id)
    if (
        scope_project_id is not None
        and requested_project_id is not None
        and requested_project_id != scope_project_id
    ):
        return []

    effective_project_id = (
        requested_project_id if requested_project_id is not None else scope_project_id
    )
    filtered = [dict(row) for row in project_rows]
    if effective_project_id is not None:
        filtered = [
            row
            for row in filtered
            if coerce_optional_project_id(row.get("id")) == effective_project_id
        ]
    workspace_id = str(filters.get("workspace_id") or "").strip()
    if workspace_id:
        filtered = [
            row for row in filtered if str(row.get("workspace_id") or "").strip() == workspace_id
        ]
    return filtered


def _filter_item_for_scope(
    item: Mapping[str, Any],
    *,
    scope: ScopeContext,
    project_id_set: frozenset[str] | None,
    selected_project_id: str | None,
) -> bool:
    group = str(item.get("group") or "").strip().lower()
    project_id = coerce_optional_project_id(item.get("project_id"))
    if (
        group in PROJECT_OPTIONAL_RESULT_GROUPS
        and selected_project_id is None
        and project_id is None
    ):
        return True
    if project_id_set is not None:
        return project_id is not None and project_id in project_id_set
    if selected_project_id is not None:
        return project_id == selected_project_id
    if scope.allowed_project_ids:
        return project_id is not None and project_id in scope.allowed_project_ids
    if scope.project_id is not None:
        return project_id == scope.project_id
    return True


def _scored_item(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    raw_score = float(payload.get("score") or 0.0)
    weight = SOURCE_WEIGHTS.get(str(payload.get("source") or "").strip(), 1.0)
    payload["score"] = round(max(0.0, min(raw_score, 1.0)) * weight, 6)
    return payload


def _normalize_backend_group_payload(
    payload: Any,
) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(payload, Mapping):
        raw_items = payload.get("items")
        total = payload.get("total")
        items = [dict(item) for item in (raw_items or []) if isinstance(item, Mapping)]
        try:
            normalized_total = int(total) if total is not None else None
        except Exception:
            normalized_total = None
        if normalized_total is not None:
            normalized_total = max(normalized_total, len(items))
        return items, normalized_total

    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        items = [dict(item) for item in payload if isinstance(item, Mapping)]
        return items, None
    return [], None


def _sort_key(item: Mapping[str, Any]) -> tuple[float, str, str]:
    timestamp = str(item.get("timestamp") or "")
    return (
        float(item.get("score") or 0.0),
        timestamp,
        str(item.get("id") or ""),
    )


class GlobalSearchService:
    def __init__(
        self,
        *,
        get_logger=None,
        postgres_search_fn=None,
        query_timeout_seconds: float = DEFAULT_SEARCH_QUERY_TIMEOUT_SECONDS,
    ) -> None:
        self._get_logger = get_logger or _get_logger
        self._postgres_search = postgres_search_fn or search_postgres_sources
        self._query_timeout_seconds = max(0.001, float(query_timeout_seconds))

    def global_search(
        self,
        query: str,
        *,
        scope: Any,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_query = _normalize_query(query)
        resolved_scope = _resolve_scope(scope)
        safe_limit = _normalize_limit(limit)
        normalized_filters = _normalize_filters(filters, resolved_scope)
        groups = tuple(normalized_filters["result_groups"])
        authorized_groups = _authorized_result_groups(groups, resolved_scope)
        group_item_limit = _normalize_group_item_limit(filters)
        cursor_info = _decode_search_cursor(
            (filters or {}).get("cursor"),
            groups=groups,
            grouped_preview=group_item_limit is not None,
        )
        page_offset = int((cursor_info or {}).get("offset") or 0)
        fetch_limit = max(
            safe_limit + page_offset
            if group_item_limit is None and len(groups) == 1
            else safe_limit,
            int(group_item_limit or 0),
        )

        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database not configured")

        project_rows = _project_rows_for_scope(bl, resolved_scope)
        selected_project_rows = _select_project_rows(
            scope=resolved_scope,
            filters=normalized_filters,
            project_rows=project_rows,
        )
        selected_project_id = coerce_optional_project_id(normalized_filters.get("project_id"))
        if selected_project_id is None:
            selected_project_id = coerce_optional_project_id(resolved_scope.project_id)
        tenant_id = str(getattr(resolved_scope, "tenant_id", None) or "").strip() or None
        selected_workspace_id = str(normalized_filters.get("workspace_id") or "").strip() or None

        if selected_project_rows:
            project_id_set: frozenset[str] | None = frozenset(
                project_id
                for row in selected_project_rows
                for project_id in [coerce_optional_project_id(row.get("id"))]
                if project_id is not None
            )
        elif selected_project_id is not None or selected_workspace_id is not None:
            project_id_set = frozenset()
        elif resolved_scope.allowed_project_ids:
            project_id_set = frozenset(
                project_id
                for row in project_rows
                for project_id in [coerce_optional_project_id(row.get("id"))]
                if project_id is not None
            )
        elif tenant_id is not None:
            project_id_set = frozenset(
                project_id
                for row in selected_project_rows
                for project_id in [coerce_optional_project_id(row.get("id"))]
                if project_id is not None
            )
        else:
            project_id_set = None
        backend_project_ids: Any = sorted(project_id_set) if project_id_set is not None else None

        postgres_groups = tuple(
            group for group in authorized_groups if group in POSTGRES_RESULT_GROUPS
        )
        postgres_results: dict[str, list[dict[str, Any]]] = {group: [] for group in postgres_groups}
        backend_errors: list[dict[str, str]] = []
        include_backend_totals = group_item_limit is None

        if postgres_groups:
            try:
                with search_timeout_budget(self._query_timeout_seconds):
                    postgres_results = self._postgres_search(
                        bl=bl,
                        query=normalized_query,
                        scope=resolved_scope,
                        filters=normalized_filters,
                        limit=fetch_limit,
                        project_ids=backend_project_ids,
                        groups=postgres_groups,
                        include_totals=include_backend_totals,
                    )
            except SearchQueryTimeoutError:
                raise
            except Exception as exc:
                logger.warning("global_search postgres backend failed: %s", exc)
                backend_errors.append({"backend": "postgres", "code": "search_backend_unavailable"})

        requested_backends = {
            backend
            for backend, requested_groups in (("postgres", postgres_groups),)
            if requested_groups
        }
        failed_backends = {entry["backend"] for entry in backend_errors}
        if requested_backends and failed_backends == requested_backends:
            backend = next(iter(failed_backends)) if len(failed_backends) == 1 else "multiple"
            raise ServiceUnavailableError(
                "Search backend unavailable",
                payload={
                    "error": "Search backend unavailable",
                    "code": "search_backend_unavailable",
                    "backend": backend,
                },
            )

        grouped_all_items = {group: [] for group in groups}
        group_totals = dict.fromkeys(groups, 0)
        for group, payload in (postgres_results or {}).items():
            if group not in grouped_all_items:
                continue
            items, backend_total = _normalize_backend_group_payload(payload)
            filtered_items: list[dict[str, Any]] = []
            for raw_item in items:
                if not isinstance(raw_item, Mapping):
                    continue
                if not _filter_item_for_scope(
                    raw_item,
                    scope=resolved_scope,
                    project_id_set=project_id_set,
                    selected_project_id=selected_project_id,
                ):
                    continue
                scored_item = _scored_item(raw_item)
                filtered_items.append(scored_item)
            grouped_all_items[group].extend(filtered_items)
            if backend_total is None:
                group_totals[group] += len(filtered_items)
            else:
                group_totals[group] += max(int(backend_total), len(filtered_items))

        grouped_items = {group: [] for group in groups}
        loaded_counts = dict.fromkeys(groups, 0)
        next_cursors = dict.fromkeys(groups)
        if group_item_limit is not None:
            for group in groups:
                ranked_group_items = sorted(
                    grouped_all_items[group],
                    key=_sort_key,
                    reverse=True,
                )
                grouped_items[group] = ranked_group_items[:group_item_limit]
                loaded_counts[group] = len(grouped_items[group])
                if int(group_totals[group]) > loaded_counts[group]:
                    next_cursors[group] = _encode_search_cursor(group, loaded_counts[group])
        elif len(groups) == 1:
            group = groups[0]
            ranked_group_items = sorted(grouped_all_items[group], key=_sort_key, reverse=True)
            grouped_items[group] = ranked_group_items[page_offset : page_offset + safe_limit]
            loaded_counts[group] = min(
                int(group_totals[group]), page_offset + len(grouped_items[group])
            )
            if int(group_totals[group]) > loaded_counts[group]:
                next_cursors[group] = _encode_search_cursor(group, loaded_counts[group])
        else:
            all_items = [item for items in grouped_all_items.values() for item in items]
            ranked_items = sorted(all_items, key=_sort_key, reverse=True)[:safe_limit]
            for item in ranked_items:
                group = str(item.get("group") or "").strip()
                if group in grouped_items:
                    grouped_items[group].append(item)
            for group in groups:
                loaded_counts[group] = len(grouped_items[group])

        return {
            "query": normalized_query,
            "limit": safe_limit,
            "cursor": str((filters or {}).get("cursor") or "").strip() or None,
            "group_item_limit": group_item_limit,
            "view_mode": "grouped_preview" if group_item_limit is not None else "ranked",
            "partial": bool(backend_errors),
            "backend_errors": backend_errors,
            "scope": {
                "account_id": resolved_scope.account_id,
                "tenant_id": str(getattr(resolved_scope, "tenant_id", None) or "").strip() or None,
                "project_id": selected_project_id,
                "workspace_id": normalized_filters.get("workspace_id"),
                "allowed_project_ids": sorted(project_id_set) if project_id_set else [],
            },
            "groups": [
                {
                    "group": group,
                    "total": int(group_totals[group]),
                    "items": grouped_items[group],
                    "returned_count": len(grouped_items[group]),
                    "loaded_count": loaded_counts[group],
                    "has_more": int(group_totals[group]) > loaded_counts[group],
                    "maybe_more": bool(
                        group_item_limit is not None
                        and int(group_totals[group]) <= loaded_counts[group]
                        and loaded_counts[group] >= group_item_limit
                    ),
                    "next_cursor": next_cursors[group],
                }
                for group in groups
            ],
            "related_context": [],
        }


def global_search(
    query: str,
    *,
    scope: Any,
    filters: Mapping[str, Any] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    return GlobalSearchService().global_search(
        query,
        scope=scope,
        filters=filters,
        limit=limit,
    )


__all__ = [
    "SUPPORTED_RESULT_GROUPS",
    "GlobalSearchService",
    "global_search",
]
