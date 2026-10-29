from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch


def test_tabular_date_action_catalog_exposes_only_single_value_operations():
    from core.application.tabular_feed.contracts import (
        TABULAR_ACTION_PARAMS,
        TABULAR_ACTION_REQUIRED_PARAMS,
    )
    from core.application.tabular_feed.validation import validate_tabular_feed_config

    expected = {
        "date.parse",
        "date.to_utc",
        "date.to_timezone",
        "date.format",
        "date.add",
        "date.part",
        "date.start_of",
        "date.end_of",
    }
    assert expected <= TABULAR_ACTION_PARAMS.keys()
    assert not {"date.diff", "date.compare", "date.combine"} & TABULAR_ACTION_PARAMS.keys()
    assert TABULAR_ACTION_REQUIRED_PARAMS["date.format"] == {"output_format"}
    assert TABULAR_ACTION_REQUIRED_PARAMS["date.to_timezone"] == {"timezone"}

    base = {
        "operation": "transform",
        "result_target": "state.result",
        "input": {"dataset_handle": "dataset:dates"},
    }
    accepted = validate_tabular_feed_config(
        {
            **base,
            "actions": [
                {
                    "action": "date.format",
                    "target": "date",
                    "params": {
                        "input_format": "MM/DD/YYYY",
                        "output_format": "DD.MM.YYYY",
                        "locale": "en",
                    },
                }
            ],
        }
    )
    rejected = validate_tabular_feed_config(
        {
            **base,
            "actions": [{"action": "date.diff", "target": "date", "params": {}}],
        }
    )
    assert accepted == []
    assert any(error["code"] == "workflow.step_tabular_feed_action_unknown" for error in rejected)
    private_rule = validate_tabular_feed_config(
        {
            **base,
            "rules": [
                {
                    "target": "date",
                    "date_operation": {"action_id": "date.parse", "params": {}},
                }
            ],
        }
    )
    assert any(
        error["code"] == "workflow.step_tabular_feed_rule_field_unknown" for error in private_rule
    )


def test_non_date_tabular_actions_do_not_load_date_lexicons():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration import date_action_support
    from core.application.tabular_feed.context import TabularFeedExecutionContext
    from core.application.tabular_feed.service import TabularFeedService

    with (
        patch.object(date_action_support, "get_date_lexicons") as get_lexicons,
        patch(
            "core.application.tabular_feed.service.engine.execute_operation",
            return_value={"ok": True, "result": {}},
        ),
    ):
        TabularFeedService(plane=ArtifactDatasetPlane()).execute(
            operation="transform",
            payload={"actions": [{"action": "text.trim", "target": "name", "params": {}}]},
            context=TabularFeedExecutionContext(),
        )

    get_lexicons.assert_not_called()


def test_nested_input_date_action_loads_requested_lexicon():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration import date_action_support
    from core.application.tabular_feed.context import TabularFeedExecutionContext
    from core.application.tabular_feed.service import TabularFeedService

    lexicons = {"en": object(), "lt": object()}
    with (
        patch.object(
            date_action_support, "get_date_lexicons", return_value=lexicons
        ) as get_lexicons,
        patch(
            "core.application.tabular_feed.service.engine.execute_operation",
            return_value={"ok": True, "result": {}},
        ) as execute_operation,
    ):
        TabularFeedService(plane=ArtifactDatasetPlane()).execute(
            operation="transform",
            payload={
                "input": {
                    "actions": [
                        {
                            "action": "date.format",
                            "target": "date",
                            "params": {"locale": "lt", "output_format": "DD.MM.YYYY"},
                        }
                    ]
                }
            },
            context=TabularFeedExecutionContext(),
        )

    get_lexicons.assert_called_once_with(("en", "lt"))
    assert execute_operation.call_args.kwargs["date_lexicons"] is lexicons


def test_tabular_date_rule_preserves_null_and_does_not_parse_nonmatching_rows():
    import polars as pl

    from core.application.orchestration.date_action_support import get_date_lexicons
    from core.infrastructure.date_operations import execute_date_operation
    from core.infrastructure.tabular_feed.actions import _apply_rule_expressions, _normalize_actions

    frame = pl.DataFrame(
        {
            "raw_date": ["10/29/2026", "not-a-date", None],
            "apply": ["yes", "no", "yes"],
        }
    )
    [rule] = _normalize_actions(
        {
            "actions": [
                {
                    "action": "date.format",
                    "target": "raw_date",
                    "params": {
                        "input_format": "MM/DD/YYYY",
                        "output_format": "DD.MM.YYYY",
                    },
                    "when": {"column": "apply", "equals": "yes"},
                }
            ]
        }
    )
    result, _report = _apply_rule_expressions(
        frame, [rule], date_lexicons=get_date_lexicons(("en",))
    )
    assert result["raw_date"].to_list() == ["29.10.2026", "not-a-date", None]
    assert (
        execute_date_operation(
            "date.format",
            "10/29/2026",
            {"input_format": "MM/DD/YYYY", "output_format": "DD.MM.YYYY"},
            lexicons=get_date_lexicons(("en",)),
        )
        == "29.10.2026"
    )


