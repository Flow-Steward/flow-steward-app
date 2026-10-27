"""Cron expression parser and next-run calculator.

Ported from serve.py so the control-plane process can evaluate cron
expressions without importing the full app module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_CRON_MONTH_ALIASES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_CRON_DOW_ALIASES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


def _normalize_cron_expr(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _cron_parse_value(
    token: str,
    *,
    minimum: int,
    maximum: int,
    aliases: dict[str, int] | None = None,
    day_of_week: bool = False,
) -> int:
    text = str(token or "").strip().lower()
    if aliases and text in aliases:
        return int(aliases[text])
    try:
        parsed = int(text)
    except Exception as e:
        raise ValueError(f"Invalid cron value '{token}'") from e
    if day_of_week and parsed == 7:
        parsed = 0
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"Cron value '{token}' is out of range [{minimum}, {maximum}]")
    return parsed


def _cron_parse_field(
    field: str,
    *,
    minimum: int,
    maximum: int,
    aliases: dict[str, int] | None = None,
    day_of_week: bool = False,
) -> tuple[set[int], bool]:
    normalized = str(field or "").strip().lower()
    if not normalized:
        raise ValueError("Cron field cannot be empty")

    all_values = set(range(minimum, maximum + 1))
    values: set[int] = set()
    wildcard = False
    for item in normalized.split(","):
        token = item.strip()
        if not token:
            raise ValueError("Cron list contains an empty token")
        if token in {"*", "?"}:
            values.update(all_values)
            wildcard = True
            continue

        if "/" in token:
            base, step_text = token.split("/", 1)
            try:
                step = int(step_text)
            except Exception as e:
                raise ValueError(f"Invalid cron step '{token}'") from e
            if step <= 0:
                raise ValueError(f"Cron step must be > 0 in '{token}'")
        else:
            base = token
            step = 1

        base = base.strip()
        if base in {"*", "?"} or not base:
            start, end = minimum, maximum
            wildcard = True
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _cron_parse_value(
                start_text,
                minimum=minimum,
                maximum=maximum,
                aliases=aliases,
                day_of_week=day_of_week,
            )
            end = _cron_parse_value(
                end_text,
                minimum=minimum,
                maximum=maximum,
                aliases=aliases,
                day_of_week=day_of_week,
            )
            if start > end:
                raise ValueError(f"Cron range start must be <= end in '{token}'")
        else:
            parsed = _cron_parse_value(
                base,
                minimum=minimum,
                maximum=maximum,
                aliases=aliases,
                day_of_week=day_of_week,
            )
            start, end = parsed, parsed

        values.update(range(start, end + 1, step))

    if not values:
        raise ValueError(f"Cron field '{field}' resolved to no values")
    return values, wildcard


def _parse_cron_expression(cron_expr: str) -> dict:
    normalized = _normalize_cron_expr(cron_expr)
    parts = normalized.split(" ")
    if len(parts) != 5:
        raise ValueError(
            "Cron expression must have exactly 5 fields: minute hour day-of-month month day-of-week"
        )

    minute_values, minute_any = _cron_parse_field(parts[0], minimum=0, maximum=59)
    hour_values, hour_any = _cron_parse_field(parts[1], minimum=0, maximum=23)
    dom_values, dom_any = _cron_parse_field(parts[2], minimum=1, maximum=31)
    month_values, month_any = _cron_parse_field(
        parts[3], minimum=1, maximum=12, aliases=_CRON_MONTH_ALIASES
    )
    dow_values, dow_any = _cron_parse_field(
        parts[4], minimum=0, maximum=6, aliases=_CRON_DOW_ALIASES, day_of_week=True
    )

    return {
        "expr": normalized,
        "minute": minute_values,
        "hour": hour_values,
        "dom": dom_values,
        "month": month_values,
        "dow": dow_values,
        "minute_any": minute_any,
        "hour_any": hour_any,
        "dom_any": dom_any,
        "month_any": month_any,
        "dow_any": dow_any,
    }


def _cron_matches_datetime(dt: datetime, parsed: dict) -> bool:
    if dt.minute not in parsed["minute"]:
        return False
    if dt.hour not in parsed["hour"]:
        return False
    if dt.month not in parsed["month"]:
        return False

    dom_match = dt.day in parsed["dom"]
    cron_dow = (dt.weekday() + 1) % 7  # python: Mon=0; cron: Sun=0
    dow_match = cron_dow in parsed["dow"]
    dom_any = bool(parsed.get("dom_any"))
    dow_any = bool(parsed.get("dow_any"))
    if dom_any and dow_any:
        return True
    if dom_any:
        return dow_match
    if dow_any:
        return dom_match
    return dom_match or dow_match


def cron_next_run_at(
    cron_expr: str,
    reference_at: datetime | None = None,
    *,
    schedule_tz: str | None = None,
) -> datetime:
    """Calculate the next run time for a cron expression after *reference_at*."""
    parsed = _parse_cron_expression(cron_expr)
    base = reference_at or datetime.now(UTC)
    base = base.replace(tzinfo=UTC) if base.tzinfo is None else base.astimezone(UTC)
    tz = ZoneInfo(str(schedule_tz or "").strip() or "UTC")
    candidate = (base.astimezone(tz) + timedelta(minutes=1)).replace(second=0, microsecond=0)
    max_minutes = 366 * 24 * 60
    for _ in range(max_minutes):
        candidate = candidate.replace(fold=0)
        if _cron_matches_datetime(candidate, parsed):
            return candidate.astimezone(UTC)
        candidate += timedelta(minutes=1)
    raise ValueError(f"Cron expression '{cron_expr}' has no next run in the next 366 days")
