"""Canonical UTC interval and DST slot-grid contracts."""

from datetime import datetime

import pytest

from core.infrastructure import date_intervals as intervals
from core.infrastructure.date_types import DateValueError


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def window(start, end):
    return {"empty": False, "start": start, "end": end}


EMPTY = {"empty": True, "start": None, "end": None}


def test_make_normalizes_absolute_operands_and_requires_positive_length():
    result = intervals.interval_make(dt("2026-10-29T10:00+01:00"), dt("2026-10-29T11:30Z"))
    assert result == window("2026-10-29T09:00:00Z", "2026-10-29T11:30:00Z")
    assert intervals.interval_duration(result) == 150
    start = dt("2026-10-29T09:00Z")
    for end in ("2026-10-29T09:00Z", "2026-10-29T08:00Z"):
        end_value = dt(end)
        with pytest.raises(DateValueError) as error:
            intervals.interval_make(start, end_value)
        assert error.value.code == "invalid_interval"


def test_intersection_and_empty_contracts():
    one = window("2026-10-29T09:00:00Z", "2026-10-29T12:00:00Z")
    two = window("2026-10-29T09:00:00Z", "2026-10-29T11:30:00Z")
    assert intervals.interval_intersect_all([one, two]) == two
    assert intervals.interval_intersect_all([one, EMPTY]) == EMPTY
    assert intervals.interval_duration(EMPTY) == 0
    assert intervals.interval_slots(EMPTY, 45, 15, 3) == []
    touching = window("2026-10-29T12:00:00Z", "2026-10-29T13:00:00Z")
    assert intervals.interval_intersect_all([one, touching]) == EMPTY
    with pytest.raises(DateValueError) as error:
        intervals.interval_intersect_all([])
    assert error.value.code == "empty_interval_list"


@pytest.mark.parametrize(
    "start,end,zone,duration,step,expected",
    [
        (
            "2026-10-29T09:00:00Z",
            "2026-10-29T11:30:00Z",
            "UTC",
            45,
            15,
            [("09:00", "09:45"), ("09:15", "10:00"), ("09:30", "10:15")],
        ),
        (
            "2026-06-15T06:00:00Z",
            "2026-06-15T08:00:00Z",
            "Asia/Kathmandu",
            45,
            30,
            [("06:15", "07:00"), ("06:45", "07:30"), ("07:15", "08:00")],
        ),
        (
            "2026-03-29T00:45:00Z",
            "2026-03-29T02:00:00Z",
            "Europe/Berlin",
            30,
            30,
            [("01:00", "01:30"), ("01:30", "02:00")],
        ),
    ],
)
def test_slots_match_approved_scenario_and_grid(start, end, zone, duration, step, expected):
    slots = intervals.interval_slots(window(start, end), duration, step, 3, align_timezone=zone)
    prefix = start[:10]
    assert slots == [window(f"{prefix}T{s}:00Z", f"{prefix}T{e}:00Z") for s, e in expected]


def test_fold_grid_preserves_both_absolute_occurrences():
    value = window("2026-10-25T00:00:00Z", "2026-10-25T02:00:00Z")
    slots = intervals.interval_slots(
        value, 30, 30, 4, align_minutes=30, align_timezone="Europe/Berlin"
    )
    assert [slot["start"] for slot in slots] == [
        "2026-10-25T00:00:00Z",
        "2026-10-25T00:30:00Z",
        "2026-10-25T01:00:00Z",
        "2026-10-25T01:30:00Z",
    ]
    # Start in the second fold: the earlier local grid occurrence must not hide it.
    value["start"] = "2026-10-25T01:10:00Z"
    assert (
        intervals.interval_slots(value, 15, 30, 1, align_timezone="Europe/Berlin")[0]["start"]
        == "2026-10-25T01:30:00Z"
    )


@pytest.mark.parametrize("name", ["duration_minutes", "step_minutes", "align_minutes", "max_slots"])
@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "1", None])
def test_slot_parameters_require_positive_integers(name, bad):
    params = {"duration_minutes": 30, "step_minutes": 15, "align_minutes": 15, "max_slots": 3}
    params[name] = bad
    # None is the documented omission sentinel only for align_minutes.
    if name == "align_minutes" and bad is None:
        return
    with pytest.raises(DateValueError) as error:
        intervals.interval_slots(EMPTY, **params)
    assert error.value.code == "invalid_amount"


def test_slot_cap_and_short_interval():
    value = window("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    slots = intervals.interval_slots(value, 1, 1, 10001)
    assert len(slots) == 10000
    assert slots[-1]["start"] == "2026-01-07T22:39:00Z"
    assert (
        intervals.interval_slots(window("2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"), 2, 1, 1)
        == []
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"empty": True, "start": "bad", "end": None},
        window("2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00Z"),
        window("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ],
)
def test_interval_objects_require_canonical_valid_boundaries(value):
    with pytest.raises(DateValueError) as error:
        intervals.interval_duration(value)
    assert error.value.code == "invalid_interval"


@pytest.mark.parametrize(
    "align,start,expected",
    [
        (2000, "2026-10-29T00:01:00Z", "2026-10-30T00:00:00Z"),
        (50, "2026-10-29T01:00:00Z", "2026-10-29T01:40:00Z"),
    ],
)
def test_alignment_grid_restarts_at_each_local_midnight(align, start, expected):
    value = window(start, "2026-10-31T00:00:00Z")
    assert intervals.interval_slots(value, 1, 15, 1, align_minutes=align)[0]["start"] == expected


def test_extreme_integer_parameters_do_not_overflow_or_allocate_excessively():
    value = window("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    assert intervals.interval_slots(value, 10**100, 1, 1) == []
    assert len(intervals.interval_slots(value, 1, 10**100, 10**100)) == 1
    edge = window("9999-12-31T23:00:00Z", "9999-12-31T23:59:00Z")
    assert intervals.interval_slots(edge, 30, 30, 3) == [
        window("9999-12-31T23:00:00Z", "9999-12-31T23:30:00Z")
    ]


def test_last_day_grid_skips_local_candidates_outside_representable_utc():
    edge = window("9999-12-31T23:00:00Z", "9999-12-31T23:59:00Z")
    assert intervals.interval_slots(edge, 30, 30, 3, align_timezone="America/New_York") == [
        window("9999-12-31T23:00:00Z", "9999-12-31T23:30:00Z")
    ]


@pytest.mark.parametrize("zone", ["America/New_York", "Pacific/Kiritimati"])
def test_make_does_not_convert_aware_boundaries_to_the_supplied_zone(zone):
    assert intervals.interval_make(
        dt("9999-12-31T22:00:00Z"), dt("9999-12-31T23:00:00Z"), timezone=zone
    ) == window("9999-12-31T22:00:00Z", "9999-12-31T23:00:00Z")


@pytest.mark.parametrize(
    "zone,code", [("CET", "abbreviated_timezone"), ("Invalid/Zone", "unknown_timezone")]
)
def test_make_still_validates_supplied_zone_for_aware_boundaries(zone, code):
    start, end = dt("2026-10-29T09:00:00Z"), dt("2026-10-29T10:00:00Z")
    with pytest.raises(DateValueError) as error:
        intervals.interval_make(start, end, timezone=zone)
    assert error.value.code == code
