"""10000 compact items and several Collect: correct result, bounded checkpoint (A17)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from core.application.orchestration.control_flow_step_helpers import _resolve_state_path
from core.application.orchestration.state import JOURNAL_RETAINED_ENTRIES, retained_journal
from core.application.orchestration.step_hub import orchestrator_hub_node
from tests._workflow_definitions import edge, node
from tests.unit.test_step_hub import _isolated as _isolated
from tests.unit.test_step_hub import _run

ITEMS = 10_000


def definition() -> dict:
    """prepare (emails = [], by_id = {}) → For each order: check → keep → index | skip."""
    fe_exit = {"kind": "for_each_exit", "for_each_id": "fe", "lane_id": None}
    return {
        "steps": [
            {
                "step_id": "prepare",
                "step_kind": "set_variable",
                "override": {
                    "set_variable": {
                        "assignments": {"state.emails": [], "state.by_id": {}},
                    }
                },
            },
            {
                "step_id": "check",
                "step_kind": "if",
                "for_each_id": "fe",
                "override": {"if": {"condition": "state.order.ok == true"}},
            },
            {
                "step_id": "keep",
                "step_kind": "collect",
                "for_each_id": "fe",
                "override": {"collect": {"target": "state.emails", "source": "state.order.e"}},
            },
            {
                "step_id": "index",
                "step_kind": "collect",
                "for_each_id": "fe",
                "override": {
                    "collect": {
                        "target": "state.by_id",
                        "source": "state.order_index",
                        "mode": "set_key",
                        "key": {"source": "state.order.e"},
                    }
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
                edge("keep", node("index")),
                edge("index", fe_exit),
                edge("fe", node("done"), source_kind="for_each"),
            ],
        },
        "settings": {},
    }


def merge(state: dict, update: dict) -> dict:
    """Apply an update with the declared reducers that matter here."""
    merged = dict(state)
    for key, value in update.items():
        if key == "orchestrator_journal":
            merged[key] = retained_journal(merged.get(key), value)
        else:
            merged[key] = value
    return merged


@pytest.mark.timeout(600)
def test_ten_thousand_items_with_two_collects_stay_bounded(monkeypatch) -> None:
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    monkeypatch.setenv("FS_DATASET_QUERY_MAX_RUNTIME_ROWS", str(ITEMS))
    orders = [{"e": f"u{n}@x.test", "ok": n % 2 == 0} for n in range(ITEMS)]
    state = _run(definition(), orders=orders)
    largest_cursor = 0
    largest_messages = 0
    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        command = orchestrator_hub_node(state)
        for _ in range(ITEMS * 8):
            state = merge(state, command.update)
            if command.goto == "orchestrator_validation":
                break
            largest_cursor = max(largest_cursor, len(json.dumps(state["run_cursor"])))
            largest_messages = max(largest_messages, len(state.get("agent_messages") or []))
            state = merge(state, control_flow_step_node(state))
            command = orchestrator_hub_node(state)
        else:
            raise AssertionError("the run did not end")

    assert state["workflow_terminal"]["kind"] == "stop_success"
    emails = _resolve_state_path(state, "emails")
    assert len(emails) == ITEMS // 2
    assert emails[:2] == ["u0@x.test", "u2@x.test"]
    by_id = _resolve_state_path(state, "by_id")
    assert len(by_id) == ITEMS // 2
    assert by_id["u9998@x.test"] == 9998
    # The checkpoint never repeats the source or the growing collection per item:
    # the cursor stays small, body messages are coalesced, the journal is capped.
    assert largest_cursor < 2_000
    assert largest_messages <= 8
    assert len(state["orchestrator_journal"]) <= JOURNAL_RETAINED_ENTRIES
    assert state.get("for_each_items") is None
    finished = [
        entry for entry in state["orchestrator_journal"] if entry["event"] == "for_each_completed"
    ]
    assert finished[-1]["detail"]["processed_count"] == ITEMS
    assert finished[-1]["detail"]["truncated"] is False
