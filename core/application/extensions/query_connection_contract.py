"""Compile-time rules for queries and pages that run with a project connection (ADR 0067)."""

from __future__ import annotations

from typing import Any

from core.application.extensions.extension_contract_policies import (
    QUERY_CONNECTION_PLATFORM_MIN,
)
from core.application.extensions.semver import compare_semver
from core.infrastructure.extension_runtime.extension_runtime_common import QUERY_REFERENCE_KEYS
from core.infrastructure.extension_runtime.extension_runtime_validation import (
    _as_dict,
    _as_list,
    _issue,
)

PAGE_REFRESH_SECONDS_MIN = 10
PAGE_REFRESH_SECONDS_MAX = 3600


def declared_connection_type_ids(value: Any) -> tuple[str, ...] | None:
    """The type ids of a valid ``connection`` declaration, or None when it is malformed."""
    if not isinstance(value, dict) or set(value) != {"connection_type_ids"}:
        return None
    raw = value.get("connection_type_ids")
    if not isinstance(raw, list) or not raw:
        return None
    ids: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or item.strip() in ids:
            return None
        ids.append(item.strip())
    return tuple(ids)


def query_connection_type_ids(query: Any) -> frozenset[str]:
    """The connection types a compiled query declares; empty for an ordinary query."""
    row = query if isinstance(query, dict) else {}
    if "connection" not in row:
        return frozenset()
    return frozenset(declared_connection_type_ids(row.get("connection")) or ())


def _own_connection_type_ids(compiled_manifest: dict[str, Any]) -> set[str]:
    return {
        str(_as_dict(row).get("connection_type_id") or "").strip()
        for row in _as_list(compiled_manifest.get("connection_types"))
        if str(_as_dict(row).get("connection_type_id") or "").strip()
    }


def _external_effect_operation_ids(compiled_manifest: dict[str, Any]) -> set[str]:
    return {
        str(_as_dict(row).get("operation_id") or "").strip()
        for row in _as_list(compiled_manifest.get("external_effects"))
        if str(_as_dict(row).get("operation_id") or "").strip()
    }


def _declares_host_minimum(compiled_manifest: dict[str, Any]) -> bool:
    runtime = _as_dict(compiled_manifest.get("runtime"))
    minimum = str(_as_dict(runtime.get("compatibility")).get("platform_min") or "").strip()
    try:
        return (
            compare_semver(minimum, QUERY_CONNECTION_PLATFORM_MIN, field_name="platform_min") >= 0
        )
    except ValueError:
        return False


def _page_query_references(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, list):
        for item in value:
            found |= _page_query_references(item)
    elif isinstance(value, dict):
        for key, nested in value.items():
            if key in QUERY_REFERENCE_KEYS and isinstance(nested, str) and nested.strip():
                found.add(nested.strip())
            found |= _page_query_references(nested)
    return found


def _validate_query_connections(
    compiled_manifest: dict[str, Any], *, issues: list[dict], manifest_path: str
) -> dict[str, frozenset[str]]:
    """Check every query's ``connection``; return the valid declarations by query id."""
    own_types = _own_connection_type_ids(compiled_manifest)
    effect_ids = _external_effect_operation_ids(compiled_manifest)
    declared: dict[str, frozenset[str]] = {}
    needs_host_minimum = False
    for idx, row in enumerate(_as_list(compiled_manifest.get("query_definitions"))):
        query = _as_dict(row)
        if "connection" not in query:
            continue
        query_id = str(query.get("query_id") or "").strip()
        field_path = f"query_definitions[{idx}].connection"
        type_ids = declared_connection_type_ids(query.get("connection"))
        if type_ids is None:
            issues.append(
                _issue(
                    severity="error",
                    code="query_connection_invalid",
                    message=(
                        "A query connection is {connection_type_ids: [...]}: a non-empty "
                        "list of unique connection type ids and no other key"
                    ),
                    file_path=manifest_path,
                    field_path=field_path,
                )
            )
            continue
        needs_host_minimum = True
        unknown = [type_id for type_id in type_ids if type_id not in own_types]
        if unknown:
            issues.append(
                _issue(
                    severity="error",
                    code="query_connection_type_unknown",
                    message=(
                        f"Query '{query_id}' names connection types this extension does not "
                        f"declare: {', '.join(unknown)}"
                    ),
                    file_path=manifest_path,
                    field_path=f"{field_path}.connection_type_ids",
                )
            )
        handler_mode = (
            str(_as_dict(query.get("handler")).get("mode") or "extension").strip().lower()
            or "extension"
        )
        if handler_mode != "extension":
            issues.append(
                _issue(
                    severity="error",
                    code="query_connection_handler_mode",
                    message=f"Query '{query_id}' runs with a connection, so handler.mode is extension",
                    file_path=manifest_path,
                    field_path=f"query_definitions[{idx}].handler.mode",
                )
            )
        if query_id in effect_ids:
            issues.append(
                _issue(
                    severity="error",
                    code="query_connection_external_effect",
                    message=(
                        f"Query '{query_id}' runs with a connection and only reads; effects "
                        "belong to actions, so external_effects must not name it"
                    ),
                    file_path=manifest_path,
                    field_path="external_effects",
                )
            )
        declared[query_id] = frozenset(type_ids)
    if needs_host_minimum and not _declares_host_minimum(compiled_manifest):
        issues.append(
            _issue(
                severity="error",
                code="query_connection_host_minimum",
                message=(
                    "Queries that run with a connection require host contract "
                    f"{QUERY_CONNECTION_PLATFORM_MIN} or later"
                ),
                file_path=manifest_path,
                field_path="runtime.compatibility.platform_min",
            )
        )
    return declared


