from __future__ import annotations

from typing import Any


def _error(
    errors: list[dict[str, str]],
    *,
    path: str,
    message: str,
    code: str = "invalid_contract",
) -> None:
    errors.append(
        {
            "path": str(path or "").strip(),
            "message": str(message or "").strip(),
            "code": str(code or "invalid_contract").strip(),
        }
    )


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_lower(value: Any) -> str:
    return _as_text(value).lower()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _as_text(value).lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return bool(value)


def _positive_int(value: Any) -> int | None:
    text = _as_text(value)
    if not text:
        return None
    try:
        parsed = int(text)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return parsed
