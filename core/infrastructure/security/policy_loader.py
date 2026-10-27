"""SEC-002: Policy loader — read default YAML, merge global + project overrides."""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.security.policy import (
    MINIMAL_OVERRIDES,
    STRICT_OVERRIDES,
    SecurityPolicy,
)

_DEFAULT_POLICY_PATH = Path(__file__).parent / "default_policy.yaml"
_cached_default: SecurityPolicy | None = None
_loader_logger = logging.getLogger(__name__)


def load_default_policy() -> SecurityPolicy:
    """Load the shipped default policy from YAML (cached after first call)."""
    global _cached_default
    if _cached_default is not None:
        return _cached_default.model_copy(deep=True)

    if _DEFAULT_POLICY_PATH.is_file():
        raw = yaml.safe_load(_DEFAULT_POLICY_PATH.read_text()) or {}
        _cached_default = SecurityPolicy(**raw)
    else:
        _cached_default = SecurityPolicy()
    return _cached_default.model_copy(deep=True)


def _policy_storage_dsn() -> str:
    return (
        _runtime_env.get("FS_OBSERVABILITY_DSN")
        or _runtime_env.get("FS_DB_DSN")
        or _runtime_env.get("DATABASE_URL")
        or _runtime_env.get("POSTGRES_DSN")
        or ""
    ).strip()


def _coerce_json_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _get_threaded_pool(dsn: str):
    """Load db pool lazily so callers/tests don't require psycopg_runtime at import time."""
    from core.infrastructure.db.db_pool import get_threaded_pool, pool_bounds

    minconn, maxconn = pool_bounds()
    return get_threaded_pool(dsn, minconn=minconn, maxconn=maxconn)


def _return_read_connection(pool: Any, conn: Any) -> None:
    """Return a policy-read connection to the pool in an idle state."""
    closed = getattr(conn, "closed", 0)
    close = isinstance(closed, (bool, int)) and bool(closed)
    if not close:
        try:
            # PostgreSQL opens a transaction even for SELECT when autocommit is
            # disabled. Never let that transaction leak to the next borrower.
            conn.rollback()
        except Exception:
            close = True
    if close:
        pool.putconn(conn, close=True)
    else:
        pool.putconn(conn)


def load_global_policy_override(account_id: Any = None) -> dict[str, Any] | None:
    """Best-effort load of the account-global policy override from system settings."""
    dsn = _policy_storage_dsn()
    if not dsn:
        return None
    try:
        from core.infrastructure.db.system_settings_store import (
            SECURITY_POLICY_SETTINGS_KEY,
            get_system_setting,
        )

        pool = _get_threaded_pool(dsn)
        conn = pool.getconn()
        try:
            row = get_system_setting(
                conn=conn,
                _table_exists=True,
                setting_key=SECURITY_POLICY_SETTINGS_KEY,
                account_id=account_id,
            )
            if not isinstance(row, dict):
                return None
            return _coerce_json_dict(row.get("setting_value_jsonb"))
        finally:
            _return_read_connection(pool, conn)
    except Exception:
        _loader_logger.debug("load_global_policy_override failed", exc_info=True)
        return None


def load_project_policy_override(project_id: Any) -> dict[str, Any] | None:
    """Best-effort load of project security override from DB.

    Primary storage:
      projects.config.security_policy
    """
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return None
    dsn = _policy_storage_dsn()
    if not dsn:
        return None
    try:
        pool = _get_threaded_pool(dsn)
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT config->'security_policy' FROM projects WHERE id = %s",
                    (normalized_project_id,),
                )
                row = cur.fetchone()
                current = _coerce_json_dict(row[0] if row else None)
                if current is not None:
                    return current
                return None
        finally:
            _return_read_connection(pool, conn)
    except Exception:
        _loader_logger.debug(
            "load_project_policy_override failed for project %s",
            project_id,
            exc_info=True,
        )
        return None


def load_project_policy_context(project_id: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Load a project's owning account and security override in one DB round trip."""
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return None, None
    dsn = _policy_storage_dsn()
    if not dsn:
        return None, None
    try:
        pool = _get_threaded_pool(dsn)
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_id, config->'security_policy' FROM projects WHERE id = %s",
                    (normalized_project_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None, None
                account_id = str(row[0] or "").strip() or None
                return account_id, _coerce_json_dict(row[1])
        finally:
            _return_read_connection(pool, conn)
    except Exception:
        _loader_logger.debug(
            "load_project_policy_context failed for project %s",
            project_id,
            exc_info=True,
        )
        return None, None


def load_global_policy(account_id: Any = None) -> SecurityPolicy:
    """Load effective account-global policy: shipped default + DB override."""
    default_policy = load_default_policy()
    global_override = load_global_policy_override(account_id)
    return merge_policies(default_policy, global_override)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into a copy of *base*."""
    result = copy.deepcopy(base)
    for key, val in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def merge_policies(
    global_policy: SecurityPolicy,
    project_override: dict[str, Any] | None = None,
) -> SecurityPolicy:
    """Merge a project-level override dict on top of the global policy.

    The *project_override* is a partial dict from ``projects.config.security_policy``.
    """
    base = global_policy.model_dump()

    # Apply level presets first
    level = (project_override or {}).get("level", base.get("level", "standard"))
    if level == "minimal":
        base = _deep_merge(base, MINIMAL_OVERRIDES)
    elif level == "strict":
        base = _deep_merge(base, STRICT_OVERRIDES)

    # Then apply explicit project overrides on top
    if project_override:
        base = _deep_merge(base, project_override)

    return SecurityPolicy(**base)


def validate_policy(policy: SecurityPolicy) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    errors: list[str] = []

    if policy.level not in ("minimal", "standard", "strict"):
        errors.append(f"Invalid level: {policy.level}")

    for rule in policy.custom_rules:
        if not rule.rule_id:
            errors.append("Custom rule missing rule_id")
        if not rule.pattern:
            errors.append(f"Custom rule {rule.rule_id!r} missing pattern")
            continue
        try:
            re.compile(rule.pattern)
        except re.error as exc:
            errors.append(
                f"Custom rule {rule.rule_id!r} has invalid pattern: {exc.msg} at position {exc.pos}"
            )

    return errors
