"""App bootstrap endpoint for the React migration shell."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.auth.effective_permissions import resolve_effective_permissions
from core.application.extensions.system_email import system_email_available
from core.application.reports.ui_page_registry import HOST_RENDERER_KINDS
from core.application.settings.date_time_format import stored_date_format, stored_time_format
from core.application.settings.feature_endpoints import entitled_feature_endpoints
from core.infrastructure.config.installation_mode import installation_mode
from core.infrastructure.config.workflow_file_limits import (
    browser_run_upload_limits,
    file_preview_max_bytes,
)


def _normalized_roles(auth_user: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(auth_user, Mapping):
        return []
    raw = auth_user.get("roles")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    normalized = [str(item or "").strip().lower() for item in values if str(item or "").strip()]
    deduped: list[str] = []
    for role in normalized:
        if role not in deduped:
            deduped.append(role)
    return deduped


def _request_effective_permissions(request: Request) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    auth_user = getattr(request.state, "auth_user", None)
    if isinstance(auth_user, Mapping):
        roles = _normalized_roles(auth_user)
        if roles:
            merged["roles"] = roles
        auth_perms = auth_user.get("effective_permissions")
        if isinstance(auth_perms, Mapping):
            merged.update(dict(auth_perms))
    return merged


def _permission_keys(auth_user: Mapping[str, Any]) -> list[str] | None:
    """The ADR 0076 permissions the middleware computed for this person, sorted.

    ``None`` for a principal that is not role-based (a service account, a system
    actor): its own rules apply and it holds no role permissions.
    """
    raw = auth_user.get("permissions")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return None
    return sorted({str(item).strip() for item in raw if str(item).strip()})


def _project_permission_keys(auth_user: Mapping[str, Any]) -> dict[str, list[str]] | None:
    """Per-project permission keys of a person limited to chosen projects, sorted.

    ``None`` when the person has no project limit: ``permissions`` applies in every
    project. Sent whether or not the account still holds the feature that writes
    limits, because a stored limit keeps applying (ADR 0076 §8, 2026-11-26).
    """
    raw = auth_user.get("project_permissions")
    if not isinstance(raw, Mapping):
        return None
    result: dict[str, list[str]] = {}
    for project_id, keys in raw.items():
        pid = str(project_id or "").strip()
        if not pid or not isinstance(keys, (list, tuple, set, frozenset)):
            continue
        result[pid] = sorted({str(item).strip() for item in keys if str(item).strip()})
    return result


def _keys(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _workspace_permission_keys(auth_user: Mapping[str, Any]) -> dict[str, list[str]] | None:
    """Per-workspace permission keys of a limited person; ``None`` without a limit.

    A workspace with a grant carries that grant's role; a workspace shown only because
    a project or workflow inside it is granted carries read access (ADR 0076 §8a).
    """
    raw = auth_user.get("workspace_permissions")
    if not isinstance(raw, Mapping):
        return None if auth_user.get("project_permissions") is None else {}
    return {
        str(key).strip(): keys
        for key, value in raw.items()
        if str(key or "").strip() and (keys := _keys(value)) is not None
    }


def _workflow_permission_keys(
    auth_user: Mapping[str, Any],
) -> dict[str, dict[str, list[str]]] | None:
    """``{project id: {workflow id: keys}}`` of a limited person's workflow grants.

    ``None`` without a limit. A workflow not listed takes its project's keys, unless
    its project is listed in ``project_workflow_ids`` (reached only through workflow
    grants), where it is out of reach.
    """
    raw = auth_user.get("workflow_permissions")
    if not isinstance(raw, Mapping):
        return None if auth_user.get("project_permissions") is None else {}
    result: dict[str, dict[str, list[str]]] = {}
    for project_id, flows in raw.items():
        pid = str(project_id or "").strip()
        if not pid or not isinstance(flows, Mapping):
            continue
        result[pid] = {
            str(workflow_id).strip(): keys
            for workflow_id, value in flows.items()
            if str(workflow_id or "").strip() and (keys := _keys(value)) is not None
        }
    return result


def _project_workflow_ids(auth_user: Mapping[str, Any]) -> dict[str, list[str]] | None:
    """Projects a limited person reaches only through workflow grants, with those workflows."""
    raw = auth_user.get("project_workflow_ids")
    if not isinstance(raw, Mapping):
        return None if auth_user.get("project_permissions") is None else {}
    return {
        str(project_id).strip(): keys
        for project_id, value in raw.items()
        if str(project_id or "").strip() and (keys := _keys(value)) is not None
    }


def _principal_type(principal: Any, auth_user: Mapping[str, Any] | None) -> str:
    value = str(getattr(principal, "principal_type", "") or "").strip().lower()
    if value:
        return value
    roles = set(_normalized_roles(auth_user))
    if "admin" in roles:
        return "admin"
    if "operator" in roles:
        return "operator"
    return "viewer"


def _user_summary(
    auth_user: Mapping[str, Any] | None,
    date_time_format_reader: Callable[[Mapping[str, Any]], tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(auth_user, Mapping):
        return None
    stored_date, stored_time = "", ""
    if callable(date_time_format_reader):
        stored_date, stored_time = date_time_format_reader(auth_user)
    return {
        "id": str(auth_user.get("id") or ""),
        "username": str(auth_user.get("username") or ""),
        "email": str(auth_user.get("email") or auth_user.get("username") or ""),
        "display_name": str(auth_user.get("display_name") or ""),
        "roles": _normalized_roles(auth_user),
        # ADR 0076: what the person may do; the interface derives menus and buttons
        # from these keys. ``roles`` stays until the interface reads only these.
        "permissions": _permission_keys(auth_user),
        # A person limited to chosen projects: what they may do in each of them.
        "project_permissions": _project_permission_keys(auth_user),
        # ADR 0076 §8a: the same per workspace and per granted workflow, and the
        # projects where only the granted workflows are reachable.
        "workspace_permissions": _workspace_permission_keys(auth_user),
        "workflow_permissions": _workflow_permission_keys(auth_user),
        "project_workflow_ids": _project_workflow_ids(auth_user),
        "account_id": str(auth_user.get("account_id") or ""),
        "account_ids": list(auth_user.get("account_ids") or []),
        "account_memberships": list(auth_user.get("account_memberships") or []),
        "timezone": str(auth_user.get("timezone") or ""),
        "locale": str(auth_user.get("locale") or ""),
        "date_format": stored_date_format(stored_date),
        "time_format": stored_time_format(stored_time),
        "principal_type": str(auth_user.get("principal_type") or "user"),
    }


def _normalize_settings_pages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return rows


def _normalize_registered_pages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        record = dict(item)
        path = str(record.get("path") or "").strip()
        component_key = str(record.get("component_key") or "").strip()
        page_key = str(record.get("page_key") or record.get("key") or "").strip()
        control_surface = record.get("control_surface")
        host_rendered = (
            isinstance(control_surface, Mapping)
            and str(control_surface.get("kind") or "").strip() in HOST_RENDERER_KINDS
        )
        if not page_key or not path.startswith("/") or not (component_key or host_rendered):
            continue
        if host_rendered:
            record["control_surface"] = dict(control_surface)
        record["key"] = str(record.get("key") or page_key)
        record["page_key"] = page_key
        record["path"] = path
        record["component_key"] = component_key
        rows.append(record)
    return rows


def _normalize_shell_navigation(payload: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(payload, Mapping):
        return {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    for section, items in payload.items():
        section_key = str(section or "").strip()
        if not section_key or not isinstance(items, list):
            continue
        rows: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, Mapping):
                rows.append(dict(item))
        normalized[section_key] = rows
    return normalized


def _normalize_external_sign_in(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    providers_path = str(payload.get("providers_path") or "").strip()
    if not providers_path.startswith("/"):
        return {}
    return {"providers_path": providers_path}


def create_api_app_bootstrap_router(
    *,
    request_principal: Callable[[Request], Any],
    local_auth_enabled: Callable[[], bool],
    local_auth_bootstrap_required: Callable[[], bool],
    external_auth_enabled: Callable[[], bool] | bool,
    current_app_edition: Callable[[], str],
    platform_contract: Callable[[str], Any],
    build_client_i18n_payload: Callable[..., dict[str, Any]],
    settings_pages_resolver: Callable[..., Any] | None = None,
    registered_pages_resolver: Callable[..., Any] | None = None,
    shell_navigation_resolver: Callable[..., Any] | None = None,
    external_sign_in_resolver: Callable[..., Any] | None = None,
    date_time_format_reader: Callable[[Mapping[str, Any]], tuple[str, str]] | None = None,
    api_error_response: Callable[..., JSONResponse],
    feature_endpoints_resolver: Callable[[], Mapping[str, str]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["api-app-bootstrap"])

    @router.get("/api/app/bootstrap")
    def api_app_bootstrap(request: Request):
        try:
            auth_user = getattr(request.state, "auth_user", None)
            principal = request_principal(request)
            authenticated = isinstance(auth_user, Mapping)
            principal_type = _principal_type(principal, auth_user if authenticated else None)

            permissions = resolve_effective_permissions(
                principal_type=principal_type,
                effective_permissions=_request_effective_permissions(request),
            )
            auth_source = str(getattr(request.state, "auth_source", "") or "").strip().lower()

            # The browser sees the features of the signed-in account only.
            account_id = str(auth_user.get("account_id") or "").strip() if authenticated else ""
            entitlement_service = platform_contract("entitlement_service")
            snapshot_fn = getattr(entitlement_service, "snapshot", None)
            feature_flags = (
                dict(snapshot_fn(account_id) or {}) if account_id and callable(snapshot_fn) else {}
            )

            local_enabled = bool(local_auth_enabled())
            bootstrap_required = bool(local_auth_bootstrap_required())
            external_enabled = (
                external_auth_enabled()
                if callable(external_auth_enabled)
                else bool(external_auth_enabled)
            )
            auth_boundary_enabled = local_enabled or external_enabled or bootstrap_required
            include_authenticated_metadata = authenticated or not auth_boundary_enabled
            client_feature_flags = feature_flags if include_authenticated_metadata else {}
            # ADR 0082: where a module-delivered feature's API lives, for entitled
            # features only; an absent endpoint keeps Core's control hidden.
            client_feature_endpoints = entitled_feature_endpoints(
                feature_endpoints_resolver() if callable(feature_endpoints_resolver) else {},
                client_feature_flags,
            )
            client_edition = "community"
            if include_authenticated_metadata:
                client_edition = str(current_app_edition() or "").strip() or "community"
            external_sign_in = _normalize_external_sign_in(
                external_sign_in_resolver() if callable(external_sign_in_resolver) else {}
            )
            i18n = build_client_i18n_payload(
                request,
                auth_user=auth_user if isinstance(auth_user, Mapping) else None,
            )
            locale = str(i18n.get("locale") or "en")
            settings_pages = (
                settings_pages_resolver(auth_user=auth_user, locale=locale)
                if include_authenticated_metadata and callable(settings_pages_resolver)
                else []
            )
            registered_pages = (
                registered_pages_resolver(auth_user=auth_user, locale=locale)
                if include_authenticated_metadata and callable(registered_pages_resolver)
                else []
            )
            shell_navigation = (
                shell_navigation_resolver(auth_user=auth_user, locale=locale)
                if include_authenticated_metadata and callable(shell_navigation_resolver)
                else {}
            )

            return {
                "status": "ok",
                "auth": {
                    "authenticated": authenticated,
                    "local_auth_enabled": local_enabled,
                    "bootstrap_required": bootstrap_required,
                    "external_auth_enabled": external_enabled,
                    "auth_source": auth_source if authenticated else "",
                    "user": _user_summary(
                        auth_user if authenticated else None, date_time_format_reader
                    ),
                    "roles": _normalized_roles(auth_user if authenticated else None),
                    "permissions": permissions,
                    "account_memberships": list(
                        (
                            auth_user.get("account_memberships")
                            if isinstance(auth_user, Mapping)
                            else []
                        )
                        or []
                    ),
                    "external_sign_in": external_sign_in,
                    # Reset links and email-change verifications can only be sent when
                    # an email extension is connected; otherwise an admin does both.
                    "system_email_available": system_email_available(),
                },
                "feature_flags": client_feature_flags,
                "feature_endpoints": client_feature_endpoints,
                "environment": {
                    "edition": client_edition,
                    "mode": installation_mode(),
                },
                # ADR 0075: the browser checks a real run's files against the same
                # policy the server enforces, instead of a fixed size. ADR 0080: a
                # file over the preview limit is offered as a download only.
                "limits": {
                    "run_upload": browser_run_upload_limits()
                    if include_authenticated_metadata
                    else {},
                    "file_preview": {"max_bytes": file_preview_max_bytes()}
                    if include_authenticated_metadata
                    else {},
                },
                "i18n": i18n,
                "settings_pages": _normalize_settings_pages(settings_pages),
                "registered_pages": _normalize_registered_pages(registered_pages),
                "shell_navigation": _normalize_shell_navigation(shell_navigation),
            }
        except Exception as exc:  # pragma: no cover - safety net mirrors other API routes
            return api_error_response(exc, request=request)

    return router


__all__ = ["create_api_app_bootstrap_router"]
