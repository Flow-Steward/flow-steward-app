"""Unified timezone conversion and validation service.

All public functions use ``zoneinfo`` from the Python standard library.
IANA timezone identifiers are the only accepted format.

Design principles:
- UTC is the canonical storage format; conversion happens at the service edge.
- Two scopes: *user timezone* (display preference) and *project schedule
  timezone* (cron interpretation).  Per-cron timezone is intentionally not
  supported.
- DST policy: for ambiguous local times (fall-back) we pick the *first*
  occurrence (``fold=0``); for non-existent local times (spring-forward) we
  retain the pre-transition offset (a UTC roundtrip advances by the gap).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.infrastructure.date_timezones import (
    legacy_to_local,
    legacy_to_utc,
)
from core.infrastructure.date_timezones import (
    validate_timezone as _shared_validate_timezone,
)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def is_valid_timezone(tz_name: str | None) -> bool:
    """Return True if the legacy settings API can load the identifier."""
    try:
        _shared_validate_timezone(tz_name, strict=False)
    except ValueError:
        return False
    return True


def validate_timezone(tz_name: str | None) -> str:
    """Return the legacy normalized timezone or its existing ValueError message."""
    return _shared_validate_timezone(tz_name, strict=False)


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

_UTC = "UTC"


def resolve_effective_user_tz(user_tz: str | None) -> str:
    """Return the effective timezone for a user.

    Falls back to ``UTC`` when the stored value is *None* or empty.
    """
    if user_tz and isinstance(user_tz, str) and user_tz.strip():
        candidate = user_tz.strip()
        if is_valid_timezone(candidate):
            return validate_timezone(candidate)
    return _UTC


def resolve_effective_project_tz(project_schedule_tz: str | None) -> str:
    """Return the effective schedule timezone for a project.

    Falls back to ``UTC`` when the stored value is *None* or empty.
    """
    if project_schedule_tz and isinstance(project_schedule_tz, str) and project_schedule_tz.strip():
        candidate = project_schedule_tz.strip()
        if is_valid_timezone(candidate):
            return validate_timezone(candidate)
    return _UTC


def default_project_schedule_tz(user_tz: str | None) -> str:
    """Determine the initial ``schedule_timezone`` for a newly created project.

    Uses the creating user's timezone when available, otherwise ``UTC``.
    """
    return resolve_effective_user_tz(user_tz)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def to_utc(local_dt: datetime, tz_name: str) -> datetime:
    """Convert a *local* datetime (naive or aware) to UTC.

    If *local_dt* is naive it is assumed to represent time in *tz_name*.
    """
    return legacy_to_utc(local_dt, tz_name)


def to_local(utc_dt: datetime, tz_name: str) -> datetime:
    """Convert a UTC datetime to the given local timezone."""
    return legacy_to_local(utc_dt, tz_name)


def format_local(utc_dt: datetime | None, tz_name: str, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """Format a UTC datetime as a local-time string, or return ``'—'``."""
    if utc_dt is None:
        return "—"
    return to_local(utc_dt, tz_name).strftime(fmt)


# ---------------------------------------------------------------------------
# Date-range boundaries for reports / filters
# ---------------------------------------------------------------------------


def day_boundaries_utc(date_str: str, tz_name: str) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for a calendar day in the given timezone.

    ``date_str`` must be ``YYYY-MM-DD``.  The range is ``[start, end)`` —
    start is inclusive, end is exclusive (midnight of the next day).
    """
    tz = ZoneInfo(validate_timezone(tz_name))
    parts = date_str.strip().split("-")
    if len(parts) != 3:
        raise ValueError(f"Invalid date format (expected YYYY-MM-DD): {date_str}")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    start_local = datetime(year, month, day, 0, 0, 0, tzinfo=tz)
    end_local = datetime(year, month, day, 0, 0, 0, tzinfo=tz) + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


# ---------------------------------------------------------------------------
# Cron / scheduler helpers
# ---------------------------------------------------------------------------


def cron_next_run_utc(
    cron_expr: str,
    project_schedule_tz: str,
    reference_utc: datetime | None = None,
    *,
    _parse_cron: Callable[[str], Any] | None = None,
    _cron_matches: Callable[[datetime, Any], bool] | None = None,
) -> datetime:
    """Calculate the next cron execution time, interpreting the expression
    in the *project* schedule timezone and returning a UTC datetime.

    The optional ``_parse_cron`` / ``_cron_matches`` callbacks allow the
    caller to inject the existing cron parser from ``serve.py`` without
    circular imports.

    DST policy:
    - Spring-forward gap (non-existent local time): the candidate minute is
      skipped automatically because ``ZoneInfo`` shifts it forward.
    - Fall-back overlap (ambiguous local time): we use ``fold=0`` (the first
      occurrence) so behaviour is deterministic.
    """
    if _parse_cron is None or _cron_matches is None:
        raise TypeError("_parse_cron and _cron_matches callbacks are required")

    tz_name = resolve_effective_project_tz(project_schedule_tz)
    tz = ZoneInfo(tz_name)

    base_utc = reference_utc or datetime.now(UTC)
    if base_utc.tzinfo is None:
        base_utc = base_utc.replace(tzinfo=UTC)

    # Work in local time of the project
    base_local = base_utc.astimezone(tz)
    candidate = (base_local + timedelta(minutes=1)).replace(second=0, microsecond=0)

    parsed = _parse_cron(cron_expr)
    max_minutes = 366 * 24 * 60
    for _ in range(max_minutes):
        # Ensure deterministic fold for ambiguous times
        candidate = candidate.replace(fold=0)
        if _cron_matches(candidate, parsed):
            return candidate.astimezone(UTC)
        candidate += timedelta(minutes=1)

    raise ValueError(f"Cron expression '{cron_expr}' has no next run in the next 366 days")


def recalculate_next_run_utc(
    cron_expr: str,
    new_schedule_tz: str,
    *,
    _parse_cron: Callable[[str], Any] | None = None,
    _cron_matches: Callable[[datetime, Any], bool] | None = None,
) -> datetime:
    """Recalculate next_run_at when a project's schedule_timezone changes.

    Always calculates from *now* in the new timezone.
    """
    return cron_next_run_utc(
        cron_expr,
        new_schedule_tz,
        reference_utc=datetime.now(UTC),
        _parse_cron=_parse_cron,
        _cron_matches=_cron_matches,
    )
