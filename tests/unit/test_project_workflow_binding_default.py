"""Linking workflows must not silently reset someone else's operator default.

Regression for the workflow E2E ledger finding F28. `PUT /api/projects/{id}/workflows`
passed `body.operator_default_workflow_id or ""` straight through, so a request that
simply did not mention the field cleared it. The sibling endpoint
`PUT /api/projects/{id}` already got this right — it re-seeds the existing value when
the key is absent — so the two disagreed about the same field, and linking a workflow
wiped the project's operator default.

Omitting the field now preserves the current value; sending an explicit empty string
still clears it.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models_api_inputs import ProjectWorkflowBindingsInput
from core.application.management.command_services import ProjectCommandService
from core.application.management.errors import ServiceUnavailableError
from core.application.management.requests import ProjectWorkflowSelectionRequest
from core.application.management.scope import ScopeContext


def _service(existing_default: str) -> tuple[ProjectCommandService, MagicMock]:
    bl = MagicMock()
    bl.get_project.return_value = {"id": 7, "name": "demo", "account_id": DEFAULT_ACCOUNT_ID}
    bl.get_project_workflows.return_value = [
        {"workflow_id": "wf-1", "is_default": existing_default == "wf-1"},
        {"workflow_id": "wf-2", "is_default": existing_default == "wf-2"},
    ]
    bl.list_workflow_choices.return_value = [{"workflow_id": "wf-1"}, {"workflow_id": "wf-2"}]
    bl.get_workflow.side_effect = lambda workflow_id: {"workflow_id": workflow_id}
    service = ProjectCommandService(
        get_logger=lambda: bl,
        resolve_workflow_selection=lambda selected, project_workflows=None: (
            selected,
            "explicit",
        ),
    )
    service._list_workflow_choices = MagicMock(  # type: ignore[method-assign]
        return_value=[{"workflow_id": "wf-1"}, {"workflow_id": "wf-2"}]
    )
    return service, bl


def _scope() -> ScopeContext:
    return ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin")


def _persisted_default(bl: MagicMock) -> str:
    return bl.save_project_workflows.call_args.kwargs["operator_default_workflow_id"]


# ---------------------------------------------------------------------------
# The request model has to be able to say "I did not mention this"
# ---------------------------------------------------------------------------


def test_an_omitted_default_is_distinguishable_from_an_explicit_clear():
    omitted = ProjectWorkflowSelectionRequest(workflow_ids=["wf-1"])
    cleared = ProjectWorkflowSelectionRequest(
        workflow_ids=["wf-1"], operator_default_workflow_id=""
    )

    assert omitted.operator_default_workflow_id is None
    assert cleared.operator_default_workflow_id == ""


def test_the_input_model_carries_the_same_distinction():
    omitted = ProjectWorkflowBindingsInput(workflow_ids=["wf-1"])
    cleared = ProjectWorkflowBindingsInput(workflow_ids=["wf-1"], operator_default_workflow_id="")

    assert omitted.operator_default_workflow_id is None
    assert cleared.operator_default_workflow_id == ""


# ---------------------------------------------------------------------------
# What the service does with each
# ---------------------------------------------------------------------------


def test_omitting_the_default_preserves_the_current_one():
    """Linking a workflow must not reset a default someone else set."""
    service, bl = _service("wf-2")

    service.update_project_workflows(
        7,
        ProjectWorkflowBindingsInput(workflow_ids=["wf-1", "wf-2"]),
        scope=_scope(),
    )

    assert _persisted_default(bl) == "wf-2"


def test_an_explicit_empty_string_still_clears_the_default():
    service, bl = _service("wf-2")

    service.update_project_workflows(
        7,
        ProjectWorkflowBindingsInput(
            workflow_ids=["wf-1", "wf-2"], operator_default_workflow_id=""
        ),
        scope=_scope(),
    )

    assert _persisted_default(bl) == ""


def test_an_explicit_value_sets_the_default():
    service, bl = _service("wf-2")

    result = service.update_project_workflows(
        7,
        ProjectWorkflowBindingsInput(
            workflow_ids=["wf-1", "wf-2"], operator_default_workflow_id="wf-1"
        ),
        scope=_scope(),
    )

    assert _persisted_default(bl) == "wf-1"
    assert result.workflow_ids == ["wf-1", "wf-2"]
    assert result.workflow_ids == [row["workflow_id"] for row in result.project_workflows]


def test_valid_authoritative_update_omits_stale_binding_and_returns_canonical_ids():
    service, bl = _service("wf-1")
    service._list_workflow_choices.return_value = [{"workflow_id": "wf-new"}]
    bl.get_workflow.side_effect = lambda workflow_id: {"workflow_id": workflow_id}
    bl.get_project_workflows.return_value = [
        {"workflow_id": "wf-disabled", "enabled": False, "is_default": True},
        {"workflow_id": "wf-new", "enabled": True, "is_default": True},
    ]

    result = service.update_project_workflows(
        7,
        ProjectWorkflowBindingsInput(
            workflow_ids=["wf-new"], operator_default_workflow_id="wf-new"
        ),
        scope=_scope(),
    )

    assert bl.save_project_workflows.call_args.args[1] == ["wf-new"]
    assert result.workflow_ids == ["wf-new"]
    assert result.operator_default_workflow_id == "wf-new"
    assert [row["workflow_id"] for row in result.project_workflows] == [
        "wf-disabled",
        "wf-new",
    ]


@pytest.mark.parametrize("existing_default", ["", "wf-1"])
def test_omitting_the_default_when_there_is_nothing_to_preserve_is_a_no_op(existing_default):
    service, bl = _service(existing_default)

    service.update_project_workflows(
        7,
        ProjectWorkflowBindingsInput(workflow_ids=["wf-1", "wf-2"]),
        scope=_scope(),
    )

    assert _persisted_default(bl) == existing_default


# ---------------------------------------------------------------------------
# Failing to read the current default must not read as "there is none"
# ---------------------------------------------------------------------------


def test_a_failed_read_of_the_current_default_does_not_clear_it():
    """Swallowing the error and substituting "" deletes someone else's default."""
    service, bl = _service("wf-2")
    bl.get_project_workflows.side_effect = RuntimeError("connection reset")

    _raises_input_183_1 = ProjectWorkflowBindingsInput(workflow_ids=["wf-1", "wf-2"])
    _raises_input_183_2 = _scope()
    with pytest.raises(ServiceUnavailableError) as excinfo:
        service.update_project_workflows(7, _raises_input_183_1, scope=_raises_input_183_2)

    assert "default" in str(excinfo.value).lower()
    bl.save_project_workflows.assert_not_called()


