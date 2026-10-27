"""Collect adds to an ordinary variable; it never creates, resets or flattens it (ADR 0038)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.application.orchestration.collect_step_runtime import (
    CollectError,
    collect_state_update,
)
from core.application.orchestration.control_flow_step_helpers import _resolve_state_path
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.application.reports.job_detail_sections import build_execution_steps
from tests._workflow_definitions import edge, node
from tests.unit.test_step_hub import _isolated as _isolated
from tests.unit.test_step_hub import _run, _step


def apply(state: dict, config: dict) -> dict:
    result = collect_state_update(config, state)
    return {**state, **result.updates}


def test_collect_preserves_prior_values() -> None:
    state = {"workflow_data": {"emails": ["old"]}, "order": {"email": "new"}}
    after = apply(state, {"target": "state.emails", "source": "state.order.email"})
    assert _resolve_state_path(after, "emails") == ["old", "new"]


def test_set_variable_replaces_while_collect_adds() -> None:
    state = {"workflow_data": {"emails": ["old"]}, "order": {"email": "new"}}
    after = apply(state, {"target": "emails", "source": "order.email"})
    after = apply(after, {"target": "emails", "source": "order.email"})
    assert _resolve_state_path(after, "emails") == ["old", "new", "new"]


def test_an_array_value_is_one_element_never_flattened() -> None:
    state = {"workflow_data": {"pairs": []}, "pair": [1, 2]}
    after = apply(state, {"target": "pairs", "source": "pair"})
    assert _resolve_state_path(after, "pairs") == [[1, 2]]


@pytest.mark.parametrize("value", ["", 0, False, None, [], {}])
def test_falsy_and_null_values_are_collected_as_values(value) -> None:
    state = {"workflow_data": {"seen": []}, "item": {"v": value}}
    after = apply(state, {"target": "seen", "source": "item.v"})
    assert _resolve_state_path(after, "seen") == [value]


def test_set_key_uses_literal_email_key() -> None:
    state = {
        "workflow_data": {"by_email": {"x@example.test": 1}},
        "order": {"email": "a.b@example.test", "total": 7},
    }
    after = apply(
        state,
        {
            "target": "by_email",
            "source": "order.total",
            "mode": "set_key",
            "key": {"source": "order.email"},
        },
    )
    assert _resolve_state_path(after, "by_email") == {"x@example.test": 1, "a.b@example.test": 7}
    assert "a" not in _resolve_state_path(after, "by_email")


def test_set_key_replaces_an_existing_key_and_keeps_the_others() -> None:
    state = {"workflow_data": {"latest": {"k": 1, "other": 2}}, "v": 3}
    after = apply(state, {"target": "latest", "source": "v", "mode": "set_key", "key": "k"})
    assert _resolve_state_path(after, "latest") == {"k": 3, "other": 2}


@pytest.mark.parametrize(
    ("state", "config", "code"),
    [
        ({"v": 1}, {"target": "missing", "source": "v"}, "collect.target_missing"),
        (
            {"workflow_data": {"t": "text"}, "v": 1},
            {"target": "t", "source": "v"},
            "collect.target_type_invalid",
        ),
        (
            {"workflow_data": {"t": []}, "v": 1},
            {"target": "t", "source": "v", "mode": "set_key", "key": "k"},
            "collect.target_type_invalid",
        ),
        (
            {"workflow_data": {"t": {}}, "v": 1, "k": 5},
            {"target": "t", "source": "v", "mode": "set_key", "key": {"source": "k"}},
            "collect.key_invalid",
        ),
        (
            {"workflow_data": {"t": []}},
            {"target": "t", "source": "v", "mode": "flatten"},
            "collect.mode_invalid",
        ),
    ],
)
def test_missing_target_and_wrong_types_are_precise_errors(state, config, code) -> None:
    with pytest.raises(CollectError) as raised:
        collect_state_update(config, state)
    assert raised.value.code == code


def test_a_stored_null_target_is_not_created_or_replaced() -> None:
    with pytest.raises(CollectError) as raised:
        collect_state_update({"target": "t", "source": "v"}, {"workflow_data": {"t": None}, "v": 1})
    assert raised.value.code == "collect.target_type_invalid"


# --- on the hub: one checkpoint boundary, no second append ---------------------


def emails_definition() -> dict:
    """prepare (emails = []) → For each fe: check → keep (Collect) | skip (Continue) → done."""
    fe_exit = {"kind": "for_each_exit", "for_each_id": "fe", "lane_id": None}
    return {
        "steps": [
            {
                "step_id": "prepare",
                "step_kind": "set_variable",
                "override": {"set_variable": {"target": "state.completed_emails", "value": []}},
            },
            {
                "step_id": "check",
                "step_kind": "if",
                "for_each_id": "fe",
                "override": {"if": {"condition": "state.order.status == 'completed'"}},
            },
            {
                "step_id": "keep",
                "step_kind": "collect",
                "for_each_id": "fe",
                "override": {
                    "collect": {"target": "state.completed_emails", "source": "state.order.email"}
                },
            },
            {"step_id": "skip", "step_kind": "continue", "for_each_id": "fe"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [],
        "for_each": [
            {
                "for_each_id": "fe",
                "over": "state.orders",
                "item_name": "order",
                "index_name": "order_index",
                "body": {"kind": "sequence", "entry": node("check")},
            }
        ],
        "execution_graph": {
            "schema_version": 1,
            "entry": node("prepare"),
            "edges": [
                edge("prepare", node("fe", "for_each")),
                edge("check", node("keep"), "true"),
                edge("check", node("skip"), "false"),
                edge("keep", fe_exit),
                edge("fe", node("done"), source_kind="for_each"),
            ],
        },
        "settings": {},
    }


ORDERS = [
    {"status": "completed", "email": "a@example.test"},
    {"status": "open", "email": "b@example.test"},
    {"status": "completed", "email": "a.b@example.test"},
]


def run_with_real_control_flow(state: dict, *, redeliver_collect: bool = False) -> dict:
    """Drive the hub with the real control-flow executor for every step."""
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    command = orchestrator_hub_node(state)
    for _ in range(40):
        state = _step(state, command)
        if command.goto == "orchestrator_validation":
            return state
        with patch(
            "core.application.orchestration.agents.common.publish_agent_message",
            return_value=True,
        ):
            produced = control_flow_step_node(state)
            if redeliver_collect and state["current_step_id"] == "keep":
                # The worker died after the executor ran but before the
                # checkpoint: the same execution runs again from the same state.
                produced = control_flow_step_node(state)
        state = {**state, **produced}
        command = orchestrator_hub_node(state)
    raise AssertionError("no end")


@pytest.mark.parametrize("redeliver", [False, True])
def test_if_then_collect_completed_emails(redeliver) -> None:
    final = run_with_real_control_flow(
        _run(emails_definition(), orders=ORDERS), redeliver_collect=redeliver
    )
    assert final["workflow_terminal"]["kind"] == "stop_success"
    assert _resolve_state_path(final, "completed_emails") == ["a@example.test", "a.b@example.test"]


def test_collect_redelivery_is_not_second_append() -> None:
    final = run_with_real_control_flow(
        _run(emails_definition(), orders=ORDERS), redeliver_collect=True
    )
    assert len(_resolve_state_path(final, "completed_emails")) == 2


def test_missing_target_is_error() -> None:
    definition = emails_definition()
    definition["steps"][0]["override"]["set_variable"]["target"] = "state.other"
    final = run_with_real_control_flow(_run(definition, orders=ORDERS))
    assert final["status"] == "failed"
    keep = [m for m in final["agent_messages"] if m.get("step_id") == "keep"][-1]
    assert keep["artifacts"]["error"] == "collect.target_missing"


def test_collect_history_keeps_a_bounded_contribution_not_the_collection() -> None:
    final = run_with_real_control_flow(_run(emails_definition(), orders=ORDERS))
    keeps = [m for m in final["agent_messages"] if m.get("step_id") == "keep"]
    assert keeps, "the latest Collect message of the body is kept"
    artifacts = keeps[-1]["artifacts"]
    assert artifacts["collect"] == {"target": "completed_emails", "operation": "append", "count": 2}
    assert artifacts["input_payload"] == "a.b@example.test"
    assert artifacts["output_payload"] == artifacts["collect"]


def test_break_and_continue_do_not_roll_back_collect() -> None:
    definition = emails_definition()
    definition["steps"].append({"step_id": "stop_here", "step_kind": "break", "for_each_id": "fe"})
    for route in definition["execution_graph"]["edges"]:
        if route["source"] == node("keep"):
            route["target"] = node("stop_here")
    final = run_with_real_control_flow(_run(definition, orders=ORDERS))
    assert _resolve_state_path(final, "completed_emails") == ["a@example.test"]
    assert final["workflow_terminal"]["kind"] == "stop_success"


def test_a_rework_re_entry_does_not_reset_the_variable_by_itself() -> None:
    """Collect never clears its target; a reset is the author's explicit Set Variable."""
    state = {"workflow_data": {"emails": ["from-first-pass"]}, "order": {"email": "x"}}
    after = apply(state, {"target": "emails", "source": "order.email"})
    assert _resolve_state_path(after, "emails") == ["from-first-pass", "x"]


