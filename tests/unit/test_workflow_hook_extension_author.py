"""A hook in an installed extension's workflow is visible to its binding service.

An installed extension workflow's platform row is a projection anchor without
steps; the steps live in the projection. The binding service reads the author
the way every API read path does, so the hook it declares is found and its owner
is the install and the workflow's local id.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, ClassVar

import core.application.workflows.workflow_hook_binding_service as service_module
from core.application.workflows.workflow_hook_binding_service import WorkflowHookBindingService
from tests.unit.test_workflow_hook_contract import declaration

INSTALL_ID = "145d0382-5752-48f0-979c-1dc0d6e7ad89"
PROJECTED_ID = f"extension.{INSTALL_ID}.prepare_order"


def _projection() -> dict[str, Any]:
    return {
        "install_id": INSTALL_ID,
        "release_id": "release-1",
        "artifact_id": "prepare_order",
        "artifact_kind": "workflow",
        "projection": {
            "workflow_id": "prepare_order",
            "display_name": "Prepare order",
            "enabled": True,
            "settings": {},
            "steps": [
                {
                    "step_id": "adjust_buyer",
                    "step_kind": "invoke_workflow",
                    "override": {"invoke_workflow": {"hook": declaration()}},
                },
                {"step_id": "done", "step_kind": "stop_success"},
            ],
            "groups": [],
            "for_each": [],
        },
    }


class _Store:
    def __init__(self) -> None:
        self.extension_runtime_store = SimpleNamespace(
            list_projections=lambda **_kwargs: [_projection()]
        )

    @staticmethod
    def get_workflow(workflow_id: str) -> dict[str, Any] | None:
        if workflow_id != PROJECTED_ID:
            return None
        # The anchor: no steps of its own.
        return {
            "workflow_id": PROJECTED_ID,
            "projection_mode": "derived",
            "source_install_id": INSTALL_ID,
            "local_workflow_id": PROJECTED_ID,
            "steps": [],
        }

    @staticmethod
    def list_workflows() -> list[dict[str, Any]]:
        return []

    @staticmethod
    def list_project_workflows(**_kwargs: Any) -> list[dict[str, Any]]:
        return []

    @contextmanager
    def _transaction(self):
        yield SimpleNamespace(connection=object())


class _NoBindings:
    seen_owners: ClassVar[list[Any]] = []

    def __init__(self, _connection: Any) -> None:
        pass

    def active(self, owner: Any, hook_ids: list[str]) -> dict[str, Any]:
        self.seen_owners.append(owner)
        return {}

    def binding_revision(self, _owner: Any, _hook_id: str) -> int:
        return 0


def test_a_hook_of_an_installed_extension_workflow_is_described(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "WorkflowHookBindingRepository", _NoBindings)
    monkeypatch.setattr(_NoBindings, "seen_owners", [])

    hooks = WorkflowHookBindingService(_Store(), account_id="default").describe(
        PROJECTED_ID, "project-1"
    )

    assert [hook["hook_id"] for hook in hooks] == ["before_invoice"]
    assert hooks[0]["step_id"] == "adjust_buyer"
    assert hooks[0]["binding"] is None
    owner = _NoBindings.seen_owners[0]
    assert owner.source_install_id == INSTALL_ID
    assert owner.project_id == "project-1"


def test_an_unknown_workflow_is_still_not_found(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "WorkflowHookBindingRepository", _NoBindings)

    try:
        WorkflowHookBindingService(_Store(), account_id="default").describe("missing", "p")
    except service_module.HookBindingError as exc:
        assert "workflow.not_found" in str(exc.issues if hasattr(exc, "issues") else exc)
    else:  # pragma: no cover - the call must refuse
        raise AssertionError("an unknown workflow was described")
