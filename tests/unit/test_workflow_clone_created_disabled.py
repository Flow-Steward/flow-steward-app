"""A clone is always created disabled (the owner's decision, 2026-09-25).

A clone into another account or project can miss what the source had -- a tool, a
connection -- and it used to take the source's enabled state with no readiness check,
so it could run enabled and invalid. Like an import, it starts disabled, and its author
enables it; the enable guard then names any errors."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import core.entrypoints.admin.workflows_routes_write as routes
from core.entrypoints.admin.workflows_models import WorkflowCloneRequest


def _clone(
    body: dict[str, object], *, source_override=None, real_policy=False, capture=None
) -> tuple[dict[str, object], MagicMock]:
    source = {"workflow_id": "source_wf", "enabled": True, "_phases": []}
    if source_override is not None:
        source = source_override
    db = MagicMock()
    if capture is not None:
        capture.append(db)
    db.get_workflow.side_effect = lambda workflow_id: source if workflow_id == "source_wf" else None
    scope = SimpleNamespace(account_id="account-1", project_id=None, actor_name="author")
    portability = SimpleNamespace(assert_clone_allowed=lambda _workflow_id: None)
    if real_policy:
        from core.application.workflows.workflow_portability import WorkflowPortabilityService

        portability = WorkflowPortabilityService(db)
    with (
        patch.object(routes, "_request_scope", return_value=scope),
        patch.object(routes, "require_default_registry_account"),
        patch.object(routes, "_get_db", return_value=db),
        patch.object(routes, "_resolve_workflow_scope_target", return_value=(source, scope)),
        patch.object(routes, "_enforce_project_scope", return_value=None),
        patch.object(routes, "_get_workflow_portability_service", return_value=portability),
        patch.object(routes, "_bind_workflow_to_project_if_requested"),
        patch.object(routes, "_get_registry", return_value=MagicMock()),
    ):
        result = routes.clone_workflow(
            "source_wf", WorkflowCloneRequest.model_validate(body), MagicMock()
        )
    return result, db


def test_the_clone_route_says_the_clone_is_disabled() -> None:
    result, db = _clone({"workflow_id": "cloned_wf"})

    assert result["enabled"] is False
    assert result["message"] == "Cloned as disabled — enable it when it is ready."
    assert db.clone_workflow.call_args.kwargs["target_workflow_id"] == "cloned_wf"


def test_asking_for_an_enabled_clone_still_creates_it_disabled() -> None:
    result, db = _clone({"workflow_id": "cloned_wf", "enabled": True})

    assert result["enabled"] is False
    assert "enabled" not in db.clone_workflow.call_args.kwargs  # nothing to pass on


@pytest.mark.parametrize("mode", ["production", "development"])
@pytest.mark.parametrize("has_hook", [False, True])
def test_public_clone_requires_development_only_for_hook_declarations(monkeypatch, mode, has_hook):
    from fastapi import HTTPException

    from tests._workflow_definitions import linear_workflow
    from tests.unit.test_workflow_hook_contract import declaration

    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    steps = []
    if has_hook:
        hook = declaration(
            input_schema={"type": "object", "properties": {}},
            input_mapping={},
            mutable_fields=[],
            additional_outputs={},
        )
        steps.append(
            {
                "step_id": "before",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"hook": hook}},
            }
        )
    source = linear_workflow(*steps, workflow_id="source_wf", enabled=True)
    stores = []
    if mode == "production" and has_hook:
        with (
            patch(
                "core.application.workflows.workflow_canvas_annotation_service.WorkflowCanvasAnnotationService.clone_annotations"
            ) as annotations,
            pytest.raises(HTTPException) as raised,
        ):
            _clone(
                {"workflow_id": "new_identity"},
                source_override=source,
                real_policy=True,
                capture=stores,
            )
        assert raised.value.status_code == 403
        assert raised.value.detail["error"] == "workflow.hook.declaration_requires_development"
        issues = raised.value.detail["errors"]
        assert any(
            issue.get("code") == "workflow.hook.declaration_requires_development"
            and issue.get("severity") == "error"
            and issue.get("path")
            for issue in issues
        )
        stores[0].clone_workflow.assert_not_called()
        annotations.assert_not_called()
        stores[0]._transaction.assert_not_called()
    else:
        result, db = _clone(
            {"workflow_id": "new_identity"},
            source_override=source,
            real_policy=True,
            capture=stores,
        )
        assert result["enabled"] is False
        assert db.clone_workflow.call_count == 1
        assert db.clone_workflow.call_args.kwargs["target_workflow_id"] == "new_identity"
        assert "enabled" not in db.clone_workflow.call_args.kwargs


def test_public_metadata_create_cannot_carry_hook_steps():
    from pydantic import ValidationError

    from core.entrypoints.admin.workflows_models import WorkflowSave
    from tests.unit.test_workflow_hook_contract import declaration

    payload = {
        "workflow_id": "metadata_backdoor",
        "display_name": "Metadata backdoor",
        "steps": [
            {
                "step_id": "before",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"hook": declaration()}},
            }
        ],
    }
    with pytest.raises(ValidationError) as raised:
        WorkflowSave.model_validate(payload)
    assert any(
        issue["type"] == "workflow_definition_field_unsupported" for issue in raised.value.errors()
    )


def test_public_clone_refuses_a_failed_declaration_guard_read(monkeypatch):
    from contextlib import nullcontext
    from copy import deepcopy

    from fastapi import HTTPException

    from core.application.registry.db_store import RegistryDBStore
    from core.application.workflows.workflow_portability import WorkflowPortabilityService
    from tests._workflow_definitions import linear_workflow
    from tests.unit.test_workflow_hook_contract import declaration

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "production")
    source = linear_workflow(
        {
            "step_id": "before",
            "step_kind": "invoke_workflow",
            "override": {"invoke_workflow": {"hook": declaration()}},
        },
        workflow_id="source_wf",
        enabled=False,
    )
    persisted, history_writes = {}, []
    fault = {"armed": False, "failures": 0, "later_reads": 0}
    db = RegistryDBStore("postgres://unused")

    def get_workflow(workflow_id, **_kwargs):
        if workflow_id == "source_wf":
            if fault["armed"]:
                fault["armed"] = False
                fault["failures"] += 1
                raise ConnectionError("one-shot declaration source read failure")
            if fault["failures"]:
                fault["later_reads"] += 1
            return deepcopy(source)
        return deepcopy(persisted.get(workflow_id))

    def save(payload, **_kwargs):
        persisted[payload["workflow_id"]] = deepcopy(payload)
        history_writes.append(deepcopy(payload))
        return 1

    db.get_workflow = MagicMock(side_effect=get_workflow)
    db.save_workflow = MagicMock(side_effect=save)
    db.clone_workflow = MagicMock(wraps=db.clone_workflow)
    db._transaction = MagicMock(side_effect=lambda: nullcontext(MagicMock()))
    portability = WorkflowPortabilityService(db)
    export_policy = portability.export_policy

    def export_then_fail_next_source_read(workflow_id):
        result = export_policy(workflow_id)
        fault["armed"] = True
        return result

    monkeypatch.setattr(portability, "export_policy", export_then_fail_next_source_read)
    scope = SimpleNamespace(account_id="account-1", project_id=None, actor_name="author")
    bindings = MagicMock()
    monkeypatch.setattr(routes, "_request_scope", lambda _request: scope)
    monkeypatch.setattr(routes, "require_default_registry_account", lambda _scope: None)
    monkeypatch.setattr(routes, "_get_db", lambda: db)
    monkeypatch.setattr(
        routes, "_resolve_workflow_scope_target", lambda *_a: (deepcopy(source), scope)
    )
    monkeypatch.setattr(routes, "_enforce_project_scope", lambda *_a: None)
    monkeypatch.setattr(routes, "_get_workflow_portability_service", lambda _db: portability)
    monkeypatch.setattr(routes, "_bind_workflow_to_project_if_requested", bindings)
    monkeypatch.setattr(routes, "_get_registry", MagicMock())
    clone_request = WorkflowCloneRequest(workflow_id="new_identity")
    http_request = MagicMock()
    with (
        patch("core.entrypoints.admin.deps.get_artifact_plane", return_value=MagicMock()),
        patch(
            "core.application.workflows.workflow_canvas_annotation_service.WorkflowCanvasAnnotationService.clone_annotations",
            side_effect=lambda *_a, **_k: nullcontext({}),
        ) as annotations,
        pytest.raises(HTTPException) as raised,
    ):
        routes.clone_workflow("source_wf", clone_request, http_request)
    assert raised.value.status_code == 503
    assert fault["failures"] == 1
    assert db.get_workflow("source_wf") == source
    assert fault["later_reads"] == 1
    annotations.assert_not_called()
    db.clone_workflow.assert_not_called()
    db._transaction.assert_not_called()
    db.save_workflow.assert_not_called()
    bindings.assert_not_called()
    assert persisted == {}
    assert history_writes == []