@pytest.mark.parametrize(
    ("orders", "key"),
    [(ORDERS[:1], "react.jobs.step_collect_added_one"), (ORDERS, "react.jobs.step_collect_added")],
)
def test_the_history_says_one_entry_or_entries(orders, key) -> None:
    final = run_with_real_control_flow(_run(emails_definition(), orders=orders))
    keys = [
        (message.get("artifacts") or {}).get("summary_key")
        for message in final["agent_messages"]
        if str((message.get("artifacts") or {}).get("summary_key") or "").startswith(
            "react.jobs.step_collect_added"
        )
    ]
    assert keys[-1] == key


def _project_recorded_collect(message: dict) -> dict:
    steps, _selected = build_execution_steps(
        job={"id": "collect-io-job", "project_id": "collect-io-project", "status": "completed"},
        messages=[message],
    )
    assert len(steps) == 1
    assert steps[0]["message_id"] == message.get("id")
    if "id" not in message:
        assert message["step_execution_id"]
        assert steps[0]["step_execution_id"] == message["step_execution_id"]
    assert steps[0]["step_id"] == message["step_id"]
    return steps[0]


def test_set_key_third_item_projects_its_recorded_contribution_as_input() -> None:
    definition = emails_definition()
    definition["steps"][0]["override"]["set_variable"] = {
        "target": "state.by_sku",
        "value": {},
    }
    definition["steps"][2]["override"]["collect"] = {
        "target": "state.by_sku",
        "source": "state.order.quantity",
        "mode": "set_key",
        "key": {"source": "state.order.email"},
    }
    final = run_with_real_control_flow(
        _run(
            definition,
            orders=[
                {"status": "completed", "email": "A-100", "quantity": 2},
                {"status": "open", "email": "B-200", "quantity": 0},
                {"status": "completed", "email": "C-300", "quantity": 5},
            ],
        )
    )
    message = [row for row in final["agent_messages"] if row.get("step_id") == "keep"][-1]
    recorded = message["artifacts"]
    assert recorded["input_payload"] == 5
    assert type(recorded["input_payload"]) is int
    assert recorded["output_payload"] == {
        "target": "by_sku",
        "operation": "set_key",
        "key": "C-300",
        "replaced": False,
        "count": 2,
    }
    row = _project_recorded_collect(message)
    assert row["inputs"] == {"value": 5}
    assert type(row["inputs"]["value"]) is int
    assert row["outputs"] == recorded["output_payload"]
    assert row["inputs"] != {"value": _resolve_state_path(final, "by_sku")}
    assert recorded["input_payload"] == 5