def _validate_page_connection(
    page: dict[str, Any],
    *,
    page_path: str,
    own_types: set[str],
    connection_queries: dict[str, frozenset[str]],
    issues: list[dict],
    manifest_path: str,
) -> None:
    referenced = _page_query_references(page.get("components"))
    page_id = str(page.get("page_id") or "").strip()
    if "connection" not in page:
        for query_id in sorted(referenced & set(connection_queries)):
            issues.append(
                _issue(
                    severity="error",
                    code="page_connection_missing",
                    message=(
                        f"Page '{page_id}' uses query '{query_id}', which runs with a "
                        "connection; declare the page's connection"
                    ),
                    file_path=manifest_path,
                    field_path=f"{page_path}.connection",
                )
            )
        return
    type_ids = declared_connection_type_ids(page.get("connection"))
    if type_ids is None:
        issues.append(
            _issue(
                severity="error",
                code="page_connection_invalid",
                message=(
                    "A page connection is {connection_type_ids: [...]}: a non-empty list of "
                    "unique connection type ids and no other key"
                ),
                file_path=manifest_path,
                field_path=f"{page_path}.connection",
            )
        )
        return
    unknown = [type_id for type_id in type_ids if type_id not in own_types]
    if unknown:
        issues.append(
            _issue(
                severity="error",
                code="page_connection_invalid",
                message=(
                    f"Page '{page_id}' names connection types this extension does not "
                    f"declare: {', '.join(unknown)}"
                ),
                file_path=manifest_path,
                field_path=f"{page_path}.connection.connection_type_ids",
            )
        )
    if page.get("project_scoped") is False:
        issues.append(
            _issue(
                severity="error",
                code="page_connection_requires_project",
                message=f"Page '{page_id}' declares a connection, so it is project-scoped",
                file_path=manifest_path,
                field_path=f"{page_path}.project_scoped",
            )
        )
    for query_id in sorted(referenced & set(connection_queries)):
        missing = [t for t in type_ids if t not in connection_queries[query_id]]
        if missing:
            issues.append(
                _issue(
                    severity="error",
                    code="page_connection_type_mismatch",
                    message=(
                        f"Query '{query_id}' does not accept the page's connection types: "
                        f"{', '.join(missing)}"
                    ),
                    file_path=manifest_path,
                    field_path=f"{page_path}.connection.connection_type_ids",
                )
            )


def _validate_page_refresh(
    page: dict[str, Any], *, page_path: str, issues: list[dict], manifest_path: str
) -> None:
    if "refresh_seconds" not in page:
        return
    value = page.get("refresh_seconds")
    valid = (
        isinstance(value, int)
        and not isinstance(value, bool)
        and PAGE_REFRESH_SECONDS_MIN <= value <= PAGE_REFRESH_SECONDS_MAX
    )
    if valid and _page_query_references(page.get("components")):
        return
    issues.append(
        _issue(
            severity="error",
            code="page_refresh_invalid",
            message=(
                f"refresh_seconds is a whole number of seconds from {PAGE_REFRESH_SECONDS_MIN} "
                f"to {PAGE_REFRESH_SECONDS_MAX}, on a page that shows at least one query"
            ),
            file_path=manifest_path,
            field_path=f"{page_path}.refresh_seconds",
        )
    )


def validate_query_connection_contract(
    compiled_manifest: dict[str, Any], *, issues: list[dict], manifest_path: str
) -> None:
    """Refuse a release whose connection queries or pages break ADR 0067's rules."""
    connection_queries = _validate_query_connections(
        compiled_manifest, issues=issues, manifest_path=manifest_path
    )
    own_types = _own_connection_type_ids(compiled_manifest)
    pages = _as_list(_as_dict(compiled_manifest.get("ui_manifest")).get("pages"))
    for idx, raw_page in enumerate(pages):
        page = _as_dict(raw_page)
        page_path = f"ui.pages[{idx}]"
        _validate_page_connection(
            page,
            page_path=page_path,
            own_types=own_types,
            connection_queries=connection_queries,
            issues=issues,
            manifest_path=manifest_path,
        )
        _validate_page_refresh(
            page, page_path=page_path, issues=issues, manifest_path=manifest_path
        )


__all__ = [
    "PAGE_REFRESH_SECONDS_MAX",
    "PAGE_REFRESH_SECONDS_MIN",
    "declared_connection_type_ids",
    "query_connection_type_ids",
    "validate_query_connection_contract",
]