def test_tabular_date_row_dispatch_uses_same_shared_operation():
    from core.application.orchestration.date_action_support import get_date_lexicons
    from core.infrastructure.tabular_feed.actions import _apply_rule, _shared_action_to_legacy_rule

    [lexicon] = get_date_lexicons(("en",)).values()
    row = {"date": "10/29/2026"}
    rule = _shared_action_to_legacy_rule(
        {
            "action": "date.format",
            "target": "date",
            "params": {"input_format": "MM/DD/YYYY", "output_format": "DD.MM.YYYY"},
        }
    )
    assert _apply_rule(row, rule, date_lexicons={"en": lexicon}) is True
    assert row["date"] == "29.10.2026"


def test_tabular_date_replacement_handles_many_unique_values_without_nested_case_tree():
    import polars as pl

    from core.application.orchestration.date_action_support import get_date_lexicons
    from core.infrastructure.tabular_feed.actions import _apply_rule_expressions, _normalize_actions

    values = [(date(2000, 1, 1) + timedelta(days=index)).isoformat() for index in range(1600)]
    frame = pl.DataFrame({"raw_date": values})
    [rule] = _normalize_actions(
        {
            "actions": [
                {
                    "action": "date.format",
                    "target": "raw_date",
                    "params": {"input_format": "YYYY-MM-DD", "output_format": "YYYY-MM-DD"},
                }
            ]
        }
    )

    result, _report = _apply_rule_expressions(
        frame, [rule], date_lexicons=get_date_lexicons(("en",))
    )
    assert result.height == len(values)
    assert result["raw_date"].to_list() == values


def test_tabular_date_value_error_keeps_named_code_and_message_in_existing_envelope():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.tabular_feed.context import TabularFeedExecutionContext
    from core.application.tabular_feed.service import TabularFeedService

    plane = ArtifactDatasetPlane()
    dataset, _metadata = plane.create_dataset(
        owner_scope={"account_id": "acct_dates", "project_id": "project_dates"},
        run_id="run_dates",
        step_id="read",
        schema={"columns": [{"name": "date", "type": "text"}]},
        rows=[{"date": "03/04/2026"}],
    )
    result = TabularFeedService(plane=plane).execute(
        operation="transform",
        payload={
            "input": {
                "dataset_handle": f"dataset:{dataset.dataset_id}",
                "actions": [
                    {
                        "action": "date.parse",
                        "target": "date",
                        "params": {},
                    }
                ],
            },
        },
        context=TabularFeedExecutionContext(
            account_id="acct_dates", project_id="project_dates", workflow_id="workflow_dates"
        ),
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "ambiguous_date"
    assert "input_format" in result["errors"][0]["message"]
    assert result["errors"][0]["path"] == "actions"
    assert result["result"] == {}


def _execute_date_part(rows: list[dict[str, object]], *, condition: dict[str, object]):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.tabular_feed.context import TabularFeedExecutionContext
    from core.application.tabular_feed.service import TabularFeedService

    plane = ArtifactDatasetPlane()
    dataset, _metadata = plane.create_dataset(
        owner_scope={"account_id": "acct_part", "project_id": "project_part"},
        run_id="run_part",
        step_id="read",
        schema={"columns": [{"name": "value", "type": "text"}, {"name": "apply", "type": "text"}]},
        rows=rows,
    )
    return TabularFeedService(plane=plane).execute(
        operation="transform",
        payload={
            "input": {
                "dataset_handle": f"dataset:{dataset.dataset_id}",
                "actions": [
                    {
                        "action": "date.part",
                        "target": "value",
                        "params": {"part": "year"},
                        "when": condition,
                    }
                ],
            }
        },
        context=TabularFeedExecutionContext(
            account_id="acct_part", project_id="project_part", workflow_id="workflow_part"
        ),
    )


def test_conditional_date_part_rejects_mixed_text_and_numeric_column_values():
    result = _execute_date_part(
        [
            {"value": "2026-10-29", "apply": "yes"},
            {"value": "not-a-date", "apply": "no"},
        ],
        condition={"column": "apply", "equals": "yes"},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "invalid_rules"
    assert "cannot mix numeric results" in result["errors"][0]["message"]


def test_conditional_date_part_treats_null_predicate_as_nonmatching_for_mixed_check():
    result = _execute_date_part(
        [
            {"value": "2026-10-29", "apply": "yes"},
            {"value": "not-a-date", "apply": None},
        ],
        condition={"column": "apply", "equals": "yes"},
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "invalid_rules"
    assert "cannot mix numeric results" in result["errors"][0]["message"]


def test_conditional_date_part_allows_unmatched_null_and_keeps_integer_results():
    result = _execute_date_part(
        [
            {"value": "2026-10-29", "apply": "yes"},
            {"value": None, "apply": "no"},
        ],
        condition={"column": "apply", "equals": "yes"},
    )

    assert result["ok"] is True
    assert [row["value"] for row in result["result"]["preview_rows"]] == [2026, None]


def test_conditional_date_part_with_no_matching_values_is_a_noop():
    result = _execute_date_part(
        [
            {"value": "not-a-date", "apply": "no"},
            {"value": None, "apply": "no"},
        ],
        condition={"column": "apply", "equals": "yes"},
    )

    assert result["ok"] is True
    assert [row["value"] for row in result["result"]["preview_rows"]] == [
        "not-a-date",
        None,
    ]


def test_unconditional_date_part_returns_integer_values():
    result = _execute_date_part(
        [
            {"value": "2026-10-29", "apply": "yes"},
            {"value": "2024-04-12", "apply": "no"},
        ],
        condition={},
    )

    assert result["ok"] is True
    assert [row["value"] for row in result["result"]["preview_rows"]] == [2026, 2024]