@pytest.mark.parametrize(
    "value",
    [0, False, None, "", [], {}, [0, False], {"quantity": 5}, [[], {}], [[], 0]],
)
def test_collect_projects_present_contribution_without_truthiness_loss(value) -> None:
    final = run_with_real_control_flow(
        _run(
            emails_definition(),
            orders=[
                {"status": "completed", "email": value},
            ],
        )
    )
    message = [row for row in final["agent_messages"] if row.get("step_id") == "keep"][-1]
    assert message["artifacts"]["input_payload"] == value
    row = _project_recorded_collect(message)
    expected = value if isinstance(value, dict) and value else {"value": value}
    assert row["inputs"] == expected
    if not isinstance(value, dict) or not value:
        assert type(row["inputs"]["value"]) is type(value)
    assert row["outputs"] == message["artifacts"]["collect"]
    assert _resolve_state_path(final, "completed_emails") == [value]


def test_collect_public_input_projection_preserves_existing_redaction() -> None:
    raw_secret = "audit-collect-only-not-a-credential"
    final = run_with_real_control_flow(
        _run(
            emails_definition(),
            orders=[
                {"status": "completed", "email": {"password": raw_secret}},
            ],
        )
    )
    message = [row for row in final["agent_messages"] if row.get("step_id") == "keep"][-1]
    recorded = message["artifacts"]["input_payload"]
    assert recorded["password"] != raw_secret
    row = _project_recorded_collect(message)
    assert row["inputs"]["password"] == "[REDACTED]"
    assert raw_secret not in str(row["inputs"])


def test_legacy_collect_without_recorded_contribution_keeps_legacy_inputs() -> None:
    message = {
        "id": "legacy-collect-message",
        "step_id": "keep",
        "status": "pass",
        "from_agent": "control_flow_step",
        "inputs": {"legacy": 0},
        "artifacts": {
            "step_kind": "collect",
            "output_payload": {"target": "by_sku", "operation": "set_key", "count": 2},
        },
    }
    row = _project_recorded_collect(message)
    assert row["inputs"] == {"legacy": 0}
    assert "value" not in row["inputs"]
    assert "input_payload" not in message["artifacts"]


def test_collect_preserves_empty_contributions_without_reintroducing_technical_items() -> None:
    value = [{"cursor": "internal-only"}, {}, 0, {"handle": "artifact:synthetic-collect"}]
    final = run_with_real_control_flow(
        _run(
            emails_definition(),
            orders=[
                {"status": "completed", "email": value},
            ],
        )
    )
    message = [row for row in final["agent_messages"] if row.get("step_id") == "keep"][-1]
    assert message["artifacts"]["input_payload"] == value
    row = _project_recorded_collect(message)
    assert row["inputs"] == {"value": [{}, 0]}
    assert "internal-only" not in str(row["inputs"])
    assert "artifact:synthetic-collect" not in str(row["inputs"])
    assert message["artifacts"]["input_payload"] == value
