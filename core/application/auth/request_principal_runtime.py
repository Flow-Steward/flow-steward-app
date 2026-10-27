"""Request principal resolution, roles and authentication context."""

from __future__ import annotations

from typing import Any


def roles_intersect(actual: list[str], required: set[str]) -> bool:
    if not required:
        return True
    normalized_actual = {str(role or "").strip().lower() for role in (actual or [])}
    return bool(normalized_actual.intersection(required))


def normalize_live_principal(
    raw_user: dict | Any | None,
    *,
    default_auth_source: str,
    normalize_principal_fn,
    resolved_principal_type,
):
    if isinstance(raw_user, resolved_principal_type):
        return raw_user
    if not isinstance(raw_user, dict):
        return None
    payload = dict(raw_user)
    if default_auth_source and not str(payload.get("auth_source") or "").strip():
        payload["auth_source"] = default_auth_source
    return normalize_principal_fn(payload)


def request_auth_user_payload(request, *, resolved_principal_type) -> dict | None:
    if request is None:
        return None
    user = getattr(request.state, "auth_user", None)
    if isinstance(user, dict):
        return user
    principal = getattr(request.state, "auth_principal", None)
    if isinstance(principal, resolved_principal_type):
        return principal.model_dump()
    return None


def request_principal(
    request,
    *,
    normalize_principal_fn,
    resolved_principal_type,
):
    if request is None:
        return None
    principal = getattr(request.state, "auth_principal", None)
    if isinstance(principal, resolved_principal_type):
        return principal
    user = getattr(request.state, "auth_user", None)
    return normalize_principal_fn(user) if isinstance(user, dict) else None


def require_authenticated_user(
    request,
    *,
    required_roles: set[str] | None = None,
    request_auth_user_payload_fn,
    roles_intersect_fn,
    err_authentication_required: str,
) -> dict:
    user = request_auth_user_payload_fn(request)
    if not isinstance(user, dict):
        raise PermissionError(err_authentication_required)
    required = {
        str(role).strip().lower() for role in (required_roles or set()) if str(role).strip()
    }
    if required and not roles_intersect_fn(user.get("roles") or [], required):
        raise PermissionError("Insufficient role")
    return user


def optional_actor_username(
    request,
    *,
    default: str = "operator",
    request_auth_user_payload_fn,
) -> str:
    user = request_auth_user_payload_fn(request)
    if isinstance(user, dict):
        username = str(user.get("username") or "").strip()
        if username:
            return username
    return default


def resolve_actor_username(
    request,
    explicit_actor: str | None,
    *,
    default: str = "operator",
    request_auth_user_payload_fn,
    optional_actor_username_fn,
) -> str:
    if request is not None:
        user = request_auth_user_payload_fn(request)
        if isinstance(user, dict):
            username = str(user.get("username") or "").strip()
            if username:
                return username
    actor_text = str(explicit_actor or "").strip()
    if actor_text:
        return actor_text
    if request is not None:
        return optional_actor_username_fn(request, default=default)
    return default


def build_request_scope_resolver(
    *,
    request_auth_user_payload_fn,
    optional_actor_username_fn,
    scope_from_principal_fn,
):
    def _resolver(
        request,
        *,
        project_id: str | None = None,
        workspace_id: str | None = None,
        workflow_id: str | None = None,
        actor_id: str | None = None,
        default_actor: str = "operator",
    ):
        user = request_auth_user_payload_fn(request)
        resolved_actor_id = actor_id
        if resolved_actor_id is None and request is not None:
            resolved_actor_id = optional_actor_username_fn(request, default=default_actor)
        return scope_from_principal_fn(
            user if isinstance(user, dict) else None,
            project_id=project_id,
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            actor_id=resolved_actor_id,
        )

    return _resolver


def request_auth_token(request, *, auth_cookie_name: str) -> str:
    auth_header = str(request.headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    return str(request.cookies.get(auth_cookie_name) or "").strip()


def normalize_requested_roles(raw_roles: list[str] | None) -> list[str]:
    allowed_roles = {"admin", "operator", "viewer"}
    deduped: list[str] = []
    for role in raw_roles or []:
        normalized = str(role or "").strip().lower()
        if normalized and normalized in allowed_roles and normalized not in deduped:
            deduped.append(normalized)
    return deduped


__all__ = [
    "build_request_scope_resolver",
    "normalize_live_principal",
    "normalize_requested_roles",
    "optional_actor_username",
    "request_auth_token",
    "request_auth_user_payload",
    "request_principal",
    "require_authenticated_user",
    "resolve_actor_username",
    "roles_intersect",
]
