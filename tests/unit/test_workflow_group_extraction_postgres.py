"""Extraction ownership and rollback against the disposable audit database."""

import pytest

from core.application.workflows.workflow_change_authorization import ChangeReferenceAccess
from core.application.workflows.workflow_group_extraction_service import GroupExtractionService
from tests.unit.test_workflow_group_extraction_structure import source_workflow


@pytest.fixture
def extraction(audit_db):
    store, conn, workflow_id, project_id = audit_db
    workflow = source_workflow(parallel=True)
    workflow.update(
        workflow_id=workflow_id,
        scope_kind="account",
        account_id="00000000-0000-0000-0000-000000000002",
    )
    workflow["settings"].pop("canvas_annotations", None)
    for step in workflow["steps"]:
        if step["step_kind"] == "http_request":
            step["override"] = {"http_request": {"url": "https://example.test/records"}}
    store.save_workflow(workflow)
    store.add_project_workflow_binding(str(project_id), workflow_id)
    account_id = str(store.get_workflow(workflow_id)["account_id"])
    service = GroupExtractionService(
        store,
        account_id=account_id,
        reference_access=ChangeReferenceAccess(
            **{key: lambda _value: True for key in ("agent", "workflow", "connection", "install")}
        ),
        project_visible=lambda project: project == str(project_id),
        reactions=lambda *_args: [],
        validate=lambda *_args: {"errors": []},
    )
    args = {
        "workflow_id": workflow_id,
        "group_id": "process",
        "child_workflow_id": workflow_id + "_child",
        "display_name": "Extracted",
        "expected_revision": 1,
    }
    try:
        yield service, store, conn, project_id, args
    finally:
        conn.execute(
            "DELETE FROM registry_workflows WHERE workflow_id = %s", (args["child_workflow_id"],)
        )


def test_extraction_persists_account_child_and_project_binding(extraction):
    service, store, conn, project_id, args = extraction
    preview = service.preview(**args)
    assert preview["can_convert"], preview["issues"]
    service.apply(**args, preview_digest=preview["preview_digest"], author="audit")
    child = store.get_workflow(args["child_workflow_id"])
    assert child["scope_kind"] == "account"
    assert str(child["account_id"]) == service.account_id
    binding = conn.execute(
        "SELECT enabled, deleted_at FROM project_workflows WHERE project_id = %s AND workflow_id = %s",
        (project_id, args["child_workflow_id"]),
    ).fetchone()
    assert binding == (True, None)
    assert store.get_workflow(args["workflow_id"])["groups"] == []


def test_binding_failure_rolls_back_both_real_definitions(extraction, monkeypatch):
    service, store, conn, project_id, args = extraction
    preview = service.preview(**args)
    assert preview["can_convert"], preview["issues"]
    original = store.get_workflow(args["workflow_id"])
    original_binding = store.add_project_workflow_binding

    def fail_after_binding(project, workflow):
        original_binding(project, workflow)
        raise RuntimeError("binding write failed")

    monkeypatch.setattr(store, "add_project_workflow_binding", fail_after_binding)
    with pytest.raises(RuntimeError, match="binding write failed"):
        service.apply(**args, preview_digest=preview["preview_digest"], author="audit")
    assert store.get_workflow(args["child_workflow_id"]) is None
    assert store.get_workflow(args["workflow_id"])["revision"] == original["revision"]
    assert store.get_workflow(args["workflow_id"])["groups"] == original["groups"]
    assert (
        conn.execute(
            "SELECT count(*) FROM project_workflows WHERE project_id = %s AND workflow_id = %s",
            (project_id, args["child_workflow_id"]),
        ).fetchone()[0]
        == 0
    )


def test_note_edit_after_preview_survives_conversion_without_invalidating_plan(extraction):
    from core.application.workflows.workflow_canvas_annotation_service import (
        WorkflowCanvasAnnotationService,
    )
    from tests.unit.test_workflow_canvas_annotations import NOTE

    service, store, _, _, args = extraction
    preview = service.preview(**args)
    assert preview["can_convert"], preview["issues"]
    WorkflowCanvasAnnotationService(store, None).save_note(
        workflow_id=args["workflow_id"],
        account_id=service.account_id,
        annotation=NOTE,
        expected_revision=1,
        expected_annotations_revision=0,
    )
    service.apply(**args, preview_digest=preview["preview_digest"], author="audit")
    parent = store.get_workflow(args["workflow_id"])
    assert parent["settings"]["canvas_annotations"] == [NOTE]
    assert parent["settings"]["canvas_annotations_revision"] == 1


