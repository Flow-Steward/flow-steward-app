"""A hook's new storage workflow is empty and ready to run at once (R2-11).

A hook is a place on the canvas for steps. The first step added to an empty
hook creates the per-project workflow that stores its steps: no steps nobody
added, only the hidden end the builder does not draw.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.application.orchestration.invoke_workflow_hook_runtime import holds_no_steps
from core.application.registry.contract_validation import validate_workflow_readiness
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.workflows.workflow_definition_snapshot import frozen_definition
from core.application.workflows.workflow_definition_validation import (
    validate_workflow_definition,
)
from core.application.workflows.workflow_hook_binding_service import (
    WorkflowHookBindingService,
    _storage_child,
    same_name_output_mapping,
)
from core.application.workflows.workflow_hook_contract import (
    HOOK_END_STEP_ID,
    HOOK_STORAGE_SETTING,
    is_hook_storage,
    parse_hook_declaration,
)
from core.infrastructure.db.logger_policy_projects_mixin import (
    PlatformLoggerPolicyProjectsMixin,
)
from tests.unit.test_contract_validation import _WorkflowOnlyDB
from tests.unit.test_workflow_hook_contract import declaration

HOOK = parse_hook_declaration(declaration())


def stored(child: dict) -> dict:
    """The child as the registry stores it: the graph lives in its settings."""
    return {**child, "settings": {**child["settings"], "execution_graph": child["execution_graph"]}}


def test_the_new_child_takes_the_hook_inputs_as_ordinary_fields() -> None:
    interface = WorkflowInterface.from_settings(
        _storage_child("actions", HOOK, "", "acc")["settings"]
    )

    assert interface.input_mode == "fields"
    assert [(row.name, row.type, row.required) for row in interface.expected_inputs] == [
        ("invoice_id", "number", True),
        ("company", "string", True),
        ("note", "string", False),
    ]
    # Every field the hook lets its steps write, none of them required: a
    # result no step sets falls back to the input (or the default).
    assert [(row.name, row.required) for row in interface.expected_outputs] == [
        ("company", False),
        ("note", False),
        ("skip_request", False),
        ("crm_id", False),
    ]


def test_the_new_child_is_empty_structurally_valid_and_runs_nothing() -> None:
    child = _storage_child("actions", HOOK, "", "acc")

    # Valid as stored (an enabled workflow is created only when it is) ...
    assert validate_workflow_definition(frozen_definition(stored(child))) == []
    readiness = validate_workflow_readiness(_WorkflowOnlyDB(stored(child)), "actions")
    assert readiness["valid"] is True, readiness["errors"]
    # ... with no Build the result / Return the result scaffolding nobody added.
    assert child["steps"] == [{"step_id": HOOK_END_STEP_ID, "step_kind": "stop_success"}]
    assert child["execution_graph"]["entry"] == {"kind": "step", "id": HOOK_END_STEP_ID}
    assert child["execution_graph"]["edges"] == []
    assert child["enabled"] is True
    # Holding nothing to run, the hook passes its inputs through and never runs it.
    assert holds_no_steps(child) is True


def test_a_step_in_the_hook_makes_it_runnable_with_unset_results_as_notes() -> None:
    child = _storage_child("actions", HOOK, "", "acc")
    child["steps"].insert(
        0,
        {
            "step_id": "rename",
            "step_kind": "set_variable",
            "override": {"set_variable": {"assignments": {"state.company": "Acme"}}},
        },
    )
    child["execution_graph"] = {
        "schema_version": 1,
        "entry": {"kind": "step", "id": "rename"},
        "edges": [
            {
                "source": {"kind": "step", "id": "rename"},
                "outcome": {"name": "success"},
                "target": {"kind": "step", "id": HOOK_END_STEP_ID},
            }
        ],
    }

    validation = validate_workflow_readiness(_WorkflowOnlyDB(stored(child)), "actions")

    assert validation["valid"] is True, validation["errors"]
    # note, skip_request and crm_id are set by no step: each keeps its input or
    # default, so that is a note, never a refusal to run.
    assert "workflow.callable.output_not_produced" in {
        warning["code"] for warning in validation["warnings"]
    }
    assert holds_no_steps(child) is False


def test_every_result_of_the_new_child_is_read_back_by_its_own_name() -> None:
    child = _storage_child("actions", HOOK, "", "acc")

    assert same_name_output_mapping(child, HOOK) == {
        "company": "company",
        "note": "note",
        "skip_request": "skip_request",
        "crm_id": "crm_id",
    }


def test_an_output_the_hook_cannot_take_stays_unmapped() -> None:
    child = _storage_child("actions", HOOK, "", "acc")
    child["settings"]["callable"]["expected_outputs"].append({"name": "invoice_id"})
    child["settings"]["callable"]["expected_outputs"].append({"name": "audit_trail"})

    mapping = same_name_output_mapping(child, HOOK)

    # invoice_id is a read-only hook input, audit_trail is no hook field at all.
    assert "invoice_id" not in mapping
    assert "audit_trail" not in mapping


def test_the_new_child_is_marked_as_hook_storage_and_kept_out_of_the_catalog() -> None:
    child = _storage_child(
        "actions", HOOK, "", "acc", author_workflow_id="invoice_flow", project_id="p-1"
    )

    assert child["settings"][HOOK_STORAGE_SETTING] == {
        "author_workflow_id": "invoice_flow",
        "hook_id": "before_invoice",
        "project_id": "p-1",
    }
    assert child["visible_in_catalog"] is False
    assert is_hook_storage(child) is True
    # The marker, not the reserved end step, says what a workflow is.
    unmarked = {**child, "settings": {"callable": child["settings"]["callable"]}}
    assert is_hook_storage(unmarked) is False
    assert holds_no_steps(unmarked) is False
    assert is_hook_storage({"workflow_id": "row", "hook_storage": True}) is True
    assert is_hook_storage({"settings": '{"hook_storage": {"hook_id": "x"}}'}) is True


def test_saving_a_project_selection_keeps_its_hook_storage_links() -> None:
    """The operator's selection never lists a hook's storage, so it never unlinks it."""
    cur = MagicMock()

    PlatformLoggerPolicyProjectsMixin._sync_project_workflows_cur(
        cur, project_id="p-1", project_workflow_ids=["order_flow"], operator_default_workflow_id=""
    )
    PlatformLoggerPolicyProjectsMixin._sync_project_workflows_cur(
        cur, project_id="p-1", project_workflow_ids=[], operator_default_workflow_id=""
    )

    removals = [
        call.args[0] for call in cur.execute.call_args_list if "deleted_at = NOW()" in call.args[0]
    ]
    assert len(removals) == 2
    assert all("settings ? 'hook_storage'" in sql for sql in removals)


def test_the_first_step_creates_the_empty_child_with_its_same_name_mapping() -> None:
    author = {
        "workflow_id": "invoice_flow",
        "account_id": "acc",
        "steps": [
            {
                "step_id": "before",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"hook": declaration()}},
            }
        ],
    }

    @contextmanager
    def transaction():
        yield SimpleNamespace(connection=None)

    db = SimpleNamespace(
        _transaction=transaction,
        get_workflow=lambda _workflow_id: None,
        add_project_workflow_binding=lambda *_args: None,
    )
    service = WorkflowHookBindingService(db, account_id="acc")
    saved: list = []
    created: list = []
    with (
        patch.object(service, "_author", return_value=author),
        patch.object(service, "_owner", return_value=object()),
        patch.object(service, "save", side_effect=lambda request: saved.append(request)),
        patch(
            "core.application.workflows.workflow_definition_service.create_workflow_with_definition",
            side_effect=lambda _db, child, **_kwargs: created.append(child),
        ),
    ):
        service.create_child_and_bind(
            owner_workflow_id="invoice_flow",
            project_id="p-1",
            hook_id="before_invoice",
            expected_revision=0,
            child_workflow_id="invoice_actions",
        )

    assert [step["step_id"] for step in created[0]["steps"]] == [HOOK_END_STEP_ID]
    assert created[0]["settings"][HOOK_STORAGE_SETTING] == {
        "author_workflow_id": "invoice_flow",
        "hook_id": "before_invoice",
        "project_id": "p-1",
    }
    assert saved[0].target_workflow_id == "invoice_actions"
    assert saved[0].output_mapping == {
        "company": "company",
        "note": "note",
        "skip_request": "skip_request",
        "crm_id": "crm_id",
    }
    assert saved[0].input_mapping == {
        "company": "company",
        "invoice_id": "invoice_id",
        "note": "note",
    }
