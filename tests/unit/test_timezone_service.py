"""Unit tests for core/application/settings/timezone_service.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.application.settings.timezone_service import (
    cron_next_run_utc,
    day_boundaries_utc,
    default_project_schedule_tz,
    format_local,
    is_valid_timezone,
    resolve_effective_project_tz,
    resolve_effective_user_tz,
    to_local,
    to_utc,
    validate_timezone,
)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestIsValidTimezone:
    def test_valid(self):
        assert is_valid_timezone("UTC") is True
        assert is_valid_timezone("Europe/Vilnius") is True
        assert is_valid_timezone("America/New_York") is True
        assert is_valid_timezone("US/Eastern") is True

    def test_invalid(self):
        assert is_valid_timezone("Not/Real") is False
        assert is_valid_timezone("") is False
        assert is_valid_timezone(None) is False
        assert is_valid_timezone("  ") is False

    def test_whitespace_stripped(self):
        assert is_valid_timezone(" UTC ") is True


class TestValidateTimezone:
    def test_valid_returns_cleaned(self):
        assert validate_timezone("  Europe/London  ") == "Europe/London"
        assert validate_timezone("UTC") == "UTC"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Unknown IANA"):
            validate_timezone("Fake/Zone")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="required"):
            validate_timezone("")

    def test_none_raises(self):
        with pytest.raises(ValueError, match="required"):
            validate_timezone(None)


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


class TestResolveEffectiveUserTz:
    def test_returns_stored(self):
        assert resolve_effective_user_tz("Europe/Berlin") == "Europe/Berlin"

    def test_fallback_on_none(self):
        assert resolve_effective_user_tz(None) == "UTC"

    def test_fallback_on_empty(self):
        assert resolve_effective_user_tz("") == "UTC"

    def test_fallback_on_invalid(self):
        assert resolve_effective_user_tz("Bogus/Zone") == "UTC"


class TestResolveEffectiveProjectTz:
    def test_returns_stored(self):
        assert resolve_effective_project_tz("Europe/Vilnius") == "Europe/Vilnius"

    def test_fallback_on_none(self):
        assert resolve_effective_project_tz(None) == "UTC"


class TestDefaultProjectScheduleTz:
    def test_uses_user_tz(self):
        assert default_project_schedule_tz("Asia/Tokyo") == "Asia/Tokyo"

    def test_fallback_to_utc(self):
        assert default_project_schedule_tz(None) == "UTC"
        assert default_project_schedule_tz("") == "UTC"


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


class TestToUtc:
    def test_naive_local(self):
        # 2024-06-15 12:00 in Europe/Vilnius (UTC+3 in summer)
        local = datetime(2024, 6, 15, 12, 0, 0)
        utc = to_utc(local, "Europe/Vilnius")
        assert utc.tzinfo == UTC
        assert utc.hour == 9  # 12:00 EEST = 09:00 UTC

    def test_aware_utc(self):
        utc_dt = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        result = to_utc(utc_dt, "US/Eastern")
        assert result.tzinfo == UTC
        assert result.hour == 10  # already UTC


class TestToLocal:
    def test_basic(self):
        utc_dt = datetime(2024, 6, 15, 9, 0, 0, tzinfo=UTC)
        local = to_local(utc_dt, "Europe/Vilnius")
        assert local.hour == 12  # UTC+3 in summer

    def test_naive_treated_as_utc(self):
        naive = datetime(2024, 1, 15, 10, 0, 0)
        local = to_local(naive, "America/New_York")
        assert local.hour == 5  # UTC-5 in winter


class TestFormatLocal:
    def test_none_returns_dash(self):
        assert format_local(None, "UTC") == "—"

    def test_formats(self):
        utc_dt = datetime(2024, 6, 15, 9, 0, 0, tzinfo=UTC)
        result = format_local(utc_dt, "UTC")
        assert "2024-06-15" in result
        assert "09:00:00" in result


# ---------------------------------------------------------------------------
# Day boundaries
# ---------------------------------------------------------------------------


class TestDayBoundariesUtc:
    def test_utc_no_offset(self):
        start, end = day_boundaries_utc("2024-06-15", "UTC")
        assert start == datetime(2024, 6, 15, 0, 0, 0, tzinfo=UTC)
        assert end == datetime(2024, 6, 16, 0, 0, 0, tzinfo=UTC)

    def test_vilnius_offset(self):
        # Europe/Vilnius in summer is UTC+3
        start, end = day_boundaries_utc("2024-06-15", "Europe/Vilnius")
        assert start.tzinfo == UTC
        assert start.hour == 21  # previous day 21:00 UTC = midnight EEST
        assert start.day == 14
        assert end.hour == 21
        assert end.day == 15

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError):
            day_boundaries_utc("not-a-date", "UTC")


# ---------------------------------------------------------------------------
# Cron next-run with timezone
# ---------------------------------------------------------------------------


def _simple_parse_cron(cron_expr: str) -> dict:
    """Minimal cron parser for testing: supports only 'M H * * *' format."""
    parts = cron_expr.strip().split()
    assert len(parts) == 5, f"Expected 5 fields: {cron_expr}"
    return {
        "minute": {int(parts[0])} if parts[0] != "*" else set(range(60)),
        "hour": {int(parts[1])} if parts[1] != "*" else set(range(24)),
        "dom": set(range(1, 32)),
        "month": set(range(1, 13)),
        "dow": set(range(7)),
        "dom_any": True,
        "dow_any": True,
    }


def _simple_cron_matches(candidate: datetime, parsed: dict) -> bool:
    return (
        candidate.minute in parsed["minute"]
        and candidate.hour in parsed["hour"]
        and candidate.day in parsed["dom"]
        and candidate.month in parsed["month"]
        and candidate.weekday() in {(d - 1) % 7 for d in parsed["dow"]}  # cron 0=Sun
    )


class TestCronNextRunUtc:
    def test_utc_baseline(self):
        ref = datetime(2024, 6, 15, 8, 30, 0, tzinfo=UTC)
        result = cron_next_run_utc(
            "0 9 * * *",
            "UTC",
            reference_utc=ref,
            _parse_cron=_simple_parse_cron,
            _cron_matches=_simple_cron_matches,
        )
        assert result.tzinfo == UTC
        assert result.hour == 9
        assert result.minute == 0
        assert result.day == 15

    def test_vilnius_timezone(self):
        # 02:00 daily in Europe/Vilnius (summer, UTC+3) → 23:00 UTC previous day
        ref = datetime(2024, 6, 14, 20, 0, 0, tzinfo=UTC)
        result = cron_next_run_utc(
            "0 2 * * *",
            "Europe/Vilnius",
            reference_utc=ref,
            _parse_cron=_simple_parse_cron,
            _cron_matches=_simple_cron_matches,
        )
        assert result.tzinfo == UTC
        assert result.hour == 23  # 02:00 EEST = 23:00 UTC
        assert result.day == 14  # same day in UTC

    def test_requires_callbacks(self):
        with pytest.raises(TypeError):
            cron_next_run_utc("0 9 * * *", "UTC")


# ---------------------------------------------------------------------------
# DST transitions
# ---------------------------------------------------------------------------


class TestDstTransitions:
    def test_spring_forward_gap_deterministic(self):
        """2024-03-31 in Europe/Vilnius: clocks jump from 03:00 EET to 04:00 EEST.
        A cron at 03:30 (gap time) fires at the UTC equivalent of 03:30 EET
        (fold=0), which is 01:30 UTC.  In wall-clock time post-DST this
        appears as 04:30 EEST — deterministic behaviour documented in DST policy.
        """
        ref = datetime(2024, 3, 30, 23, 0, 0, tzinfo=UTC)
        result = cron_next_run_utc(
            "30 3 * * *",
            "Europe/Vilnius",
            reference_utc=ref,
            _parse_cron=_simple_parse_cron,
            _cron_matches=_simple_cron_matches,
        )
        assert result.tzinfo == UTC
        # 03:30 EET (UTC+2, fold=0) = 01:30 UTC
        assert result.hour == 1
        assert result.minute == 30
        assert result.day == 31
        # In post-DST wall clock: 04:30 EEST
        from zoneinfo import ZoneInfo

        local = result.astimezone(ZoneInfo("Europe/Vilnius"))
        assert local.hour == 4
        assert local.minute == 30

    def test_fall_back_ambiguous_picks_first(self):
        """2024-10-27 in Europe/Vilnius: clocks go back from 04:00 to 03:00.
        A cron at 03:30 occurs twice — we pick the FIRST occurrence (fold=0).
        """
        # Reference just before the fallback
        ref = datetime(2024, 10, 26, 22, 0, 0, tzinfo=UTC)
        result = cron_next_run_utc(
            "30 3 * * *",
            "Europe/Vilnius",
            reference_utc=ref,
            _parse_cron=_simple_parse_cron,
            _cron_matches=_simple_cron_matches,
        )
        assert result.tzinfo == UTC
        # First 03:30 EEST (UTC+3) = 00:30 UTC on Oct 27
        from zoneinfo import ZoneInfo

        local = result.astimezone(ZoneInfo("Europe/Vilnius"))
        assert local.hour == 3
        assert local.minute == 30
        assert local.day == 27