def test_retained_output_conversion_uses_real_canonical_rows_and_persists_contracts(extraction):
    from tests.unit.test_workflow_group_extraction_retained_outputs import retained_source

    service, store, _, _, args = extraction
    original = store.get_workflow(args["workflow_id"])
    workflow, _ = retained_source()
    workflow.update(
        workflow_id=args["workflow_id"], account_id=original["account_id"], scope_kind="account"
    )
    workflow["settings"].pop("canvas_annotations", None)
    store.save_workflow(workflow, expected_revision=1)
    args = {**args, "expected_revision": 2}
    result = service.preview(**args)
    assert result["can_convert"], result["issues"]
    service.apply(**args, preview_digest=result["preview_digest"], author="audit")
    parent = store.get_workflow(args["workflow_id"])
    child = store.get_workflow(args["child_workflow_id"])
    assert parent["groups"] == []
    assert parent["settings"]["callable"]["expected_outputs"] == [
        {"name": "items", "type": "array", "required": True}
    ]
    assert child["settings"]["callable"]["expected_outputs"] == [
        {"name": "normalized", "type": "object", "required": False}
    ]
    invoke = next(step for step in parent["steps"] if step["step_kind"] == "invoke_workflow")
    assert invoke["override"]["invoke_workflow"]["output_mapping"] == {
        "normalized": "state.normalized"
    }


def test_a_hook_connected_in_a_project_cannot_move_into_a_new_child(extraction):
    from core.application.workflows.workflow_hook_binding_repository import (
        WorkflowHookBindingRepository,
    )
    from core.application.workflows.workflow_invocation_target import hook_owner_for
    from tests.unit.test_workflow_definition_snapshot_repository import _RealDictConnection
    from tests.unit.test_workflow_hook_contract import declaration

    service, store, conn, project_id, args = extraction
    source = store.get_workflow(args["workflow_id"])
    hook_step = {
        "step_id": "before",
        "step_kind": "invoke_workflow",
        "group_id": "process",
        "override": {"invoke_workflow": {"hook": declaration()}},
    }
    owner = hook_owner_for(source, account_id=service.account_id, project_id=str(project_id))
    repository = WorkflowHookBindingRepository(_RealDictConnection(conn))
    assert service._connected_hooks(source, [hook_step]) == []
    repository.save(
        owner,
        "before_invoice",
        expected_revision=0,
        target_workflow_id=args["workflow_id"],
        input_mapping={},
        output_mapping={},
        hook_contract_version=1,
        hook_semantic_digest="d",
    )
    assert service._connected_hooks(source, [hook_step]) == [("before_invoice", str(project_id))]
    # A cleared binding is no longer a connection that would be lost.
    repository.save(
        owner,
        "before_invoice",
        expected_revision=1,
        target_workflow_id=None,
        input_mapping={},
        output_mapping={},
        hook_contract_version=1,
        hook_semantic_digest="d",
    )
    assert service._connected_hooks(source, [hook_step]) == []


