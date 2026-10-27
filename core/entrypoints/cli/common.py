"""Shared helpers for the management CLI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passphrase|private[_-]?key|dsn)",
    re.IGNORECASE,
)


class CLIError(RuntimeError):
    """Domain error for user-facing CLI failures."""

    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(str(message))
        self.exit_code = int(exit_code)


@dataclass(slots=True)
class CLIContext:
    """Execution context shared by all commands."""

    logger: Any
    json_output: bool = False


def resolve_dsn(explicit_dsn: str = "") -> str:
    """Resolve DSN from explicit value, env, or core config."""
    try:
        from core.infrastructure.config import (
            runtime_config as core_config,  # lazy — avoids dotenv at import time
        )

        _core_cfg = core_config.CONFIG
    except Exception:
        _core_cfg = {}
    candidates = (
        explicit_dsn,
        _runtime_env.get("FS_OBSERVABILITY_DSN", ""),
        _runtime_env.get("FS_DB_DSN", ""),
        _runtime_env.get("DATABASE_URL", ""),
        _runtime_env.get("POSTGRES_DSN", ""),
        str(_core_cfg.get("fs_observability_dsn", "") or ""),
        str(_core_cfg.get("fs_db_dsn", "") or ""),
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def dict_cursor(bl: Any):
    """Return a dict-like cursor when psycopg_runtime extras are available."""
    try:
        from core.infrastructure.runtime.entrypoint_db_adapters import dict_cursor_kwargs

        return bl.conn.cursor(**dict_cursor_kwargs())
    except Exception:
        return bl.conn.cursor()


def parse_json_dict(raw: str | None, field_name: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CLIError(f"{field_name} must be valid JSON object: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise CLIError(f"{field_name} must be a JSON object")
    return parsed


def split_csv(raw: str | None) -> list[str]:
    if raw is None:
        return []
    parts = [str(item).strip() for item in str(raw).split(",")]
    return [item for item in parts if item]


def resolve_user(bl: Any, *, user_id: str | None = None, email: str = "") -> dict[str, Any]:
    from core.application.auth.resolve import get_user_by_id, get_user_by_username

    if user_id is not None:
        payload = get_user_by_id(bl, user_id)
        if payload:
            return dict(payload)
        raise CLIError(f"User id={user_id} not found", exit_code=3)

    normalized_email = str(email or "").strip()
    if not normalized_email:
        raise CLIError("Either --user-id or --email is required", exit_code=2)
    payload = get_user_by_username(bl, normalized_email)
    if payload:
        return dict(payload)
    raise CLIError(f"User '{normalized_email}' not found", exit_code=3)


def resolve_project(
    bl: Any,
    *,
    project_id: str | None = None,
    project_name: str | None = None,
    include_trashed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a project row plus its runtime config from id or name."""
    project: dict[str, Any] | None = None

    if project_id is not None:
        if hasattr(bl, "get_project"):
            project = bl.get_project(str(project_id), include_trashed=include_trashed)
        if not project and hasattr(bl, "get_projects"):
            rows = bl.get_projects(include_trashed=include_trashed) or []
            project = next(
                (
                    dict(row)
                    for row in rows
                    if isinstance(row, dict)
                    and str(row.get("id") or "").strip() == str(project_id).strip()
                ),
                None,
            )
        if not project:
            raise CLIError(f"Project id={project_id} not found", exit_code=3)
    else:
        normalized_name = str(project_name or "").strip()
        if not normalized_name:
            raise CLIError("Either --project-id or --project-name is required", exit_code=2)
        rows: list[dict[str, Any]] = []
        if hasattr(bl, "list_projects"):
            try:
                listing = (
                    bl.list_projects(
                        limit=500,
                        offset=0,
                        search=normalized_name,
                        include_trashed=include_trashed,
                    )
                    or []
                )
                rows = [dict(row) for row in listing if isinstance(row, dict)]
            except TypeError:
                rows = []
        if not rows and hasattr(bl, "get_projects"):
            listing = bl.get_projects(include_trashed=include_trashed) or []
            rows = [dict(row) for row in listing if isinstance(row, dict)]
        exact = [
            row
            for row in rows
            if str(row.get("name") or "").strip().lower() == normalized_name.lower()
        ]
        if not exact:
            raise CLIError(f"Project '{normalized_name}' not found", exit_code=3)
        if len(exact) > 1:
            ids = ", ".join(str(row.get("id")) for row in exact)
            raise CLIError(
                f"Project name '{normalized_name}' is ambiguous, use --project-id ({ids})",
                exit_code=2,
            )
        project = exact[0]

    resolved_project = dict(project or {})
    resolved_project_id = str(resolved_project.get("id") or "").strip()
    run_config = {}
    if resolved_project_id and hasattr(bl, "get_project_run_config"):
        loaded = bl.get_project_run_config(resolved_project_id) or {}
        if isinstance(loaded, dict):
            run_config = dict(loaded)
    return resolved_project, run_config


def redact_project_config_snapshot(project_config: dict | None) -> dict[str, Any]:
    if not isinstance(project_config, dict):
        return {}
    try:
        parsed = json.loads(json.dumps(project_config, default=str))
    except Exception:
        return {}
    redacted = _redact_value(parsed)
    return redacted if isinstance(redacted, dict) else {}


def cli_json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def cli_json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, default=cli_json_default)


def render_cli_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return cli_json_dumps(value)
    if isinstance(value, (datetime, date, time)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        payload: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key or "")
            if _SENSITIVE_KEY_RE.search(key_text):
                payload[key_text] = "[REDACTED]"
                continue
            payload[key_text] = _redact_value(item)
        return payload
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def invoke_service(handler, *args, **kwargs):
    try:
        return handler(*args, **kwargs)
    except Exception as exc:
        _raise_service_error_as_cli(exc)


def invoke_async_service(awaitable):
    import asyncio

    try:
        return asyncio.run(awaitable)
    except Exception as exc:
        _raise_service_error_as_cli(exc)


def _raise_service_error_as_cli(exc: Exception) -> None:
    try:
        from core.application.management import ServiceError
    except Exception:
        raise exc from None

    if not isinstance(exc, ServiceError):
        raise exc
    status_code = int(getattr(exc, "status_code", 500) or 500)
    if status_code >= 500:
        exit_code = 4
    elif status_code == 404:
        exit_code = 3
    else:
        exit_code = 2
    raise CLIError(str(exc), exit_code=exit_code) from exc
