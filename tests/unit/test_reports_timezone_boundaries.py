"""Unit tests for report date-range boundary calculations.

Validates that day/period filters compute correct UTC boundaries when
the effective timezone introduces offsets (including across DST transitions).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.application.settings.timezone_service import day_boundaries_utc


class TestDayBoundariesUTC:
    """The daily filter computes the UTC window without an off-by-one error."""

    def test_utc_full_day(self):
        start, end = day_boundaries_utc("2024-06-15", "UTC")
        assert start == datetime(2024, 6, 15, 0, 0, 0, tzinfo=UTC)
        assert end == datetime(2024, 6, 16, 0, 0, 0, tzinfo=UTC)
        assert (end - start).total_seconds() == 86400

    def test_positive_offset_vilnius_summer(self):
        # Europe/Vilnius summer = UTC+3
        start, end = day_boundaries_utc("2024-06-15", "Europe/Vilnius")
        # June 15 00:00 EEST = June 14 21:00 UTC
        assert start.hour == 21
        assert start.day == 14
        # June 16 00:00 EEST = June 15 21:00 UTC
        assert end.hour == 21
        assert end.day == 15

    def test_negative_offset_new_york_summer(self):
        # America/New_York summer = UTC-4
        start, end = day_boundaries_utc("2024-06-15", "America/New_York")
        # June 15 00:00 EDT = June 15 04:00 UTC
        assert start.hour == 4
        assert start.day == 15
        # June 16 00:00 EDT = June 16 04:00 UTC
        assert end.hour == 4
        assert end.day == 16

    def test_negative_offset_new_york_winter(self):
        # America/New_York winter = UTC-5
        start, end = day_boundaries_utc("2024-01-15", "America/New_York")
        assert start.hour == 5
        assert start.day == 15
        assert end.hour == 5
        assert end.day == 16

    def test_dst_transition_day_spring_forward(self):
        """March 10 2024 in US/Eastern: clocks spring forward at 02:00.
        Day is only 23 hours long in local time."""
        start, end = day_boundaries_utc("2024-03-10", "US/Eastern")
        duration = (end - start).total_seconds()
        assert duration == 23 * 3600  # 23 hours

    def test_dst_transition_day_fall_back(self):
        """November 3 2024 in US/Eastern: clocks fall back at 02:00.
        Day is 25 hours long in local time."""
        start, end = day_boundaries_utc("2024-11-03", "US/Eastern")
        duration = (end - start).total_seconds()
        assert duration == 25 * 3600  # 25 hours

    def test_invalid_date_format_raises(self):
        with pytest.raises(ValueError):
            day_boundaries_utc("15-06-2024", "UTC")

    def test_invalid_timezone_raises(self):
        with pytest.raises(ValueError, match="Unknown IANA"):
            day_boundaries_utc("2024-06-15", "Fake/Zone")

    def test_boundary_no_off_by_one(self):
        """Verify that the last second of the day is INSIDE the range,
        and the first second of the next day is OUTSIDE."""
        start, end = day_boundaries_utc("2024-06-15", "Asia/Tokyo")
        # Asia/Tokyo = UTC+9
        # June 15 00:00 JST = June 14 15:00 UTC
        assert start == datetime(2024, 6, 14, 15, 0, 0, tzinfo=UTC)
        # June 16 00:00 JST = June 15 15:00 UTC
        assert end == datetime(2024, 6, 15, 15, 0, 0, tzinfo=UTC)
        # A timestamp at June 15 14:59:59 UTC should be INSIDE
        inside = datetime(2024, 6, 15, 14, 59, 59, tzinfo=UTC)
        assert start <= inside < end
        # A timestamp at June 15 15:00:00 UTC should be OUTSIDE
        outside = datetime(2024, 6, 15, 15, 0, 0, tzinfo=UTC)
        assert not (start <= outside < end)
