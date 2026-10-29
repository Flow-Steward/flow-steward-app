"""Pure calendar arithmetic and absolute comparisons on parsed date values."""

from __future__ import annotations

import calendar
import math
from datetime import UTC, datetime, time, timedelta

from core.infrastructure.date_timezones import resolve_datetime, validate_timezone
from core.infrastructure.date_types import DateValue, DateValueError


def _datetime(value: DateValue) -> datetime:
    return value if isinstance(value, datetime) else datetime.combine(value, time())


def _utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError) as error:
        raise DateValueError(
            "invalid_date", "The absolute moment is outside years 1000 through 9999."
        ) from error


def _aware(value: DateValue) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _absolute(value: DateValue, timezone: str | None, dst_policy: str) -> datetime:
    # An explicit offset already defines the instant. A supplied zone must be
    # valid but is only needed to resolve a naive operand, never to display it.
    if timezone is not None:
        validate_timezone(timezone)
    source_zone = None if _aware(value) else timezone
    return _datetime(resolve_datetime(value, source_zone, dst_policy, require_aware=True))


def _range(value: DateValue) -> DateValue:
    if value.year < 1000:
        raise DateValueError("invalid_date", "The resulting year must be between 1000 and 9999.")
    return value


def _calendar_result(value: datetime, template: DateValue, dst_policy: str) -> DateValue:
    if not isinstance(template, datetime):
        return _range(value.date())
    if _aware(template):
        zone = getattr(template.tzinfo, "key", None)
        if zone:
            return _range(resolve_datetime(value, zone, dst_policy))
        return _range(value.replace(tzinfo=template.tzinfo))
    return _range(value)


def add(
    value: DateValue,
    amount: int,
    unit: str,
    *,
    timezone: str | None = None,
    dst_policy: str = "error",
) -> DateValue:
    """Add elapsed hours/minutes or calendar units, clamping month ends."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise DateValueError(
            "invalid_amount", "amount must be an integer, not a boolean or fraction."
        )
    if unit not in {"minutes", "hours", "days", "weeks", "months", "years"}:
        raise DateValueError("unsupported_unit", f"Unsupported date addition unit: {unit!r}.")
    resolved = resolve_datetime(value, timezone, dst_policy)
    current = _datetime(resolved)
    try:
        if unit in {"minutes", "hours"}:
            delta = timedelta(**{unit: amount})
            if _aware(current):
                return _range((current.astimezone(UTC) + delta).astimezone(current.tzinfo))
            return _range(current + delta)
        wall = current.replace(tzinfo=None)
        if unit in {"days", "weeks"}:
            result = wall + timedelta(**{unit: amount})
        else:
            month_index = wall.year * 12 + wall.month - 1 + amount * (12 if unit == "years" else 1)
            year, month_zero = divmod(month_index, 12)
            if not 1000 <= year <= 9999:
                raise ValueError("year out of range")
            month = month_zero + 1
            result = wall.replace(
                year=year, month=month, day=min(wall.day, calendar.monthrange(year, month)[1])
            )
        return _calendar_result(result, resolved, dst_policy)
    except (OverflowError, ValueError) as error:
        if isinstance(error, DateValueError):
            raise
        raise DateValueError(
            "invalid_date", "The date addition is outside years 1000 through 9999."
        ) from error


def diff(
    value: DateValue,
    from_value: DateValue,
    unit: str,
    *,
    timezone: str | None = None,
    dst_policy: str = "error",
) -> int | float:
    """Return signed complete calendar units or fractional elapsed hours/minutes."""
    if unit not in {"minutes", "hours", "days", "months", "years"}:
        raise DateValueError("unsupported_unit", f"Unsupported date difference unit: {unit!r}.")
    elapsed = unit in {"minutes", "hours"}
    if not elapsed and timezone is None and (_aware(value) or _aware(from_value)):
        raise DateValueError(
            "timezone_required", "Calendar differences involving offsets require a common timezone."
        )
    if elapsed:
        finish = _absolute(value, timezone, dst_policy)
        start = _absolute(from_value, timezone, dst_policy)
        seconds = (_utc(finish) - _utc(start)).total_seconds()
        return seconds / (60 if unit == "minutes" else 3600)
    finish = _datetime(resolve_datetime(value, timezone, dst_policy))
    start = _datetime(resolve_datetime(from_value, timezone, dst_policy))
    finish, start = finish.replace(tzinfo=None), start.replace(tzinfo=None)
    if unit == "days":
        return math.trunc((finish - start).total_seconds() / 86400)
    sign = 1 if finish >= start else -1
    early, late = (start, finish) if sign > 0 else (finish, start)
    if unit == "months":
        count = (late.year - early.year) * 12 + late.month - early.month
        if (late.day, late.time()) < (early.day, early.time()):
            count -= 1
    else:
        count = late.year - early.year
        if (late.month, late.day, late.time()) < (early.month, early.day, early.time()):
            count -= 1
    return sign * count


def part(
    value: DateValue, part: str, *, timezone: str | None = None, dst_policy: str = "error"
) -> int:
    """Extract a calendar field; weekdays follow ISO Monday=1."""
    if part not in {"year", "month", "day", "weekday", "iso_week", "hour", "minute"}:
        raise DateValueError("unsupported_unit", f"Unsupported date part: {part!r}.")
    resolved = _datetime(resolve_datetime(value, timezone, dst_policy))
    if part == "weekday":
        return resolved.isoweekday()
    if part == "iso_week":
        return resolved.isocalendar().week
    return getattr(resolved, part)


def _boundary(
    value: DateValue, unit: str, timezone: str | None, dst_policy: str, *, end: bool
) -> DateValue:
    if unit not in {"day", "week", "month", "year"}:
        raise DateValueError("unsupported_unit", f"Unsupported date boundary unit: {unit!r}.")
    resolved = resolve_datetime(value, timezone, dst_policy)
    wall = _datetime(resolved).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    try:
        if unit == "week":
            wall -= timedelta(days=wall.weekday())
        elif unit == "month":
            wall = wall.replace(day=1)
        elif unit == "year":
            wall = wall.replace(month=1, day=1)
        if end:
            if unit in {"day", "week"}:
                wall += timedelta(days=1 if unit == "day" else 7)
            elif unit == "month":
                wall = wall.replace(year=wall.year + (wall.month == 12), month=wall.month % 12 + 1)
            else:
                wall = wall.replace(year=wall.year + 1)
        return _calendar_result(wall, resolved, dst_policy)
    except (OverflowError, ValueError) as error:
        if isinstance(error, DateValueError):
            raise
        raise DateValueError(
            "invalid_date", "The period boundary is outside years 1000 through 9999."
        ) from error


def start_of(
    value: DateValue, unit: str, *, timezone: str | None = None, dst_policy: str = "error"
) -> DateValue:
    """Return the inclusive calendar boundary, with Monday starting the week."""
    return _boundary(value, unit, timezone, dst_policy, end=False)


def end_of(
    value: DateValue, unit: str, *, timezone: str | None = None, dst_policy: str = "error"
) -> DateValue:
    """Return the next boundary, excluded from the current calendar period."""
    return _boundary(value, unit, timezone, dst_policy, end=True)


def compare(
    value: DateValue, to_value: DateValue, *, timezone: str | None = None, dst_policy: str = "error"
) -> int:
    """Compare absolute moments; naive values require an explicit source zone."""
    left = _utc(_absolute(value, timezone, dst_policy))
    right = _utc(_absolute(to_value, timezone, dst_policy))
    return (left > right) - (left < right)
