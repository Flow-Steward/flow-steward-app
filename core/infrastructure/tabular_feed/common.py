"""Small shared helpers for tabular feed infrastructure modules."""

from __future__ import annotations

from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def _pl():
    import polars as pl

    return pl


def _envelope(
    *,
    ok: bool,
    result: dict[str, Any] | None = None,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "result": result or {},
        "commands": [],
        "events": [],
        "reload": [],
        "errors": errors or [],
    }


def _error(code: str, message: str, *, path: str = "") -> dict[str, str]:
    return {"code": str(code), "message": str(message), "path": str(path or "")}


def _fail(code: str, message: str, *, path: str = "") -> dict[str, Any]:
    return _envelope(ok=False, errors=[_error(code, message, path=path)])


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _input(action: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(action.get("input"))


def _limit_env(name: str, default: int) -> int:
    raw = _text(_runtime_env.get(name))
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clamped_limit(options: dict[str, Any], option_name: str, env_name: str, default: int) -> int:
    operator_max = _limit_env(env_name, default)
    requested = _positive_int(options.get(option_name))
    return min(operator_max, requested) if requested is not None else operator_max
