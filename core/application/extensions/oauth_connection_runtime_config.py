"""The non-secret connection settings an extension action is allowed to see."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The Core-owned `oauth` partition fields an extension may read. Everything
#: else in that partition is lifecycle bookkeeping (leases, revisions) that the
#: extension has no business depending on.
RUNTIME_OAUTH_CONFIG_FIELDS = frozenset(
    {
        "authorization_intent_id",
        "last_error_code",
        "lifecycle_status",
        "safe_metadata",
        "subject",
        "token_type",
        "verified_display_name",
        "verified_email",
    }
)


def runtime_connection_config(connection: Mapping[str, Any]) -> dict[str, Any]:
    """Project only closed, non-secret connection metadata to an extension action."""
    raw = connection.get("connection_config")
    config = dict(raw) if isinstance(raw, Mapping) else {}
    raw_oauth = config.get("oauth")
    oauth = dict(raw_oauth) if isinstance(raw_oauth, Mapping) else {}
    return {
        **{key: value for key, value in config.items() if key != "oauth"},
        "oauth": {key: value for key, value in oauth.items() if key in RUNTIME_OAUTH_CONFIG_FIELDS},
    }


__all__ = ["RUNTIME_OAUTH_CONFIG_FIELDS", "runtime_connection_config"]
