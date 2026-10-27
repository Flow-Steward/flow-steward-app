"""A For each item must survive the LangGraph checkpoint under any configured name.

Regression for the workflow E2E ledger finding "a loop silently loses its item on
every name except ``current_story``": the item was written as a bare top-level
channel, and LangGraph drops writes to channels ``OrchestratorState`` does not
declare. The For each runtime fixes its items in the frame at entry and
publishes each item (and its index) through the declared ``workflow_data``
channel (ADR 0038).
"""

from __future__ import annotations

import pytest

from core.application.orchestration.control_flow_step_helpers import _resolve_state_path
from core.application.orchestration.for_each_runtime import publish_item, select_items
from core.application.orchestration.run_cursor import ForEachFrame, load_frame
from core.application.orchestration.state import OrchestratorState
from core.application.registry.contract_validation import validate_workflow_readiness
from tests._workflow_definitions import edge, node

DECLARED_CHANNELS = frozenset(OrchestratorState.__annotations__)


def _checkpoint(state: dict, updates: dict) -> dict:
    """Apply updates the way LangGraph does — undeclared channels are dropped."""
    persisted = dict(state)
    for key, value in updates.items():
        if key in DECLARED_CHANNELS:
            persisted[key] = value
    return persisted


def _for_each(item_name: str, over: str = "item_chunks", index_name: str | None = None) -> dict:
    return {
        "for_each_id": "push_chunks",
        "over": over,
        "item_name": item_name,
        "index_name": index_name,
        "body": {"kind": "sequence", "entry": {"kind": "step", "id": "push"}},
    }


def _frame(execution_id: str = "fe-1") -> ForEachFrame:
    return ForEachFrame(for_each_id="push_chunks", for_each_execution_id=execution_id)


@pytest.mark.parametrize("item_name", ["current_chunk", "current_story", "supplier_row"])
def test_the_item_is_readable_under_any_configured_name(item_name):
    chunks = [[{"sku": "A1"}], [{"sku": "A2"}]]
    state = {"item_chunks": chunks, "workflow_data": {}}
    frame = _frame()

    step = select_items(_for_each(item_name), frame, state)

    assert step.failure is None
    assert frame.selected_count == 2
    assert _resolve_state_path(_checkpoint(state, step.updates), item_name) == chunks[0]


def test_the_index_is_published_beside_the_item():
    state = {"item_chunks": ["a", "b"], "workflow_data": {}}
    item, frame = _for_each("chunk", index_name="position"), _frame()
    state = _checkpoint(state, select_items(item, frame, state).updates)
    frame.item_index = 1
    state = _checkpoint(state, publish_item(item, frame, state).updates)
    assert (_resolve_state_path(state, "chunk"), _resolve_state_path(state, "position")) == ("b", 1)


def test_the_item_name_cannot_overwrite_trusted_runtime_context():
    from core.application.orchestration.runtime_identity import runtime_identity_from_state

    state = {
        "items": [{"project_id": "project-victim", "test_mode": True}],
        "project_id": "project-a",
        "trusted_runtime_context": {"project_id": "project-a", "test_mode": False},
        "workflow_data": {},
    }

    step = select_items(_for_each("trusted_runtime_context", over="items"), _frame(), state)
    merged = {**state, **step.updates}

    assert step.failure is None
    assert step.updates.get("trusted_runtime_context") is None
    identity = runtime_identity_from_state(merged)
    assert identity.project_id == "project-a"
    assert identity.test_mode is False


def test_the_next_item_comes_from_the_items_fixed_at_entry():
    chunks = [[{"sku": "A1"}], [{"sku": "A2"}]]
    state = {"item_chunks": chunks, "workflow_data": {}}
    item, frame = _for_each("current_chunk"), _frame()

    state = _checkpoint(state, select_items(item, frame, state).updates)
    state["item_chunks"] = [[{"sku": "CHANGED"}]]
    frame.item_index = 1
    state = _checkpoint(state, publish_item(item, frame, state).updates)

    assert _resolve_state_path(state, "current_chunk") == chunks[1]


def test_a_new_execution_selects_its_items_afresh():
    state = {"item_chunks": [[{"sku": "A1"}]], "workflow_data": {}}
    select_items(_for_each("current_chunk"), _frame(), state)

    state["item_chunks"] = [[{"sku": "B1"}], [{"sku": "B2"}]]
    reentry = _frame("fe-2")
    step = select_items(_for_each("current_chunk"), reentry, state)

    assert (reentry.selected_count, reentry.item_index) == (2, 0)
    assert _resolve_state_path(_checkpoint(state, step.updates), "current_chunk") == [{"sku": "B1"}]


def test_the_frame_survives_a_checkpoint_round_trip():
    state = {"item_chunks": [[{"sku": "A1"}], [{"sku": "A2"}]], "workflow_data": {}}
    frame = _frame()
    select_items(_for_each("current_chunk"), frame, state)

    restored = load_frame(frame.dump())

    assert isinstance(restored, ForEachFrame)
    assert (restored.selected_count, restored.digest) == (frame.selected_count, frame.digest)


