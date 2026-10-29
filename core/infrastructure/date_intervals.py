"""Validated canonical UTC intervals and bounded elapsed-time meeting slots."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, TypedDict

from core.infrastructure.date_formatting import canonical_iso
from core.infrastructure.date_timezones import local_candidates, resolve_datetime, validate_timezone
from core.infrastructure.date_types import DateValue, DateValueError

MAX_INTERVAL_SLOTS = 10000


class Interval(TypedDict):
    """Half-open UTC interval, or its explicit empty representation."""

    empty: bool
    start: str | None
    end: str | None


def _empty() -> Interval:
    return {"empty": True, "start": None, "end": None}


def _interval(start: datetime, end: datetime) -> Interval:
    return {"empty": False, "start": canonical_iso(start), "end": canonical_iso(end)}


def _absolute(value: DateValue, timezone: str | None, dst_policy: str) -> datetime:
    if timezone is not None:
        validate_timezone(timezone)
    aware = isinstance(value, datetime) and value.utcoffset() is not None
    resolved = resolve_datetime(value, None if aware else timezone, dst_policy, require_aware=True)
    if not isinstance(resolved, datetime):
        raise DateValueError(
            "timezone_required", "An interval boundary requires a date and an explicit timezone."
        )
    try:
        return resolved.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise DateValueError(
            "invalid_interval", "The interval boundary lies outside the supported date range."
        ) from error


def interval_make(
    start: DateValue, end: DateValue, *, timezone: str | None = None, dst_policy: str = "error"
) -> Interval:
    """Create a nonempty interval from parsed absolute or explicitly zoned values."""
    left, right = _absolute(start, timezone, dst_policy), _absolute(end, timezone, dst_policy)
    if right <= left:
        raise DateValueError("invalid_interval", "The interval end must be later than its start.")
    return _interval(left, right)


def _boundaries(value: Any) -> tuple[datetime, datetime] | None:
    if not isinstance(value, dict) or type(value.get("empty")) is not bool:
        raise DateValueError(
            "invalid_interval", "An interval must contain empty, start and end fields."
        )
    if value["empty"]:
        if (
            "start" not in value
            or "end" not in value
            or value["start"] is not None
            or value["end"] is not None
        ):
            raise DateValueError(
                "invalid_interval", "An empty interval must have null start and end boundaries."
            )
        return None
    parsed = []
    for name in ("start", "end"):
        raw = value.get(name)
        if not isinstance(raw, str) or not raw.endswith("Z"):
            raise DateValueError(
                "invalid_interval",
                f"The interval {name} must be a canonical UTC ISO string ending in Z.",
            )
        try:
            moment = datetime.fromisoformat(raw[:-1] + "+00:00")
            if canonical_iso(moment) != raw:
                raise ValueError("noncanonical boundary")
        except (ValueError, OverflowError) as error:
            raise DateValueError(
                "invalid_interval",
                f"The interval {name} must be a valid canonical UTC ISO boundary.",
            ) from error
        parsed.append(moment)
    if parsed[1] <= parsed[0]:
        raise DateValueError("invalid_interval", "The interval end must be later than its start.")
    return parsed[0], parsed[1]


def interval_duration(value: Any, unit: str = "minutes") -> float:
    """Measure elapsed minutes, hours or days; empty intervals have zero duration."""
    divisors = {"minutes": 60, "hours": 3600, "days": 86400}
    if unit not in divisors:
        raise DateValueError("unsupported_unit", f"Unsupported interval duration unit: {unit!r}.")
    bounds = _boundaries(value)
    return 0 if bounds is None else (bounds[1] - bounds[0]).total_seconds() / divisors[unit]


def interval_intersect_all(values: Any) -> Interval:
    """Intersect every validated interval, preserving the explicit empty shape."""
    if not isinstance(values, list):
        raise DateValueError(
            "invalid_interval", "Intervals to intersect must be supplied as an array."
        )
    if not values:
        raise DateValueError("empty_interval_list", "Supply at least one interval to intersect.")
    start = end = None
    empty = False
    for value in values:
        bounds = _boundaries(value)
        if bounds is None:
            empty = True
        else:
            start = bounds[0] if start is None else max(start, bounds[0])
            end = bounds[1] if end is None else min(end, bounds[1])
    if empty or start is None or end is None or end <= start:
        return _empty()
    return _interval(start, end)


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DateValueError(
            "invalid_amount", f"{name} must be a positive integer, not a boolean or fraction."
        )
    return value


def _first_grid(start: datetime, latest: datetime, align: int, zone: str) -> datetime | None:
    """Find the earliest UTC candidate on daily local-midnight wall grids.

    Search retains only one candidate. The UTC-offset bound allows stopping once
    future local dates cannot precede it; gaps are skipped and both folds count.
    """
    day = max(date(1000, 1, 1), start.date() - timedelta(days=1))
    last_day = (
        min(date.max, latest.date() + timedelta(days=1)) if latest.date() < date.max else date.max
    )
    best = None
    while day <= last_day:
        midnight = datetime.combine(day, time())
        for minute in range(0, 1440, align):
            wall = midnight + timedelta(minutes=minute)
            try:
                candidates = local_candidates(wall, zone)
            except DateValueError as error:
                if error.code != "invalid_date":
                    raise
                # A valid local grid point at the supported year limits can
                # have no representable UTC counterpart.
                continue
            for candidate in candidates:
                try:
                    utc = candidate.astimezone(UTC)
                except (ValueError, OverflowError):
                    continue
                if start <= utc <= latest and (best is None or utc < best):
                    best = utc
        if day == date.max:
            break
        day += timedelta(days=1)
        if (
            best is not None
            and datetime.combine(day, time(), tzinfo=UTC) - timedelta(days=1) > best
        ):
            break
    return best


def interval_slots(
    value: Any,
    duration_minutes: int,
    step_minutes: int,
    max_slots: int,
    *,
    align_minutes: int | None = None,
    align_timezone: str = "UTC",
) -> list[Interval]:
    """Return at most 10,000 complete UTC slots after local wall-grid alignment.

    Each day's grid starts at local midnight, with k*align_minutes < 1440.
    align_minutes greater than a day therefore offers local midnight each day.
    Only the first start is aligned; subsequent starts use elapsed UTC steps.
    """
    duration = _positive_integer(duration_minutes, "duration_minutes")
    step = _positive_integer(step_minutes, "step_minutes")
    limit = min(_positive_integer(max_slots, "max_slots"), MAX_INTERVAL_SLOTS)
    align = step if align_minutes is None else _positive_integer(align_minutes, "align_minutes")
    zone = validate_timezone(align_timezone)
    bounds = _boundaries(value)
    if bounds is None:
        return []
    start, end = bounds
    # Compare integers before constructing timedeltas so arbitrarily large valid
    # parameter integers cannot overflow datetime or force a large allocation.
    span_microseconds = (end - start) // timedelta(microseconds=1)
    if duration * 60_000_000 > span_microseconds:
        return []
    duration_delta = timedelta(minutes=duration)
    first = _first_grid(start, end - duration_delta, align, zone)
    if first is None:
        return []
    available_microseconds = (end - duration_delta - first) // timedelta(microseconds=1)
    count = min(limit, available_microseconds // (step * 60_000_000) + 1)
    result: list[Interval] = []
    current = first
    for index in range(count):
        result.append(_interval(current, current + duration_delta))
        if index + 1 < count:
            current += timedelta(minutes=step)
    return result
