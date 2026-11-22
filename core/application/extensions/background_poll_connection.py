"""Resolve one connection for a ``connection_poll`` background call.

A background action that declares ``connection_type_ids`` is called only for
connections of those types, and each call receives its connection resolved the
way operations resolve it (``target.connection``): an app-only type with its own
connection secrets, an OAuth type with a resolved, refreshed access token. A
connection that cannot be resolved is skipped before any extension call:

- missing connection secrets: skipped with a visible status on the connection;
- revoked or invalid grant: the OAuth runtime marks the connection
  ``authorization_required``, and it is not polled again until reconnected;
- a temporary refresh failure: only this poll is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.application.extensions.connection_partition_validation import (
    ConnectionPartitionSchemaRejected,
    connection_partition_schema,
)

#: OAuth lifecycle states in which a connection is not polled at all.
_NOT_POLLED_OAUTH_STATES = frozenset(
    {"authorization_required", "needs_attention", "disconnecting", "post_connect_pending"}
)
_PERMANENT_OAUTH_DENIALS = ("oauth_authorization_required", "oauth_needs_attention")
_TEMPORARY_OAUTH_DENIALS = ("oauth_provider_unavailable",)

SKIP_SECRETS_MISSING = "connection_secrets_missing"
SKIP_AUTHORIZATION_REQUIRED = "authorization_required"
SKIP_REFRESH_UNAVAILABLE = "oauth_refresh_unavailable"
SKIP_RESOLUTION_FAILED = "connection_resolution_failed"
#: The connection takes its credentials from an OAuth configuration that is gone
#: or incomplete (``credentials_from``).
SKIP_CREDENTIALS_UNAVAILABLE = "credentials_unavailable"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def ingress_defaults(extension: dict[str, Any] | None, connection_type: str) -> dict[str, Any]:
    """The ``default`` of every property the connection type's ingress schema declares."""
    try:
        schema = connection_partition_schema(
            _as_dict(extension), connection_type=connection_type, partition="ingress"
        )
    except ConnectionPartitionSchemaRejected:
        return {}
    defaults: dict[str, Any] = {}
    for name, spec in _as_dict(schema.get("properties")).items():
        if isinstance(spec, dict) and "default" in spec and _text(name):
            defaults[_text(name)] = spec["default"]
    return defaults


def with_ingress_defaults(
    extension: dict[str, Any] | None, connection_type: str, ingress_config: Any
) -> dict[str, Any]:
    """``ingress_config`` with every declared default filled in where absent."""
    ingress = _as_dict(ingress_config)
    for name, value in ingress_defaults(extension, connection_type).items():
        ingress.setdefault(name, value)
    return ingress


def required_secret_keys(extension: dict[str, Any] | None, connection_type: str) -> list[str]:
    try:
        schema = connection_partition_schema(
            _as_dict(extension), connection_type=connection_type, partition="secret"
        )
    except ConnectionPartitionSchemaRejected:
        return []
    required = schema.get("required")
    return [_text(item) for item in required if _text(item)] if isinstance(required, list) else []


@dataclass(frozen=True)
class PollConnectionResolution:
    connection: dict[str, Any] = field(default_factory=dict)
    skip_reason: str = ""
    #: True when the connection stays unpolled until someone fixes it.
    lasting: bool = False

    @property
    def ok(self) -> bool:
        return not self.skip_reason


def _is_oauth(row: dict[str, Any]) -> bool:
    return bool(_text(row.get("oauth_configuration_id")) and _text(row.get("oauth_provider_id")))