@pytest.mark.parametrize(
    "target",
    [
        "state.outputs.text",
        "outputs.text",
        "state.workflow_data.outputs.text",
        "state.workflow_data.text",
        "text",
    ],
)
def test_post_group_template_retains_required_greeting_and_typed_mappings(
    extraction, target, monkeypatch
):
    from core.application.registry.contract_validation import validate_workflow_readiness
    from core.application.registry.io_resolver import IOResolver
    from tests.unit.test_workflow_group_extraction_retained_outputs import greeting_source

    service, store, _, _, args = extraction
    workflow = greeting_source(target)
    workflow.update(
        workflow_id=args["workflow_id"], account_id=service.account_id, scope_kind="account"
    )
    store.save_workflow(workflow, expected_revision=1)
    args = {**args, "expected_revision": 2}
    service.validate = validate_workflow_readiness
    saved = store.get_workflow(args["workflow_id"])
    template = next(s for s in saved["steps"] if s["step_id"] == "greet")
    rows = IOResolver(store).get_step_declared_outputs(
        template, workflow_account_id=service.account_id
    )
    assert any(
        row["name"] == target.removeprefix("state.") and row["type"] == "text" for row in rows
    )
    preview = service.preview(**args)
    assert preview["can_convert"], preview["issues"]
    service.apply(**args, preview_digest=preview["preview_digest"], author="audit")
    parent = store.get_workflow(args["workflow_id"])
    child = store.get_workflow(args["child_workflow_id"])
    assert parent["settings"]["callable"] == saved["settings"]["callable"]
    retained_template = next(s for s in parent["steps"] if s["step_id"] == "greet")
    assert {k: v for k, v in retained_template.items() if k != "sort_order"} == {
        k: v for k, v in template.items() if k != "sort_order"
    }
    assert child["settings"]["callable"]["expected_outputs"] == [
        {"name": "normalized", "type": "object", "required": False}
    ]
    assert child["settings"]["callable"]["expected_inputs"] == [
        {"name": "source_person", "type": "object", "required": False}
    ]
    invoke = next(s for s in parent["steps"] if s["step_kind"] == "invoke_workflow")
    spec = invoke["override"]["invoke_workflow"]
    assert spec["input_mapping"] == {"source_person": "state.person"}
    assert spec["output_mapping"] == {"normalized": "state.normalized"}
    transform = next(s for s in child["steps"] if s["step_id"] == "fetch")
    assert transform["override"]["transform"]["input"]["source"] == "state.inputs.source_person"
    assert any(
        e["source"]["id"] == invoke["step_id"] and e["target"]["id"] == "greet"
        for e in parent["settings"]["execution_graph"]["edges"]
    )
    from core.application.orchestration.agents import common
    from core.application.orchestration.run_definition import clear_run_definition_cache

    monkeypatch.setattr(common, "emit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(common, "publish_agent_message", lambda *_args, **_kwargs: True)
    try:
        before, before_steps = _execute_stored_graph(saved)
        after, after_steps = _execute_stored_graph(parent, child=child)
        assert before == after == {"text": "Hello, Ada!"}
        assert before_steps == ["prepare", "fetch", "greet", "done"]
        assert after_steps == ["prepare", invoke["step_id"], "greet", "done"]
    finally:
        clear_run_definition_cache()


def _execute_stored_graph(workflow, *, inputs=None, child=None):
    """Real hub/builtin nodes; invoke transport is synchronously simulated.

    This exercises saved graph/state and boundary mapping parity, not worker jobs.
    """
    from core.application.orchestration.active_step import active_step
    from core.application.orchestration.control_flow_step_node import control_flow_step_node
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.orchestration.invoke_workflow_step_node_common import (
        _resolve_invoke_input,
    )
    from core.application.orchestration.invoke_workflow_step_node_results import (
        _set_nested_state_update,
    )
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from core.application.registry.workflow_contract import interface_from_definition
    from core.application.registry.workflow_contract_service import (
        WorkflowInterfaceService,
        collect_runtime_outputs,
    )
    from core.application.workflows.workflow_definition_service import definition_from_workflow
    from tests._step_run_state import prime
    from tests.unit.test_step_hub import _result, _step

    state = {
        **prime(definition_from_workflow(workflow)),
        "workflow_id": workflow["workflow_id"],
        "agent_messages": [],
        "inputs": inputs or {},
    }
    seen = []
    for _ in range(20):
        command = orchestrator_hub_node(state)
        state = _step(state, command)
        if command.goto in {"orchestrator_validation", "__end__"}:
            assert state["workflow_terminal"]["kind"] == "stop_success"
            outputs, artifacts = collect_runtime_outputs(
                state, interface=interface_from_definition(workflow)
            )
            assert artifacts == {}
            boundary = WorkflowInterfaceService().validate_outputs(
                interface_from_definition(workflow), state
            )
            assert boundary.ok, boundary.errors
            return outputs, seen
        current = active_step(state)
        seen.append(current.step_id)
        if command.goto == "invoke_workflow_step":
            assert child is not None
            spec = current.step["override"]["invoke_workflow"]
            values = {}
            for key, source in spec["input_mapping"].items():
                values[key], error = _resolve_invoke_input(state, source, key)
                assert not error
            outputs, child_seen = _execute_stored_graph(child, inputs=values)
            assert child_seen == ["fetch", "child_done"]
            assert outputs == {"normalized": {"name": "Ada"}}
            updates = {}
            for name, target in spec["output_mapping"].items():
                _set_nested_state_update(
                    state=state, updates=updates, state_target=target, value=outputs[name]
                )
            state = _result({**state, **updates}, actor="invoke_workflow")
        else:
            assert command.goto in {"execute_step", "control_flow_step"}
            update = (
                execute_step_node if command.goto == "execute_step" else control_flow_step_node
            )(state)
            assert update["agent_messages"][-1]["status"] == "pass", update
            state = {**state, **update}
    raise AssertionError("stored graph failed to terminate")


def test_both_runtime_workflow_data_aliases_pass_persisted_preview_readiness(extraction):
    from core.application.registry.contract_validation import validate_workflow_readiness
    from tests.unit.test_workflow_group_extraction_retained_outputs import greeting_source

    service, store, _, _, args = extraction
    service.validate = validate_workflow_readiness
    failures = {}
    for revision, target in enumerate(
        ["state.workflow_data.outputs.text", "state.workflow_data.text"], start=1
    ):
        workflow = greeting_source(target)
        workflow.update(
            workflow_id=args["workflow_id"], account_id=service.account_id, scope_kind="account"
        )
        store.save_workflow(workflow, expected_revision=revision)
        preview = service.preview(**{**args, "expected_revision": revision + 1})
        if not preview["can_convert"]:
            failures[target] = preview["issues"]
    assert failures == {}
