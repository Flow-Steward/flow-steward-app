from __future__ import annotations


def _helpers():
    from core.infrastructure.db import platform_logger as _platform_logger

    return _platform_logger


def _as_dict(value):
    return _helpers()._as_dict(value)


def _coerce_bool(value, default: bool = False) -> bool:
    return _helpers()._coerce_bool(value, default=default)


def _coerce_int(value, default: int) -> int:
    return _helpers()._coerce_int(value, default)


def _coerce_float(value, default: float) -> float:
    return _helpers()._coerce_float(value, default)


def _merge_string_fields(*sources: dict) -> dict:
    return _helpers()._merge_string_fields(*sources)


def _normalize_policy_enum(
    value,
    *,
    default: str,
    allowed: set[str],
    strict: bool,
    field_name: str,
    aliases: dict[str, str] | None = None,
) -> str:
    return _helpers()._normalize_policy_enum(
        value,
        default=default,
        allowed=allowed,
        strict=strict,
        field_name=field_name,
        aliases=aliases,
    )