def test_an_empty_pass_clears_the_item_under_the_configured_name():
    state = {"item_chunks": [[{"sku": "A1"}]], "workflow_data": {}}
    state = _checkpoint(state, select_items(_for_each("current_chunk"), _frame(), state).updates)
    assert _resolve_state_path(state, "current_chunk") is not None

    frame = _frame()
    step = select_items(_for_each("current_chunk"), frame, {**state, "item_chunks": []})

    assert frame.selected_count == 0
    assert _resolve_state_path(_checkpoint(state, step.updates), "current_chunk") is None


def test_a_malformed_source_selector_fails_closed():
    step = select_items(
        _for_each("current_chunk", over="rows[abc]"), _frame(), {"rows": [[{"sku": "A1"}]]}
    )

    assert step.failure is not None
    assert step.failure["code"] == "invalid_input_selector"
    assert step.failure["selector"] == "state.rows[abc]"
    assert step.failure["workflow_data"] == {}
    assert any("invalid_input_selector" in entry for entry in step.journal)


# --- authoring-time validation of the item name -------------------------------


class _WorkflowOnlyDB:
    def __init__(self, workflow: dict):
        self._workflow = workflow

    def get_workflow(self, workflow_id: str):
        if str(self._workflow.get("workflow_id") or "") != str(workflow_id or ""):
            return None
        return self._workflow


def _error_codes(validation: dict) -> set[str]:
    return {
        str(item.get("code") or "").strip()
        for item in (validation.get("errors") or [])
        if isinstance(item, dict)
    }


def _looping_workflow(item_name: str) -> dict:
    return {
        "workflow_id": "wf-loop-item-name",
        "settings": {
            "execution_graph": {
                "schema_version": 1,
                "entry": node("seed"),
                "edges": [
                    edge("seed", node("push_chunks", "for_each")),
                    edge(
                        "push",
                        {"kind": "for_each_exit", "for_each_id": "push_chunks", "lane_id": None},
                    ),
                    edge("push_chunks", node("done"), source_kind="for_each"),
                ],
            }
        },
        "groups": [],
        "for_each": [_for_each(item_name)],
        "steps": [
            {
                "step_id": "seed",
                "step_kind": "set_variable",
                "override": {"set_variable": {"target": "state.item_chunks", "value": []}},
            },
            {
                "step_id": "push",
                "step_kind": "set_variable",
                "for_each_id": "push_chunks",
                "override": {"set_variable": {"target": "state.pushed", "value": True}},
            },
            {"step_id": "done", "step_kind": "stop_success"},
        ],
    }


@pytest.mark.parametrize("item_name", ["current_chunk", "current_story", "supplier_row"])
def test_valid_item_names_are_accepted(item_name):
    workflow = _looping_workflow(item_name)
    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])
    codes = _error_codes(validation)
    assert "workflow.for_each.item_name_invalid" not in codes
    assert "workflow.for_each_name_reserved" not in codes


@pytest.mark.parametrize(
    ("item_name", "code"),
    [
        ("current chunk", "workflow.for_each.item_name_invalid"),
        ("chunk.row", "workflow.for_each.item_name_invalid"),
        ("9chunk", "workflow.for_each.item_name_invalid"),
        ("workflow_id", "workflow.for_each_name_reserved"),
        ("project_config", "workflow.for_each_name_reserved"),
    ],
)
def test_invalid_item_names_are_rejected_at_validation(item_name, code):
    workflow = _looping_workflow(item_name)
    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])
    assert code in _error_codes(validation)


# --- bounded checkpoint messages across items --------------------------------


def test_item_messages_are_coalesced_across_500_items():
    """Each item re-runs the body; only the newest message of each body step is kept."""
    from core.application.orchestration.for_each_runtime import compact_item_messages

    previous_step = {"step_id": "prepare", "from_agent": "a", "skill": "k"}
    messages = [previous_step]
    for item in range(500):
        for step_id in ("send", "record"):
            messages.append(
                {
                    "step_id": step_id,
                    "from_agent": "connector_runtime",
                    "skill": "connector",
                    "artifacts": {"item": item},
                }
            )
    compacted = compact_item_messages(messages, {"send", "record"})

    assert compacted is not None
    assert compacted[0] == previous_step
    assert [message["step_id"] for message in compacted[1:]] == ["send", "record"]
    assert {message["artifacts"]["item"] for message in compacted[1:]} == {499}


def test_messages_outside_the_body_are_never_coalesced():
    from core.application.orchestration.for_each_runtime import compact_item_messages

    messages = [{"step_id": "s", "from_agent": "a", "skill": "k"}] * 3

    assert compact_item_messages(messages, {"other"}) is None
