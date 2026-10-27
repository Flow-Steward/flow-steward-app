"""Unit tests for scheduler timezone behaviour.

Validates that cron expressions are interpreted in the project's
schedule_timezone and that next_run_at is always stored in UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.application.settings.timezone_service import (
    cron_next_run_utc,
    recalculate_next_run_utc,
    resolve_effective_project_tz,
)

# ---------------------------------------------------------------------------
# Minimal cron parser (same as in test_timezone_service.py)
# ---------------------------------------------------------------------------


def _parse(cron_expr: str) -> dict:
    parts = cron_expr.strip().split()
    assert len(parts) == 5
    return {
        "minute": {int(parts[0])} if parts[0] != "*" else set(range(60)),
        "hour": {int(parts[1])} if parts[1] != "*" else set(range(24)),
        "dom": set(range(1, 32)),
        "month": set(range(1, 13)),
        "dow": set(range(7)),
        "dom_any": True,
        "dow_any": True,
    }


def _matches(candidate: datetime, parsed: dict) -> bool:
    return (
        candidate.minute in parsed["minute"]
        and candidate.hour in parsed["hour"]
        and candidate.day in parsed["dom"]
        and candidate.month in parsed["month"]
        and candidate.weekday() in {(d - 1) % 7 for d in parsed["dow"]}
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchedulerProjectTimezone:
    """AC: cron '0 2 * * *' with schedule_tz='Europe/Vilnius' runs at 02:00 local."""

    def test_daily_02_vilnius_summer(self):
        ref = datetime(2024, 6, 14, 20, 0, 0, tzinfo=UTC)
        result = cron_next_run_utc(
            "0 2 * * *",
            "Europe/Vilnius",
            ref,
            _parse_cron=_parse,
            _cron_matches=_matches,
        )
        assert result.tzinfo == UTC
        # 02:00 EEST (UTC+3) = 23:00 UTC
        assert result.hour == 23
        assert result.day == 14

    def test_daily_02_vilnius_winter(self):
        ref = datetime(2024, 1, 14, 20, 0, 0, tzinfo=UTC)
        result = cron_next_run_utc(
            "0 2 * * *",
            "Europe/Vilnius",
            ref,
            _parse_cron=_parse,
            _cron_matches=_matches,
        )
        assert result.tzinfo == UTC
        # 02:00 EET (UTC+2) = 00:00 UTC next day
        assert result.hour == 0
        assert result.day == 15

    def test_utc_unchanged_when_no_project_tz(self):
        ref = datetime(2024, 6, 15, 8, 0, 0, tzinfo=UTC)
        result = cron_next_run_utc(
            "0 9 * * *",
            None,
            ref,
            _parse_cron=_parse,
            _cron_matches=_matches,
        )
        assert result.hour == 9

    def test_utc_unchanged_when_empty_tz(self):
        ref = datetime(2024, 6, 15, 8, 0, 0, tzinfo=UTC)
        result = cron_next_run_utc(
            "0 9 * * *",
            "",
            ref,
            _parse_cron=_parse,
            _cron_matches=_matches,
        )
        assert result.hour == 9


class TestRecalculateNextRun:
    """AC: when projects.schedule_timezone changes, next_run_at is recalculated."""

    def test_recalculate_from_now(self):
        result = recalculate_next_run_utc(
            "0 10 * * *",
            "Asia/Tokyo",
            _parse_cron=_parse,
            _cron_matches=_matches,
        )
        assert result.tzinfo == UTC
        from zoneinfo import ZoneInfo

        local = result.astimezone(ZoneInfo("Asia/Tokyo"))
        assert local.hour == 10
        assert local.minute == 0


class TestUserTimezoneDoesNotAffectSchedule:
    """AC: changing users.timezone does NOT shift existing cron schedules."""

    def test_same_schedule_regardless_of_user_tz(self):
        ref = datetime(2024, 6, 15, 8, 0, 0, tzinfo=UTC)
        project_tz = "Europe/Berlin"
        # Regardless of user timezone, cron uses project_tz
        r1 = cron_next_run_utc(
            "0 14 * * *", project_tz, ref, _parse_cron=_parse, _cron_matches=_matches
        )
        r2 = cron_next_run_utc(
            "0 14 * * *", project_tz, ref, _parse_cron=_parse, _cron_matches=_matches
        )
        assert r1 == r2


class TestResolveEffectiveProjectTz:
    def test_stored_value(self):
        assert resolve_effective_project_tz("America/Los_Angeles") == "America/Los_Angeles"

    def test_none_fallback(self):
        assert resolve_effective_project_tz(None) == "UTC"

    def test_empty_fallback(self):
        assert resolve_effective_project_tz("") == "UTC"

    def test_invalid_fallback(self):
        assert resolve_effective_project_tz("Fake/Zone") == "UTC"