def test_a_store_without_the_getter_does_not_clear_the_default_either():
    service, bl = _service("wf-2")
    del bl.get_project_workflows

    _raises_input_198_1 = ProjectWorkflowBindingsInput(workflow_ids=["wf-1", "wf-2"])
    _raises_input_198_2 = _scope()
    with pytest.raises(ServiceUnavailableError):
        service.update_project_workflows(7, _raises_input_198_1, scope=_raises_input_198_2)

    bl.save_project_workflows.assert_not_called()


def test_an_explicit_value_is_persisted_without_reading_the_current_default():
    """Nothing to preserve, so the preservation read must not run at all.

    The result view reads the bindings back afterwards, which still fails on a
    broken store — but by then the write has happened with the explicit value,
    which is what this pins.
    """
    service, bl = _service("wf-2")
    bl.get_project_workflows.side_effect = RuntimeError("connection reset")

    with contextlib.suppress(Exception):
        service.update_project_workflows(
            7,
            ProjectWorkflowBindingsInput(
                workflow_ids=["wf-1", "wf-2"], operator_default_workflow_id="wf-1"
            ),
            scope=_scope(),
        )

    assert _persisted_default(bl) == "wf-1"


def _refusing_service(existing_default: str) -> tuple[ProjectCommandService, MagicMock]:
    """A service whose resolver refuses every default, as for a disabled workflow."""
    service, bl = _service(existing_default)

    def refuse(selected, project_workflows=None):
        raise ValueError(f"Workflow '{selected}' is disabled")

    service._resolve_workflow_selection = refuse  # type: ignore[assignment]
    return service, bl


def test_linking_keeps_the_current_default_without_rechecking_it():
    """A default disabled later must not stop every other workflow from being linked."""
    service, bl = _refusing_service("wf-2")

    service.update_project_workflows(
        7,
        ProjectWorkflowBindingsInput(workflow_ids=["wf-1", "wf-2"]),
        scope=_scope(),
    )

    assert _persisted_default(bl) == "wf-2"


def test_a_kept_default_that_is_no_longer_linked_is_dropped():
    service, bl = _refusing_service("wf-2")

    service.update_project_workflows(
        7,
        ProjectWorkflowBindingsInput(workflow_ids=["wf-1"]),
        scope=_scope(),
    )

    assert _persisted_default(bl) == ""


def test_choosing_a_disabled_workflow_as_the_default_is_still_refused():
    from core.application.management.errors import ServiceValidationError

    service, _bl = _refusing_service("")
    bindings = ProjectWorkflowBindingsInput(
        workflow_ids=["wf-1"], operator_default_workflow_id="wf-1"
    )
    scope = _scope()

    with pytest.raises(ServiceValidationError, match="disabled"):
        service.update_project_workflows(7, bindings, scope=scope)