def resolve_background_poll_connection(
    bl: Any,
    *,
    connection_row: dict[str, Any],
    extension: dict[str, Any],
    extension_id: str,
    extension_install_id: str,
    account_id: str,
    project_id: str,
) -> PollConnectionResolution:
    """``target.connection`` for one background poll, or why it is skipped."""
    from core.application.extensions.operation_targets import _direct_action_connection_target
    from core.application.management.errors import ServiceError

    row = _as_dict(connection_row)
    connection_id = _text(row.get("connection_id"))
    connection_type = _text(row.get("connection_type"))
    oauth = _is_oauth(row)
    if oauth:
        lifecycle = _text(
            _as_dict(_as_dict(row.get("connection_config")).get("oauth")).get("lifecycle_status")
        )
        if lifecycle in _NOT_POLLED_OAUTH_STATES:
            return PollConnectionResolution(skip_reason=SKIP_AUTHORIZATION_REQUIRED, lasting=True)
    try:
        resolved = _direct_action_connection_target(
            bl,
            connection_id=connection_id,
            account_id=account_id,
            project_id=project_id,
            extension_id=extension_id,
            extension_install_id=extension_install_id,
        )
    except ServiceError as exc:
        detail = str(exc)
        if "credentials_unavailable" in detail:
            return PollConnectionResolution(skip_reason=SKIP_CREDENTIALS_UNAVAILABLE, lasting=True)
        if any(code in detail for code in _PERMANENT_OAUTH_DENIALS):
            return PollConnectionResolution(skip_reason=SKIP_AUTHORIZATION_REQUIRED, lasting=True)
        if any(code in detail for code in _TEMPORARY_OAUTH_DENIALS):
            return PollConnectionResolution(skip_reason=SKIP_REFRESH_UNAVAILABLE)
        return PollConnectionResolution(skip_reason=SKIP_RESOLUTION_FAILED, lasting=True)
    connection = dict(resolved)
    if not oauth:
        secrets = _as_dict(connection.get("secrets"))
        missing = [
            key
            for key in required_secret_keys(extension, connection_type)
            if not _text(secrets.get(key))
        ]
        if missing:
            return PollConnectionResolution(skip_reason=SKIP_SECRETS_MISSING, lasting=True)
    # Re-read with the resolution: the poll starts from the row as it is now.
    current = _as_dict(
        bl.get_project_connection(
            project_id=project_id,
            connection_id=connection_id,
            extension_id=extension_id,
            enabled_only=True,
        )
    )
    if not current:
        return PollConnectionResolution(skip_reason=SKIP_RESOLUTION_FAILED)
    connection["ingress_config"] = with_ingress_defaults(
        extension, connection_type, current.get("ingress_config")
    )
    connection["state_version"] = int(current.get("state_version") or 0)
    connection["config_generation"] = int(current.get("config_generation") or 1)
    return PollConnectionResolution(connection=connection)


def record_background_poll_skip(
    bl: Any, *, connection_id: str, action_id: str, reason: str, at: str
) -> None:
    """Show why a connection is not polled, in its ``ingress_config``."""
    import json

    status = {"action_id": action_id, "status": "skipped", "reason": reason, "at": at}
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE project_connections
               SET ingress_config = jsonb_set(
                       COALESCE(ingress_config, '{}'::jsonb),
                       '{background_poll_status}', %s::jsonb, true)
             WHERE connection_id = %s
               AND (ingress_config -> 'background_poll_status' ->> 'reason') IS DISTINCT FROM %s
            """,
            (json.dumps(status), connection_id, reason),
        )


def clear_background_poll_skip(bl: Any, *, connection_id: str) -> None:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE project_connections
               SET ingress_config = ingress_config - 'background_poll_status'
             WHERE connection_id = %s AND ingress_config ? 'background_poll_status'
            """,
            (connection_id,),
        )


__all__ = [
    "SKIP_AUTHORIZATION_REQUIRED",
    "SKIP_CREDENTIALS_UNAVAILABLE",
    "SKIP_REFRESH_UNAVAILABLE",
    "SKIP_RESOLUTION_FAILED",
    "SKIP_SECRETS_MISSING",
    "PollConnectionResolution",
    "clear_background_poll_skip",
    "ingress_defaults",
    "record_background_poll_skip",
    "required_secret_keys",
    "resolve_background_poll_connection",
    "with_ingress_defaults",
]
