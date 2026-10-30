"""Approved date arithmetic contracts, independent of the host timezone."""

from datetime import UTC, date, datetime

import pytest

from core.infrastructure import date_arithmetic as dates
from core.infrastructure.date_types import DateValueError


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize(
    "value,amount,unit,expected",
    [
        (date(2026, 1, 31), 1, "months", date(2026, 2, 28)),
        (date(2028, 1, 31), 1, "months", date(2028, 2, 29)),
        (date(2028, 2, 29), 1, "years", date(2029, 2, 28)),
        (dt("2026-03-29T01:30:00"), 2, "hours", dt("2026-03-29T03:30:00")),
        (date(2026, 12, 31), 1, "days", date(2027, 1, 1)),
    ],
)
def test_add_calendar_and_clamping(value, amount, unit, expected):
    assert dates.add(value, amount, unit) == expected


@pytest.mark.parametrize(
    "amount,unit,expected",
    [
        (1, "days", "2026-10-25T12:00:00+01:00"),
        (24, "hours", "2026-10-25T11:00:00+01:00"),
    ],
)
def test_add_distinguishes_wall_days_from_elapsed_hours(amount, unit, expected):
    result = dates.add(dt("2026-10-24T12:00:00"), amount, unit, timezone="Europe/Berlin")
    assert result.isoformat() == expected


@pytest.mark.parametrize("amount", [True, 1.5, "1", None])
def test_add_requires_integer_amount(amount):
    start = date(2026, 1, 1)
    with pytest.raises(DateValueError) as error:
        dates.add(start, amount, "days")
    assert error.value.code == "invalid_amount"


@pytest.mark.parametrize(
    "source,value,unit,expected",
    [
        ("2026-01-31T00:00:00", "2026-02-28T00:00:00", "months", 0),
        ("2026-01-31T00:00:00", "2026-03-01T00:00:00", "months", 1),
        ("2026-10-28T23:59:00", "2026-10-29T00:01:00", "days", 0),
        ("2026-01-01T12:00:00", "2027-01-01T11:59:00", "years", 0),
    ],
)
def test_diff_counts_complete_calendar_units_in_both_directions(source, value, unit, expected):
    assert dates.diff(dt(value), dt(source), unit) == expected
    assert dates.diff(dt(source), dt(value), unit) == -expected


def test_diff_calendar_dst_day_is_not_elapsed_24_hours():
    source, value = dt("2026-10-24T12:00:00+02:00"), dt("2026-10-25T12:00:00+01:00")
    assert dates.diff(value, source, "days", timezone="Europe/Berlin") == 1
    assert dates.diff(value, source, "hours") == 25
    with pytest.raises(DateValueError) as error:
        dates.diff(value, source, "days")
    assert error.value.code == "timezone_required"


def test_mixed_diff_resolves_naive_in_explicit_common_zone():
    source, value = dt("2026-10-24T12:00:00"), dt("2026-10-25T12:00:00+01:00")
    assert dates.diff(value, source, "days", timezone="Europe/Berlin") == 1
    with pytest.raises(DateValueError) as error:
        dates.diff(value, source, "hours")
    assert error.value.code == "timezone_required"


def test_boundaries_keep_naive_and_resolve_each_dst_boundary():
    value = dt("2026-10-25T12:00:00+01:00")
    start = dates.start_of(value, "day", timezone="Europe/Berlin")
    end = dates.end_of(value, "day", timezone="Europe/Berlin")
    assert start.isoformat() == "2026-10-25T00:00:00+02:00"
    assert end.isoformat() == "2026-10-26T00:00:00+01:00"
    assert (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() == 90000
    naive = dt("2026-10-29T17:00:00")
    assert dates.start_of(naive, "day") == dt("2026-10-29T00:00:00")
    assert dates.end_of(naive, "day") == dt("2026-10-30T00:00:00")


@pytest.mark.parametrize(
    "unit,start,end",
    [
        ("week", date(2026, 10, 26), date(2026, 11, 2)),
        ("month", date(2026, 10, 1), date(2026, 11, 1)),
        ("year", date(2026, 1, 1), date(2027, 1, 1)),
    ],
)
def test_date_only_boundaries_preserve_date_type(unit, start, end):
    assert dates.start_of(date(2026, 10, 29), unit) == start
    assert dates.end_of(date(2026, 10, 29), unit) == end


def test_parts_and_absolute_comparison():
    assert dates.part(date(2026, 11, 1), "weekday") == 7
    assert dates.part(date(2026, 10, 29), "iso_week") == 44
    assert dates.compare(dt("2026-10-29T09:00Z"), dt("2026-10-29T10:00+01:00")) == 0
    assert dates.compare(dt("2026-10-29T09:01Z"), dt("2026-10-29T09:00Z")) == 1
    first, second = date(2026, 1, 1), date(2026, 1, 2)
    with pytest.raises(DateValueError) as error:
        dates.compare(first, second)
    assert error.value.code == "timezone_required"


def test_calendar_add_rejects_gap_and_supports_explicit_first_policy():
    value = dt("2026-03-28T02:30:00")
    with pytest.raises(DateValueError) as error:
        dates.add(value, 1, "days", timezone="Europe/Berlin")
    assert error.value.code == "nonexistent_local_time"
    assert (
        dates.add(value, 1, "days", timezone="Europe/Berlin", dst_policy="first").isoformat()
        == "2026-03-29T03:30:00+02:00"
    )


@pytest.mark.parametrize(
    "operation,args",
    [
        (dates.add, (date(2026, 1, 1), 1, "fortnights")),
        (dates.diff, (date(2026, 1, 1), date(2026, 1, 2), "weeks")),
        (dates.start_of, (date(2026, 1, 1), "hour")),
        (dates.part, (date(2026, 1, 1), "second")),
    ],
)
def test_unsupported_units_have_named_code(operation, args):
    with pytest.raises(DateValueError) as error:
        operation(*args)
    assert error.value.code == "unsupported_unit"


@pytest.mark.parametrize("operation", [dates.compare, lambda a, b: dates.diff(a, b, "hours")])
def test_absolute_arithmetic_maps_utc_overflow_to_named_error(operation):
    first, second = dt("9999-12-31T23:00:00-12:00"), dt("9999-12-31T23:00:00Z")
    with pytest.raises(DateValueError) as error:
        operation(first, second)
    assert error.value.code == "invalid_date"


@pytest.mark.parametrize("zone", ["America/New_York", "Pacific/Kiritimati"])
def test_absolute_operations_validate_zone_without_representing_aware_values_in_it(zone):
    # Kiritimati presentation would overflow into year 10000; an absolute
    # comparison/difference still only needs the original representable instant.
    earlier = dt("9999-12-31T22:00:00Z")
    later = dt("9999-12-31T23:00:00Z")
    assert dates.compare(later, earlier, timezone=zone) == 1
    assert dates.diff(later, earlier, "hours", timezone=zone) == 1
    assert dates.diff(earlier, later, "minutes", timezone=zone) == -60


@pytest.mark.parametrize(
    "zone,code", [("CET", "abbreviated_timezone"), ("Invalid/Zone", "unknown_timezone")]
)
def test_absolute_operations_still_validate_supplied_zone_for_aware_values(zone, code):
    instant = dt("2026-10-29T09:00:00Z")
    for operation in (dates.compare, lambda a, b, **kwargs: dates.diff(a, b, "hours", **kwargs)):
        with pytest.raises(DateValueError) as error:
            operation(instant, instant, timezone=zone)
        assert error.value.code == code
