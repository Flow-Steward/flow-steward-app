"""The executable projection of a secret consumer and its fingerprint (ADR 0055).

The projection is everything that decides where a consumer's credentials go and
which code receives them. An approval names one fingerprint of it; any change to the
projection needs a new approval. This module is the only implementation: the runtime
check, the approval service and the upgrade phase all call it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from core.application.canonical_json import canonical_json_hash

PROJECTION_VERSION = "connection.v1"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalized_origin(url: Any) -> dict[str, Any] | None:
    """Scheme, lowercase IDNA host and effective port of a URL, or None when it has none."""
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    return {"scheme": scheme, "host": host, "port": port or _DEFAULT_PORTS[scheme]}


def connection_destinations(connection: dict[str, Any]) -> list[dict[str, Any]]:
    """Destinations the host itself sends a connection's credentials to.

    Only a remote runtime has one the host controls. A locally run extension
    receives the values and its approved release is the boundary.
    """
    if str(connection.get("runtime_mode") or "").strip() != "remote_https":
        return []
    origin = normalized_origin(connection.get("remote_base_url"))
    return [{"kind": "exact_host", **origin}] if origin else []


#: OAuth grant state the OAuth lifecycle rewrites on every connect, refresh and
#: disconnect. It describes a token, not where credentials go: the provider's
#: endpoints and client credentials come from the extension release, which the
#: projection pins. Any other key of `connection_config.oauth` stays in the projection.
OAUTH_OPERATIONAL_FIELDS = frozenset(
    {
        "access_token_secret_name",
        "authorization_intent_id",
        "connected_at",
        "disconnect_operation_id",
        "disconnect_started_at",
        "expires_at",
        "granted_scopes",
        "last_error_code",
        "last_refreshed_at",
        "lifecycle_status",
        "post_connect_pending",
        "post_connect_retry",
        "provider_subject",
        "reconnect_reason",
        "refresh_expires_at",
        "safe_metadata",
        "subject",
        "token_type",
        "verified_absent",
        "verified_display_name",
        "verified_email",
    }
)


def _object(value: Any) -> Any:
    return value if isinstance(value, (dict, list)) else ({} if value is None else value)


def _executable_config(value: Any) -> Any:
    config = _object(value)
    if not isinstance(config, dict) or not isinstance(config.get("oauth"), dict):
        return config
    oauth = {
        key: item for key, item in config["oauth"].items() if key not in OAUTH_OPERATIONAL_FIELDS
    }
    executable = {key: item for key, item in config.items() if key != "oauth"}
    if oauth:
        executable["oauth"] = oauth
    return executable


def connection_projection(
    connection: dict[str, Any],
    *,
    bindings: dict[str, str],
    release: dict[str, Any],
) -> dict[str, Any]:
    """Canonical projection of one project connection; never contains a secret value."""
    return {
        "version": PROJECTION_VERSION,
        "connection_id": str(connection.get("connection_id") or "").strip(),
        "project_id": str(connection.get("project_id") or "").strip(),
        "connection_kind": str(connection.get("connection_kind") or "").strip(),
        "extension_id": str(connection.get("extension_id") or "").strip(),
        "extension_install_id": str(connection.get("extension_install_id") or "").strip(),
        "connection_type": str(connection.get("connection_type") or "").strip(),
        "runtime_mode": str(connection.get("runtime_mode") or "").strip(),
        "remote_base_url": str(connection.get("remote_base_url") or "").strip(),
        "connection_config": _executable_config(connection.get("connection_config")),
        "connection_routes": _object(connection.get("connection_routes")),
        "ingress_config": _object(connection.get("ingress_config")),
        "workflow_mappings": _object(connection.get("workflow_mappings")),
        "bindings": {str(field): str(secret_id) for field, secret_id in sorted(bindings.items())},
        "destinations": connection_destinations(connection),
        "release": {
            "release_id": str(release.get("release_id") or "").strip(),
            "descriptor_sha256": str(release.get("descriptor_sha256") or "").strip(),
        },
    }


def projection_fingerprint(projection: dict[str, Any]) -> str:
    return "sha256:" + canonical_json_hash(projection)


def projection_changes(approved: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    """Top-level projection keys that differ from the approved projection, for display."""
    if not isinstance(approved, dict):
        return sorted(current)
    return sorted(
        key for key in set(approved) | set(current) if approved.get(key) != current.get(key)
    )


#: Connection row keys the projection reads; a caller's row is compared on these.
DISPATCH_FIELDS = (
    "project_id",
    "connection_kind",
    "extension_id",
    "extension_install_id",
    "connection_type",
    "runtime_mode",
    "remote_base_url",
    "connection_config",
    "connection_routes",
    "ingress_config",
    "workflow_mappings",
)

__all__ = [
    "DISPATCH_FIELDS",
    "OAUTH_OPERATIONAL_FIELDS",
    "PROJECTION_VERSION",
    "connection_destinations",
    "connection_projection",
    "normalized_origin",
    "projection_changes",
    "projection_fingerprint",
]
