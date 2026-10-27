"""A terminal node must not make silently skipped user work look reachable."""

import copy

import pytest
from fastapi import HTTPException

from core.application.orchestration.run_cursor import (
    TRANSITION_STOP,
    FrozenDefinition,
    RunCursor,
    begin_step,
    resolve_after_step,
    resolve_entry,
)
from core.application.registry.contract_validation import validate_workflow_readiness
from core.application.workflows.execution_graph import Outcome
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    apply_workflow_changes,
    definition_from_workflow,
)
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from core.entrypoints.admin._readiness import enforce_enabled_save_validation
from tests.unit.test_contract_validation import _WorkflowOnlyDB
from tests.unit.test_workflow_definition_service import _Store, runnable
from tests.unit.test_workflow_definition_validation import (
    codes,
    edge,
    exit_ref,
    in_group,
    lanes_group,
    ref,
    sequence_group,
    step,
)


def stopped_workflow(kind="stop_success", outcome="error"):
    workflow = runnable()
    workflow["steps"] = [
        step("end", kind, error_policy={"on_error": "route_to_step"}),
        step(
            "user_work", "set_variable", override={"set_variable": {"assignments": {"ran": True}}}
        ),
        step("final", "stop_success"),
    ]
    workflow["settings"]["execution_graph"] = {
        "schema_version": 1,
        "entry": ref("end"),
        "edges": [edge("end", ref("user_work"), outcome), edge("user_work", ref("final"))],
    }
    return workflow


@pytest.mark.parametrize("kind", ["stop_success", "stop_fail"])
@pytest.mark.parametrize("outcome", ["success", "error"])
def test_stop_cannot_hide_skipped_steps_behind_any_authored_edge(kind, outcome):
    workflow = stopped_workflow(kind, outcome)
    definition = definition_from_workflow(workflow)
    # A Stop stays terminal even for persisted definitions authored before the fix.
    frozen = FrozenDefinition(definition)
    cursor = begin_step(RunCursor(), resolve_entry(frozen).step_id)
    assert resolve_after_step(frozen, cursor, Outcome(outcome)).kind == TRANSITION_STOP
    found = codes(validate_workflow_definition(definition))
    assert "workflow.graph.outcome_unsupported" in found
    assert "workflow.graph.unreachable" in found


@pytest.mark.parametrize("kind", ["stop_success", "stop_fail"])
def test_enabled_edit_and_activation_refuse_terminal_error_edge(kind):
    workflow = stopped_workflow(kind)
    store = _Store(workflow)
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            author="test",
            operations=[
                {
                    "op": "update_step_config",
                    "step_id": "user_work",
                    "patch": {"display_name": "User work"},
                },
            ],
        )
    assert "workflow.graph.outcome_unsupported" in codes(raised.value.issues)
    assert store.saves == []
    assert store.workflows["wf"] == workflow
    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf")
    assert validation["valid"] is False
    assert "workflow.graph.outcome_unsupported" in codes(validation["errors"])
    with pytest.raises(HTTPException) as rejected:
        enforce_enabled_save_validation(
            entity_type="workflow",
            entity_id="wf",
            action="enable",
            validation=validation,
            final_enabled=True,
        )
    assert rejected.value.status_code == 400


def appended_operations(kind):
    if kind == "step":
        return [
            {"op": "create_step", "step": step("new_work")},
            {"op": "connect", "edge": edge("new_work", ref("end"))},
        ]
    group = (
        sequence_group("new_group", "body")
        if kind == "loop"
        else lanes_group("new_group", {"lane": "body"})
    )
    lane = None if kind == "loop" else "lane"
    return [
        {"op": "create_group", "group": group},
        {"op": "create_step", "step": in_group(step("body"), "new_group", lane)},
        {"op": "connect", "edge": edge("body", exit_ref("new_group", lane))},
        {"op": "connect", "edge": edge("new_group", ref("end"), source_kind="group")},
    ]


@pytest.mark.parametrize("kind", ["step", "loop", "parallel"])
@pytest.mark.parametrize("enabled", [False, True])
def test_appending_nodes_does_not_rewire_stop_and_never_reports_valid(kind, enabled):
    workflow = runnable()
    workflow["enabled"] = enabled
    store = _Store(workflow)
    operations = appended_operations(kind)
    if enabled:
        with pytest.raises(WorkflowDefinitionChangeError) as raised:
            apply_workflow_changes(
                store, "wf", expected_revision=3, author="test", operations=operations
            )
        assert "workflow.graph.unreachable" in codes(raised.value.issues)
        assert store.saves == []
        assert store.workflows["wf"] == workflow
    else:
        result = apply_workflow_changes(
            store, "wf", expected_revision=3, author="test", operations=operations
        )
        assert not result.valid
        assert "workflow.graph.unreachable" in codes(result.issues)
        assert result.workflow["settings"]["execution_graph"]["entry"] == ref("a")
        assert result.workflow["enabled"] is False


def test_insert_between_runs_new_step_before_stop_even_when_appended_in_storage_order():
    store = _Store(runnable())
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        author="test",
        operations=[
            {
                "op": "insert_step_between",
                "source": ref("a"),
                "outcome": {"name": "success"},
                "step": step("user_work"),
            },
        ],
    )
    assert result.valid
    assert [item["step_id"] for item in result.workflow["steps"]] == ["a", "end", "user_work"]
    definition = FrozenDefinition(definition_from_workflow(result.workflow))
    cursor = begin_step(RunCursor(), resolve_entry(definition).step_id)
    assert resolve_after_step(definition, cursor, Outcome("success")).step_id == "user_work"
    begin_step(cursor, "user_work")
    assert resolve_after_step(definition, cursor, Outcome("success")).step_id == "end"


@pytest.mark.parametrize("kind", ["stop_success", "stop_fail"])
def test_existing_stop_policy_does_not_require_an_impossible_error_edge(kind):
    workflow = stopped_workflow(kind)
    definition = definition_from_workflow(workflow)
    definition["steps"] = copy.deepcopy(definition["steps"][:1])
    definition["execution_graph"]["edges"] = []
    assert validate_workflow_definition(definition) == []
