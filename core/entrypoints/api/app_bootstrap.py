"""App bootstrap endpoint for the React migration shell."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.auth.effective_permissions import resolve_effective_permissions
from core.application.extensions.system_email import system_email_available
from core.infrastructure.config.installation_mode import installation_mode


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

    state_perms = getattr(request.state, "effective_permissions", None)
    if isinstance(state_perms, Mapping):
        merged.update(dict(state_perms))
    return merged


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


def _user_summary(auth_user: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(auth_user, Mapping):
        return None
    return {
        "id": str(auth_user.get("id") or ""),
        "username": str(auth_user.get("username") or ""),
        "email": str(auth_user.get("email") or auth_user.get("username") or ""),
        "display_name": str(auth_user.get("display_name") or ""),
        "roles": _normalized_roles(auth_user),
        "account_id": str(auth_user.get("account_id") or ""),
        "account_ids": list(auth_user.get("account_ids") or []),
        "account_memberships": list(auth_user.get("account_memberships") or []),
        "timezone": str(auth_user.get("timezone") or ""),
        "locale": str(auth_user.get("locale") or ""),
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
        if not page_key or not path.startswith("/") or not component_key:
            continue
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
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["api-app-bootstrap"])

    @router.get("/api/app/bootstrap")
    async def api_app_bootstrap(request: Request):
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

            entitlement_service = platform_contract("entitlement_service")
            snapshot_fn = getattr(entitlement_service, "snapshot", None)
            feature_flags = dict(snapshot_fn() or {}) if callable(snapshot_fn) else {}

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
                    "user": _user_summary(auth_user if authenticated else None),
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
                "environment": {
                    "edition": client_edition,
                    "mode": installation_mode(),
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
