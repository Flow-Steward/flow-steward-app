"""Admin-registered platform OAuth clients for the MCP server (ADR 0061 decision 2).

A platform such as Microsoft 365 Copilot cannot publish a Client ID Metadata
Document; it signs in with a client id an administrator registered beforehand.
A Flow Steward admin creates such a client from a fixed preset: the client id is
an opaque value generated here, the redirect URIs come only from the preset,
the client is public (PKCE S256, no secret), and everything after the client
check is ADR 0047 unchanged.

Registrations live in the account's ``system_settings`` row
``mcp_oauth_platform_clients``. The ``oauth_clients`` row that grants reference
is written on demand from the registration, like a CIMD cache entry, so the
retention cleanup of unused client rows stays harmless.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from core.application.auth import inbound_oauth_repository as repo

PLATFORM_CLIENTS_SETTING_KEY = "mcp_oauth_platform_clients"
CLIENT_ID_PREFIX = "fs-platform-"
_CLIENT_ID_PATTERN = re.compile(r"fs-platform-[a-z0-9]{2,16}-[0-9a-f]{32}")
#: How long the on-demand ``oauth_clients`` row is trusted before it is rewritten.
CLIENT_ROW_LIFETIME = timedelta(days=1)
#: At most this many registrations per account, revoked ones included.
MAX_REGISTRATIONS = 20


@dataclass(frozen=True, slots=True)
class PlatformPreset:
    id: str
    #: Short form used inside the client id.
    slug: str
    name: str
    redirect_uris: tuple[str, ...]
    #: Whether a missing ``resource`` binds to this installation (ADR 0061 decision 3).
    implies_resource: bool


PLATFORM_PRESETS: Mapping[str, PlatformPreset] = {
    "microsoft_365": PlatformPreset(
        id="microsoft_365",
        slug="m365",
        name="Microsoft 365 Copilot",
        # The one redirect URI Microsoft documents for every plugin's OAuth
        # registration (Teams Developer Portal).
        redirect_uris=("https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect",),
        implies_resource=True,
    ),
}


class PlatformClientError(ValueError):
    """A registration request that cannot be honoured; ``code`` names why."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def is_platform_client_id(client_id: str) -> bool:
    return bool(_CLIENT_ID_PATTERN.fullmatch(client_id or ""))


def new_platform_client_id(preset: PlatformPreset) -> str:
    return f"{CLIENT_ID_PREFIX}{preset.slug}-{secrets.token_hex(16)}"


def _decode(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _registrations(value: Any) -> list[dict[str, Any]]:
    rows = _decode(value).get("clients")
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def load_registrations(conn: Any, account_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT setting_value_jsonb FROM system_settings "
            "WHERE account_id = %s AND setting_key = %s",
            (account_id, PLATFORM_CLIENTS_SETTING_KEY),
        )
        row = repo._row(cur)
    return _registrations((row or {}).get("setting_value_jsonb"))


def _save_registrations(
    conn: Any, account_id: str, rows: list[dict[str, Any]], updated_by: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_settings (account_id, setting_key, setting_value_jsonb, updated_by)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (account_id, setting_key) DO UPDATE
            SET setting_value_jsonb = EXCLUDED.setting_value_jsonb,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (account_id, PLATFORM_CLIENTS_SETTING_KEY, json.dumps({"clients": rows}), updated_by),
        )


def find_registration(conn: Any, client_id: str) -> dict[str, Any] | None:
    """The registration of ``client_id`` in any account, with its ``account_id``."""
    if not is_platform_client_id(client_id):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, setting_value_jsonb FROM system_settings WHERE setting_key = %s",
            (PLATFORM_CLIENTS_SETTING_KEY,),
        )
        rows = repo._rows(cur)
    for row in rows:
        for registration in _registrations(row.get("setting_value_jsonb")):
            if registration.get("client_id") == client_id:
                return {**registration, "account_id": str(row.get("account_id") or "")}
    return None


def active_registration(conn: Any, client_id: str) -> dict[str, Any] | None:
    """A registration that exists, is not revoked and names a known preset."""
    registration = find_registration(conn, client_id)
    if not registration or registration.get("revoked_at"):
        return None
    if str(registration.get("preset") or "") not in PLATFORM_PRESETS:
        return None
    return registration


def preset_of(registration: Mapping[str, Any]) -> PlatformPreset:
    return PLATFORM_PRESETS[str(registration["preset"])]


def ensure_client_row(conn: Any, registration: Mapping[str, Any], now: datetime) -> list[str]:
    """Write the ``oauth_clients`` row grants reference; return the redirect URIs."""
    preset = preset_of(registration)
    digest = hashlib.sha256(f"platform:{preset.id}".encode()).hexdigest()
    repo.upsert_client(
        conn,
        client_id=str(registration["client_id"]),
        client_name=preset.name,
        redirect_uris=list(preset.redirect_uris),
        metadata_sha256=digest,
        fetched_at=now,
        cache_expires_at=now + CLIENT_ROW_LIFETIME,
    )
    return list(preset.redirect_uris)


def register(
    conn: Any, account_id: str, preset_id: str, *, created_by: str, now: datetime
) -> dict[str, Any]:
    preset = PLATFORM_PRESETS.get(preset_id)
    if preset is None:
        raise PlatformClientError("unknown_preset")
    rows = load_registrations(conn, account_id)
    if len(rows) >= MAX_REGISTRATIONS:
        raise PlatformClientError("too_many_platform_clients", 409)
    registration = {
        "client_id": new_platform_client_id(preset),
        "preset": preset.id,
        "created_at": now.isoformat(),
        "created_by": created_by,
        "revoked_at": None,
    }
    _save_registrations(conn, account_id, [*rows, registration], created_by)
    return registration


def revoke(conn: Any, account_id: str, client_id: str, *, revoked_by: str, now: datetime) -> int:
    """Revoke a registration and every live grant it holds; return the grant count."""
    rows = load_registrations(conn, account_id)
    target = next((row for row in rows if row.get("client_id") == client_id), None)
    if target is None:
        raise PlatformClientError("platform_client_not_found", 404)
    if not target.get("revoked_at"):
        target["revoked_at"] = now.isoformat()
        _save_registrations(conn, account_id, rows, revoked_by)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_grants SET revoked_at = %s, revoked_reason = %s "
            "WHERE client_id = %s AND revoked_at IS NULL",
            (now, "platform_client_revoked", client_id),
        )
        return int(cur.rowcount or 0)


__all__ = [
    "CLIENT_ID_PREFIX",
    "PLATFORM_CLIENTS_SETTING_KEY",
    "PLATFORM_PRESETS",
    "PlatformClientError",
    "PlatformPreset",
    "active_registration",
    "ensure_client_row",
    "find_registration",
    "is_platform_client_id",
    "load_registrations",
    "new_platform_client_id",
    "preset_of",
    "register",
    "revoke",
]
