"""Shared timezone mechanisms with explicit strict-date and legacy policies."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.infrastructure.date_types import DateValue, DateValueError, validate_date_value

_IANA_LEGACY_ALIASES = {
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
    "US/Alaska": "America/Anchorage",
    "US/Hawaii": "Pacific/Honolulu",
    "US/Arizona": "America/Phoenix",
}


def validate_timezone(name: str | None, *, strict: bool = True) -> str:
    """Canonicalize IANA aliases; strict date actions additionally reject abbreviations."""
    if not isinstance(name, str) or not name.strip():
        raise DateValueError("timezone_required", "Timezone identifier is required.")
    clean = name.strip()
    if strict and clean != "UTC" and re.fullmatch(r"[A-Za-z]+", clean):
        raise DateValueError(
            "abbreviated_timezone",
            f"Timezone abbreviation {clean!r} is ambiguous; use an IANA region name.",
        )
    canonical = _IANA_LEGACY_ALIASES.get(clean, clean)
    try:
        ZoneInfo(canonical)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise DateValueError("unknown_timezone", f"Unknown IANA timezone: {clean}") from exc
    return canonical


def local_candidates(naive: datetime, zone: str) -> tuple[datetime, ...]:
    """Return valid UTC-ordered folds; gaps return no candidates."""
    if naive.tzinfo is not None:
        raise DateValueError("invalid_date", "local_candidates requires a naive local datetime.")
    validate_date_value(naive)
    tz = ZoneInfo(validate_timezone(zone))
    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        try:
            utc = aware.astimezone(UTC)
            roundtrip = utc.astimezone(tz).replace(tzinfo=None)
        except (OverflowError, ValueError) as exc:
            raise DateValueError(
                "invalid_date", "Timezone conversion exceeds the supported calendar range."
            ) from exc
        if roundtrip == naive:
            candidates[utc] = aware
    return tuple(candidates[key] for key in sorted(candidates))


def resolve_datetime(
    value: DateValue,
    timezone: str | None = None,
    dst_policy: str = "error",
    *,
    require_aware: bool = False,
) -> DateValue:
    """Resolve local values explicitly while preserving unzoned calendar values."""
    validate_date_value(value)
    if dst_policy not in {"error", "first"}:
        raise DateValueError("invalid_date", "dst_policy must be error or first.")
    zone = validate_timezone(timezone) if timezone is not None else None
    if isinstance(value, datetime) and value.tzinfo is not None:
        try:
            return value.astimezone(ZoneInfo(zone)) if zone else value
        except (OverflowError, ValueError) as exc:
            raise DateValueError(
                "invalid_date", "Timezone conversion exceeds the supported calendar range."
            ) from exc
    if zone is None:
        if require_aware:
            raise DateValueError(
                "timezone_required", "An offset or an explicit IANA timezone is required."
            )
        return value
    naive = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
    candidates = local_candidates(naive, zone)
    if len(candidates) == 1:
        return candidates[0]
    if dst_policy == "error":
        code = "ambiguous_local_time" if candidates else "nonexistent_local_time"
        raise DateValueError(
            code,
            f"Local time {naive.isoformat()} is {'ambiguous' if candidates else 'nonexistent'} in {zone}; choose dst_policy explicitly.",
        )
    if candidates:
        return candidates[0]
    # fold=0 uses the pre-transition offset; UTC roundtrip advances by the gap.
    try:
        return (
            naive.replace(tzinfo=ZoneInfo(zone), fold=0).astimezone(UTC).astimezone(ZoneInfo(zone))
        )
    except (OverflowError, ValueError) as exc:
        raise DateValueError(
            "invalid_date", "Timezone conversion exceeds the supported calendar range."
        ) from exc


def legacy_to_utc(value: datetime, zone: str) -> datetime:
    """Preserve the settings API's historical attach/convert policy exactly."""
    tz = ZoneInfo(validate_timezone(zone, strict=False))
    local = value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return local.astimezone(UTC)


def legacy_to_local(value: datetime, zone: str) -> datetime:
    """Preserve the settings API's naive-as-UTC conversion behavior."""
    tz = ZoneInfo(validate_timezone(zone, strict=False))
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).astimezone(tz)
