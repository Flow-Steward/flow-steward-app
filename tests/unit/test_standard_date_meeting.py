"""Cross-path meeting acceptance vectors for the registered standard date actions."""

import pytest


def test_meeting_availability_pipeline_uses_registered_date_actions():
    from core.application.orchestration.date_actions import build_date_actions
    from core.application.orchestration.transform_actions import list_transform_action_contracts

    registered = build_date_actions()
    published_ids = {contract["action_id"] for contract in list_transform_action_contracts()}
    required_actions = {
        "date.combine",
        "date.to_utc",
        "date.interval_make",
        "date.interval_intersect_all",
        "date.interval_duration",
        "date.interval_slots",
        "date.to_timezone",
        "date.format",
    }
    assert required_actions <= registered.keys()
    assert required_actions <= published_ids

    participants = (
        {"timezone": "Europe/Berlin", "start": "10:00", "end": "13:00"},
        {"timezone": "Europe/London", "start": "09:00", "end": "11:30"},
        {"timezone": "America/New_York", "start": "05:00", "end": "08:00"},
    )
    windows = []
    for participant in participants:
        local_start = registered["date.combine"].func(
            "2026-10-29",
            {"time": participant["start"], "timezone": participant["timezone"]},
        )
        local_end = registered["date.combine"].func(
            "2026-10-29",
            {"time": participant["end"], "timezone": participant["timezone"]},
        )
        utc_start = registered["date.to_utc"].func(
            local_start, {"timezone": participant["timezone"]}
        )
        utc_end = registered["date.to_utc"].func(local_end, {"timezone": participant["timezone"]})
        windows.append(registered["date.interval_make"].func(utc_start, {"end": utc_end}))

    intersection = registered["date.interval_intersect_all"].func(windows, {})
    assert intersection == {
        "empty": False,
        "start": "2026-10-29T09:00:00Z",
        "end": "2026-10-29T11:30:00Z",
    }
    assert registered["date.interval_duration"].func(intersection, {}) == 150

    slots = registered["date.interval_slots"].func(
        intersection,
        {"duration_minutes": 45, "step_minutes": 15, "max_slots": 3},
    )
    assert slots == [
        {
            "empty": False,
            "start": "2026-10-29T09:00:00Z",
            "end": "2026-10-29T09:45:00Z",
        },
        {
            "empty": False,
            "start": "2026-10-29T09:15:00Z",
            "end": "2026-10-29T10:00:00Z",
        },
        {
            "empty": False,
            "start": "2026-10-29T09:30:00Z",
            "end": "2026-10-29T10:15:00Z",
        },
    ]

    local_starts = []
    for slot in slots:
        berlin_time = registered["date.to_timezone"].func(
            slot["start"], {"timezone": "Europe/Berlin"}
        )
        local_starts.append(registered["date.format"].func(berlin_time, {"output_format": "HH:mm"}))
    assert local_starts == ["10:00", "10:15", "10:30"]


def test_date_format_adapter_converts_explicit_us_format_and_reports_ambiguity():
    from core.application.orchestration.date_action_support import get_date_lexicons
    from core.application.orchestration.date_actions import execute_date_action
    from core.infrastructure.date_types import DateValueError

    lexicons = get_date_lexicons(("en", "lt"))
    assert (
        execute_date_action(
            "date.format",
            "10/29/2026 5:00 PM",
            {
                "input_format": "MM/DD/YYYY h:mm A",
                "output_format": "DD.MM.YYYY HH:mm",
                "locale": "en",
            },
            lexicons=lexicons,
        )
        == "29.10.2026 17:00"
    )
    assert (
        execute_date_action(
            "date.format",
            "10/29/2026",
            {"input_format": "MM/DD/YYYY", "output_format": "D MMMM YYYY", "locale": "en"},
            lexicons=lexicons,
        )
        == "29 October 2026"
    )

    with pytest.raises(DateValueError) as error:
        execute_date_action("date.parse", "03/04/2026", {"locale": "en"}, lexicons=lexicons)
    assert error.value.code == "ambiguous_date"
