"""Application date adapters preserve typed contracts and real gettext names."""

from dataclasses import FrozenInstanceError

import pytest


def test_date_registry_exposes_fifteen_typed_actions_and_shared_params():
    from core.application.orchestration.date_actions import build_date_actions

    actions = build_date_actions()
    assert len(actions) == 15
    assert actions["date.interval_intersect_all"].output_type == "object"
    assert actions["date.interval_slots"].required_params == (
        "duration_minutes",
        "step_minutes",
        "max_slots",
    )
    for name in ("parse", "combine", "diff", "compare", "interval_make"):
        action = actions[f"date.{name}"]
        assert action.tool_id == "std.date"
        assert action.params_schema["properties"]["dst_policy"]["default"] == "error"
        assert action.params_schema["properties"]["century"]["default"] == 2000
    assert "timezone" in actions["date.to_utc"].param_sources
    assert "from" in actions["date.diff"].param_sources


def test_real_gettext_lexicons_are_complete_immutable_and_strict():
    from core.application.orchestration.date_action_support import get_date_lexicons
    from core.infrastructure.date_types import DateValueError

    lexicons = get_date_lexicons(("en", "lt"))
    assert lexicons["lt"].months_full[9] == "spalio"
    assert lexicons["lt"].months_full_standalone[9] == "spalis"
    assert lexicons["en"].weekdays_full[0] == "Monday"
    with pytest.raises(TypeError):
        lexicons["xx"] = lexicons["en"]
    with pytest.raises(FrozenInstanceError):
        lexicons["en"].locale = "xx"
    with pytest.raises(DateValueError) as error:
        get_date_lexicons(("not-supported",))
    assert error.value.code == "unknown_locale"


@pytest.mark.parametrize(
    ("action", "value", "params", "expected"),
    [
        ("parse", "2026-10-29T17:00", {}, "2026-10-29T17:00:00"),
        ("to_utc", "2026-10-29T10:00", {"timezone": "Europe/Berlin"}, "2026-10-29T09:00:00Z"),
        (
            "to_utc",
            "2026-10-29T10:00+01:00",
            {"timezone": "America/New_York"},
            "2026-10-29T09:00:00Z",
        ),
        (
            "to_timezone",
            "2026-10-29T09:00:00Z",
            {"timezone": "Europe/Berlin"},
            "2026-10-29T10:00:00+01:00",
        ),
        (
            "format",
            "29 spalio 2026",
            {"input_format": "D MMMM YYYY", "output_format": "LLLL YYYY", "locale": "lt"},
            "spalis 2026",
        ),
        (
            "format",
            "10/03/85",
            {"input_format": "DD/MM/YY", "output_format": "YYYY-MM-DD", "century": 1900},
            "1985-03-10",
        ),
        (
            "combine",
            "2026-10-29",
            {"time": "5:00 PM", "timezone": "Europe/Berlin"},
            "2026-10-29T17:00:00+01:00",
        ),
        ("add", "2026-01-31", {"amount": 1, "unit": "months"}, "2026-02-28"),
        ("diff", "2026-02-28", {"from": "2026-01-31", "unit": "months"}, 0),
        ("part", "2026-10-25", {"part": "weekday"}, 7),
        ("start_of", "2026-10-29T17:00", {"unit": "day"}, "2026-10-29T00:00:00"),
        ("end_of", "2026-10-29T17:00", {"unit": "day"}, "2026-10-30T00:00:00"),
        ("compare", "2026-10-29T10:00:00+01:00", {"to": "2026-10-29T09:00:00Z"}, 0),
        (
            "interval_make",
            "2026-10-29T09:00Z",
            {"end": "2026-10-29T10:00Z"},
            {"empty": False, "start": "2026-10-29T09:00:00Z", "end": "2026-10-29T10:00:00Z"},
        ),
        ("interval_duration", {"empty": True, "start": None, "end": None}, {}, 0),
        (
            "interval_intersect_all",
            [{"empty": True, "start": None, "end": None}],
            {},
            {"empty": True, "start": None, "end": None},
        ),
        (
            "interval_slots",
            {"empty": True, "start": None, "end": None},
            {"duration_minutes": 45, "step_minutes": 15, "max_slots": 3},
            [],
        ),
    ],
)
def test_date_operation_adapter_vectors(action, value, params, expected):
    from core.application.orchestration.date_actions import execute_date_action

    assert execute_date_action(f"date.{action}", value, params) == expected


@pytest.mark.parametrize(
    ("value", "params", "code"),
    [
        ("2026-10-25T02:30", {"timezone": "Europe/Berlin"}, "ambiguous_local_time"),
        ("2026-03-29T02:30", {"timezone": "Europe/Berlin"}, "nonexistent_local_time"),
        ("2026-10-29", {"timezone": "CET"}, "abbreviated_timezone"),
        ("2026-10-29", {"locale": "no-such-language"}, "unknown_locale"),
    ],
)
def test_date_adapter_preserves_domain_error_codes(value, params, code):
    from core.application.orchestration.date_actions import execute_date_action
    from core.infrastructure.date_types import DateValueError

    with pytest.raises(DateValueError) as error:
        execute_date_action("date.parse", value, params)
    assert error.value.code == code


