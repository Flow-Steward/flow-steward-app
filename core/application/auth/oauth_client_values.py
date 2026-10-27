"""Shared OAuth value codecs, error identities and secret vocabulary."""

from __future__ import annotations

import time
from typing import Any

_MESSAGE_OAUTH_PROVIDER_CREDENTIALS_CHANGED_RETRY_AUTHORIZATION = (
    "OAuth provider credentials changed; retry authorization"
)


_MESSAGE_OAUTH_CONNECTION_NO_LONGER_EXISTS = "OAuth connection no longer exists"


_MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED = "OAuth state has expired or was already used"


OAUTH_CALLBACK_PATH = "/api/integrations/oauth/callback"


OAUTH_STATE_TTL_SECONDS = 600


OAUTH_REFRESH_SKEW_SECONDS = 120


OAUTH_ACCESS_TOKEN_SECRET_DEFAULT = "access_token"  # pragma: allowlist secret


OAUTH_REFRESH_TOKEN_SECRET = "_oauth_refresh_token"  # pragma: allowlist secret


OAUTH_EXPIRES_AT_SECRET = "_oauth_expires_at"  # pragma: allowlist secret


OAUTH_GRANTED_SCOPES_SECRET = "_oauth_granted_scopes"  # pragma: allowlist secret


OAUTH_TOKEN_TYPE_SECRET = "_oauth_token_type"  # pragma: allowlist secret


OAUTH_INTERNAL_SECRET_PREFIX = "_oauth_"  # pragma: allowlist secret


class OAuthConnectionError(ValueError):
    """Raised when an OAuth connection cannot proceed safely."""


class OAuthTokenEndpointError(OAuthConnectionError):
    """Safe structured token endpoint failure without provider response contents."""

    def __init__(self, *, error_code: str, status_code: int) -> None:
        self.error_code = _as_text(error_code).lower() or "token_endpoint_error"
        self.status_code = int(status_code)
        self.callback_context: dict[str, Any] = {}
        super().__init__(f"OAuth token endpoint failed ({self.error_code})")


class _OAuthRefreshStaleError(OAuthConnectionError):
    """Raised when a refresh response no longer matches current connection state."""


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    rows: list[str] = []
    for item in value:
        text = _as_text(item)
        if text and text not in rows:
            rows.append(text)
    return rows


def _now() -> float:
    return time.time()
