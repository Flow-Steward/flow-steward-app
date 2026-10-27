"""Private all-row emptiness query and secret-free running bootstrap metadata."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

BOOTSTRAP_STATE_FILE = "/run/flow-steward-runtime/bootstrap-token-state.json"


def count_bootstrap_users(connection: Any) -> int:
    """Count every row, including disabled/deleted users, independently of auth flags."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM users")
        row = cursor.fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise ValueError("User count is unavailable")
    return row[0]


def bootstrap_user_count(environment: Mapping[str, str]) -> dict[str, int]:
    """Resolve the supplied runtime DB environment and perform the bounded count query."""
    from core.infrastructure.config.connection_strings import resolve_postgres_dsn
    from core.infrastructure.db.psycopg_runtime import connect

    dsn = resolve_postgres_dsn(environment)
    if not dsn:
        raise ValueError("Bootstrap database configuration is unavailable")
    with connect(dsn, connect_timeout=5, options="-c statement_timeout=5000") as connection:
        return {"schema_version": 1, "count": count_bootstrap_users(connection)}


def bootstrap_runtime_status(
    environment: Mapping[str, str], *, state_path: Path | None = None
) -> dict[str, object]:
    """Use the exec process's inherited container environment, never a dotenv load."""

    def flag(name: str, default: bool) -> bool:
        value = environment.get(name)
        if value is None:
            return default
        if value.strip().lower() in {"1", "true", "yes", "on"}:
            return True
        if value.strip().lower() in {"0", "false", "no", "off"}:
            return False
        raise ValueError("Invalid bootstrap flag")

    path = environment.get("FS_AUTH_BOOTSTRAP_STATE_FILE", "")
    if path not in {"", BOOTSTRAP_STATE_FILE}:
        raise ValueError("Unexpected bootstrap state path")
    state: dict[str, object] = {}
    selected = state_path or (Path(path) if path else None)
    if selected is not None and selected.exists():
        if selected.stat().st_size > 4096:
            raise ValueError("Invalid bootstrap state")
        state = json.loads(selected.read_text())
        if not isinstance(state, dict) or set(state) != {"token_sha256", "expires_at"}:
            raise ValueError("Invalid bootstrap state")
    for value in (
        environment.get("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT", ""),
        state.get("expires_at", ""),
    ):
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("Invalid bootstrap expiry")
        if value and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Bootstrap expiry must specify its timezone")
    digest = state.get("token_sha256", "")
    if not isinstance(digest, str) or (digest and not re.fullmatch(r"[a-f0-9]{64}", digest)):
        raise ValueError("Invalid bootstrap state digest")
    token = environment.get("FS_AUTH_BOOTSTRAP_TOKEN", "")
    return {
        "schema_version": 1,
        "local_auth_enabled": flag("FS_LOCAL_AUTH_ENABLED", True),
        "bootstrap_enabled": flag("FS_AUTH_BOOTSTRAP_ENABLED", True),
        "localhost_only": flag("FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY", True),
        "allow_trusted_proxy": flag("FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY", False),
        "token_sha256": hashlib.sha256(token.encode()).hexdigest() if token else "",
        "expires_at": environment.get("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT", ""),
        "state_path": path,
        "state_sha256": state.get("token_sha256", ""),
        "state_expires_at": state.get("expires_at", ""),
    }