def test_named_error_passes_existing_runtime_with_complete_context(monkeypatch):
    from core.application.orchestration import transform_step_runtime as runtime
    from core.application.orchestration.date_actions import build_date_actions

    for key, action in build_date_actions().items():
        monkeypatch.setitem(runtime._ACTIONS, key, action)
    result = runtime.execute_transform_step(
        config={
            "input": {"source": "state.rows"},
            "mode": "list",
            "result_target": "state.result",
            "fields": [
                {
                    "from": "when",
                    "to": "when",
                    "type": "text",
                    "actions": [{"action": "date.to_utc", "params": {"timezone": "Europe/Berlin"}}],
                }
            ],
        },
        state={"rows": [{"when": "2026-10-25T02:30"}]},
    )
    assert result["ok"] is False
    error = result["errors"][0]
    assert error["reason"] == "ambiguous_local_time"
    assert error["action"] == "date.to_utc"
    assert error["selector"] == "when"
    assert error["field_index"] == error["action_index"] == error["element_index"] == 0
    assert result["state_patch"] == {}


def test_untrusted_code_attribute_does_not_change_runtime_reason(monkeypatch):
    from core.application.orchestration import transform_step_runtime as runtime
    from core.application.orchestration.transform_action_contract import TransformAction

    class UntrustedFailure(ValueError):
        code = "invalid_date"

    def fail(value, params):
        raise UntrustedFailure("not a date-domain exception")

    action = TransformAction(
        "text.test_failure",
        "text",
        "Test failure",
        "scalar",
        "text",
        "text",
        {"type": "object", "properties": {}},
        (),
        {},
        fail,
    )
    monkeypatch.setitem(runtime._ACTIONS, action.action_id, action)
    with pytest.raises(runtime.TransformRuntimeFailure) as error:
        runtime._execute_action_pipeline(
            "x",
            [{"action": action.action_id}],
            field_index=0,
            field_to="x",
            selector="x",
            field_type="text",
            selector_root={},
            element_index=None,
        )
    assert error.value.errors[0]["reason"] == "action_execution_failed"


def test_to_utc_validates_aware_timezone_without_converting_through_it():
    from core.application.orchestration.date_actions import execute_date_action

    assert (
        execute_date_action(
            "date.to_utc", "9999-12-31T23:00:00Z", {"timezone": "Pacific/Kiritimati"}
        )
        == "9999-12-31T23:00:00Z"
    )


def test_to_utc_reports_named_error_for_utc_calendar_overflow():
    from core.application.orchestration.date_actions import execute_date_action
    from core.infrastructure.date_types import DateValueError

    with pytest.raises(DateValueError) as error:
        execute_date_action("date.to_utc", "9999-12-31T23:00:00-12:00", {})
    assert error.value.code == "invalid_date"
    assert isinstance(error.value.__cause__, (OverflowError, ValueError))


@pytest.mark.parametrize(
    ("locale", "output_locale", "expected"),
    [
        (None, None, "October 2026"),
        ("lt", None, "spalio 2026"),
        ("lt", "en", "October 2026"),
    ],
)
def test_none_locale_defaults_are_shared_with_pure_dispatch(locale, output_locale, expected):
    from core.application.orchestration.date_action_support import get_date_lexicons
    from core.application.orchestration.date_actions import execute_date_action
    from core.infrastructure.date_operations import execute_date_operation

    params = {"locale": locale, "output_locale": output_locale, "output_format": "MMMM YYYY"}
    required = ("en",) if locale is None else ("lt", "en") if output_locale == "en" else ("lt",)
    lexicons = get_date_lexicons(required)
    assert execute_date_action("date.format", "2026-10-29", params) == expected
    assert (
        execute_date_operation("date.format", "2026-10-29", params, lexicons=lexicons) == expected
    )


@pytest.mark.parametrize("key", ["locale", "output_locale"])
@pytest.mark.parametrize("invalid", ["", "de", 12, False])
def test_locale_defaults_do_not_relax_invalid_explicit_values(key, invalid):
    from core.application.orchestration.date_actions import execute_date_action
    from core.infrastructure.date_types import DateValueError

    with pytest.raises(DateValueError) as error:
        execute_date_action(
            "date.format", "2026-10-29", {key: invalid, "output_format": "MMMM YYYY"}
        )
    assert error.value.code == "unknown_locale"


def test_pure_date_dispatch_rejects_float_century_before_calendar_construction():
    from core.application.orchestration.date_action_support import get_date_lexicons
    from core.infrastructure.date_operations import execute_date_operation
    from core.infrastructure.date_types import DateValueError

    with pytest.raises(DateValueError) as error:
        execute_date_operation(
            "date.parse",
            "29/10/26",
            {"input_format": "DD/MM/YY", "century": 2000.0},
            lexicons=get_date_lexicons(("en",)),
        )
    assert error.value.code == "invalid_date"
