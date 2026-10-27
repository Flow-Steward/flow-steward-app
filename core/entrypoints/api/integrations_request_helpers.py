"""HTTP request parsing and scoped connector views."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Request

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.extensions.oauth_contract import (
    OAUTH_ERROR_CODES,
    OAUTH_ID_MAX_LENGTH,
    OAUTH_ID_RE,
)
from core.application.management import ServiceValidationError

_MESSAGE_CONNECTION_ID_IS_NOT_VALID_FOR_THIS_PROJECT = "connection_id is not valid for this project"


_MESSAGE_DATABASE_IS_NOT_CONFIGURED = "Database is not configured"


def _request_has_admin_role(request: Request) -> bool:
    auth_user = getattr(request.state, "auth_user", None)
    roles = (
        {str(role or "").strip().lower() for role in (auth_user.get("roles") or [])}
        if isinstance(auth_user, Mapping)
        else set()
    )
    return "admin" in roles


def _valid_project_id(project_id: str) -> bool:
    try:
        uuid.UUID(str(project_id or "").strip())
    except (TypeError, ValueError):
        return False
    return True


_VIEWER_CONNECTION_SUMMARY_FIELDS = (
    "connection_id",
    "project_id",
    "extension_id",
    "display_name",
    "connection_kind",
    "connection_type",
    "environment",
    "extension_install_id",
    "health_status",
    "runtime_status",
    "last_seen_ok_at",
    "enabled",
    "oauth_configuration_id",
    "oauth_provider_id",
)


_VIEWER_OAUTH_LIFECYCLE_STATUSES = {
    "connected",
    "disconnected",
    "pending",
    "needs_attention",
    "authorization_required",
    "post_connect_pending",
    "disconnecting",
    "reconnect_required",
}


def _browser_connection_view(scope: Any, connection: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(connection)
    payload.pop("secrets", None)
    if principal_type_has_minimum_role(getattr(scope, "principal_type", None), "operator"):
        return payload
    projected = {
        field: payload[field] for field in _VIEWER_CONNECTION_SUMMARY_FIELDS if field in payload
    }
    config = payload.get("connection_config")
    config = config if isinstance(config, Mapping) else {}
    oauth = config.get("oauth")
    oauth = oauth if isinstance(oauth, Mapping) else {}
    lifecycle = oauth.get("lifecycle_status")
    lifecycle = lifecycle.strip().lower() if isinstance(lifecycle, str) else ""
    error_code = oauth.get("last_error_code")
    error_code = error_code.strip().lower() if isinstance(error_code, str) else ""
    authorization_intent_id = oauth.get("authorization_intent_id")
    authorization_intent_id = (
        authorization_intent_id.strip() if isinstance(authorization_intent_id, str) else ""
    )
    oauth_summary: dict[str, str] = {}
    if lifecycle in _VIEWER_OAUTH_LIFECYCLE_STATUSES:
        oauth_summary["lifecycle_status"] = lifecycle
    if error_code in OAUTH_ERROR_CODES:
        oauth_summary["last_error_code"] = error_code
    if (
        authorization_intent_id
        and len(authorization_intent_id) <= OAUTH_ID_MAX_LENGTH
        and OAUTH_ID_RE.fullmatch(authorization_intent_id)
    ):
        oauth_summary["authorization_intent_id"] = authorization_intent_id
    if oauth_summary:
        projected["connection_config"] = {"oauth": oauth_summary}
    return projected


def _oauth_redirect_url(
    return_path: str,
    *,
    outcome: str,
    reason: str = "",
    sync_outcome: str = "",
) -> str:
    parts = urlsplit(return_path)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["oauth"] = outcome
    if reason:
        params["reason"] = reason
    else:
        params.pop("reason", None)
    if sync_outcome in {"success", "failed"}:
        params["sync"] = sync_outcome
    else:
        params.pop("sync", None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def _query_csv_values(request: Request, key: str) -> list[str]:
    values: list[str] = []
    for raw in request.query_params.getlist(key):
        text = str(raw or "").strip()
        if not text:
            continue
        values.extend([item.strip() for item in text.split(",") if item.strip()])
    return values


def _parse_optional_bool_query(raw_value: str | None, *, field_name: str) -> bool | None:
    if raw_value in (None, ""):
        return None
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ServiceValidationError(f"Invalid boolean for '{field_name}': {raw_value}")


def _parse_bool_query(raw_value: str | None, *, field_name: str, default: bool = False) -> bool:
    parsed = _parse_optional_bool_query(raw_value, field_name=field_name)
    if parsed is None:
        return bool(default)
    return parsed


def _require_extension_state_connection_owner(
    bl: Any,
    *,
    project_id: str,
    account_id: str,
    extension_id: str,
    connection_id: str | None,
) -> tuple[str | None, str]:
    normalized_connection_id = str(connection_id or "").strip()
    if not normalized_connection_id:
        return None, ""

    row = None
    getter = getattr(bl, "get_project_connection", None)
    if callable(getter):
        try:
            row = getter(
                project_id=project_id,
                connection_id=normalized_connection_id,
                enabled_only=False,
            )
        except Exception:
            row = None
    if row is None and hasattr(bl, "cursor"):
        try:
            from core.infrastructure.runtime.entrypoint_db_adapters import (
                get_project_connection_row,
            )

            row = get_project_connection_row(bl, normalized_connection_id)
        except Exception:
            row = None
    connection = row if isinstance(row, dict) else {}
    if not connection:
        raise ServiceValidationError(_MESSAGE_CONNECTION_ID_IS_NOT_VALID_FOR_THIS_PROJECT)
    if str(connection.get("connection_id") or "").strip() != normalized_connection_id:
        raise ServiceValidationError(_MESSAGE_CONNECTION_ID_IS_NOT_VALID_FOR_THIS_PROJECT)
    if str(connection.get("project_id") or "").strip() != str(project_id):
        raise ServiceValidationError(_MESSAGE_CONNECTION_ID_IS_NOT_VALID_FOR_THIS_PROJECT)
    if str(connection.get("account_id") or "").strip() != str(account_id):
        raise ServiceValidationError("connection_id is not valid for this account")
    connection_extension_id = str(connection.get("extension_id") or "").strip()
    if connection_extension_id != str(extension_id or "").strip():
        raise ServiceValidationError("connection_id does not belong to this extension")
    install_id = str(connection.get("extension_install_id") or "").strip()
    return normalized_connection_id, install_id
