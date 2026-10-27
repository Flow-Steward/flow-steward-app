"""Transaction-owned encrypted token persistence for extension OAuth grants."""

from __future__ import annotations

import json
import time
from typing import Any

from core.infrastructure.db import model_provider_store
from core.infrastructure.security.connection_secret_store import (
    connection_secret_ref_key,
    resolve_connection_secrets,
    save_connection_secrets,
)

_ACCESS_TOKEN = "_oauth_access_token"  # pragma: allowlist secret
_REFRESH_TOKEN = "_oauth_refresh_token"  # pragma: allowlist secret
_EXPIRES_AT = "_oauth_expires_at"  # pragma: allowlist secret
#: When the refresh token itself stops working. Kept beside the tokens in the
#: same encrypted store rather than in a column of its own, because it is
#: part of the grant and rotates atomically with it.
_REFRESH_EXPIRES_AT = "_oauth_refresh_expires_at"  # pragma: allowlist secret
_GRANTED_SCOPES = "_oauth_granted_scopes"  # pragma: allowlist secret
_TOKEN_TYPE = "_oauth_token_type"  # pragma: allowlist secret


class DatabaseOAuthCallbackTokenStore:
    def __init__(self, conn: Any, *, now: Any = time.time) -> None:
        self._conn = conn
        self._now = now

    def resolve_refresh_token(self, *, account_id: str, secret_refs: Any) -> str:
        values = resolve_connection_secrets(
            self._conn,
            account_id=account_id,
            secret_refs=secret_refs,
        )
        return str(values.get(_REFRESH_TOKEN) or "")

    def resolve_access_token(self, *, account_id: str, secret_refs: Any) -> str:
        values = resolve_connection_secrets(
            self._conn,
            account_id=account_id,
            secret_refs=secret_refs,
        )
        return str(values.get(_ACCESS_TOKEN) or "")

    def resolve_token_snapshot(self, *, account_id: str, secret_refs: Any) -> dict[str, Any]:
        values = resolve_connection_secrets(
            self._conn,
            account_id=account_id,
            secret_refs=secret_refs,
        )

        def _moment(key: str) -> float | None:
            raw = str(values.get(key) or "").strip()
            try:
                return float(raw) if raw else None
            except ValueError:
                return None

        return {
            "access_token": str(values.get(_ACCESS_TOKEN) or ""),
            "refresh_token": str(values.get(_REFRESH_TOKEN) or ""),
            "expires_at": _moment(_EXPIRES_AT),
            "refresh_expires_at": _moment(_REFRESH_EXPIRES_AT),
        }

    def write_tokens(
        self,
        *,
        account_id: str,
        connection_id: str,
        token_response: dict[str, Any],
        existing_refresh_token: str,
    ) -> dict[str, Any]:
        access_token = str(token_response.get("access_token") or "")
        token_type = str(token_response.get("token_type") or "")
        if not access_token or token_type.lower() != "bearer":
            raise ValueError("invalid OAuth token payload")
        refresh_token = str(token_response.get("refresh_token") or "") or str(
            existing_refresh_token or ""
        )
        scope = str(token_response.get("scope") or "")
        granted_scopes = sorted(dict.fromkeys(scope.split())) if scope else []
        secrets: dict[str, str] = {
            _ACCESS_TOKEN: access_token,
            _TOKEN_TYPE: "Bearer",
            _GRANTED_SCOPES: json.dumps(granted_scopes, separators=(",", ":")),
        }
        expires_in = token_response.get("expires_in")
        if isinstance(expires_in, int) and not isinstance(expires_in, bool):
            secrets[_EXPIRES_AT] = str(float(self._now()) + expires_in)
        else:
            # An unknown lifetime must not inherit the previous access token's
            # expiry. Delete the deterministic ref in the same transaction.
            model_provider_store.delete_secret_ref(
                self._conn,
                account_id=account_id,
                ref_key=connection_secret_ref_key(
                    connection_id,
                    _EXPIRES_AT,
                ),
            )
        if refresh_token:
            secrets[_REFRESH_TOKEN] = refresh_token
        refresh_expires_in = token_response.get("refresh_token_expires_in")
        if isinstance(refresh_expires_in, int) and not isinstance(refresh_expires_in, bool):
            secrets[_REFRESH_EXPIRES_AT] = str(float(self._now()) + refresh_expires_in)
        else:
            # The same rule as the access token: an unknown lifetime must not
            # inherit the previous one, or a dead grant would look alive.
            model_provider_store.delete_secret_ref(
                self._conn,
                account_id=account_id,
                ref_key=connection_secret_ref_key(connection_id, _REFRESH_EXPIRES_AT),
            )
        return save_connection_secrets(
            self._conn,
            account_id=account_id,
            connection_id=connection_id,
            secrets=secrets,
        )


__all__ = ["DatabaseOAuthCallbackTokenStore"]
