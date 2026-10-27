"""Preview/apply share one plan; neither half of an extraction may survive failure."""

import copy
from types import SimpleNamespace

import pytest

from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.workflow_change_authorization import ChangeReferenceAccess
from core.application.workflows.workflow_definition_service import definition_from_workflow
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from core.application.workflows.workflow_group_extraction_service import (
    GroupExtractionError,
    GroupExtractionService,
)
from tests.unit.test_workflow_definition_service import _Store
from tests.unit.test_workflow_group_extraction_bindings import source
from tests.unit.test_workflow_group_extraction_structure import source_workflow


class Store(_Store):
    def __init__(self):
        workflow = source()
        workflow["account_id"] = "account-a"
        super().__init__(workflow)
        self.bindings = []
        self.fail_parent = False

    def get_workflow(self, workflow_id, include_trashed=False):
        return super().get_workflow(workflow_id)

    def save_workflow(self, data, author="test", *, expected_revision=None, create_only=False):
        if create_only:
            if data["workflow_id"] in self.workflows:
                raise ValueError("already exists")
            self.workflows[data["workflow_id"]] = copy.deepcopy({**data, "revision": 1})
            return 1
        if self.fail_parent:
            raise RuntimeError("parent save failed")
        return super().save_workflow(data, author, expected_revision=expected_revision)

    def list_project_workflows(self, project_id=None, enabled_only=False):
        return [{"workflow_id": "wf", "project_id": "project-a", "enabled": True}]

    def add_project_workflow_binding(self, project_id, workflow_id):
        self.bindings.append((project_id, workflow_id))


@pytest.fixture
def service(monkeypatch):
    store = Store()
    monkeypatch.setattr(
        "core.application.workflows.workflow_group_extraction_service.IOResolver",
        lambda _db: SimpleNamespace(
            get_step_state_writes=lambda *_args, **_kwargs: [],
            get_step_outputs=lambda *_args: [],
            get_step_state_outputs=lambda *_args: [
                {"name": "response", "type": "object", "source": "http_request"}
            ],
            get_available_type_map_for_step=lambda *_args: {"state.items": "array"},
        ),
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_group_extraction_service.WorkflowPortabilityService",
        lambda _db: SimpleNamespace(assert_structure_change_allowed=lambda *_args, **_kwargs: None),
    )
    access = ChangeReferenceAccess(
        **{key: lambda _value: True for key in ("agent", "workflow", "connection", "install")}
    )
    return GroupExtractionService(
        store,
        account_id="account-a",
        reference_access=access,
        project_visible=lambda _project: True,
        reactions=lambda *_args: [],
        validate=lambda *_args: {"errors": []},
    ), store


def preview(service):
    return service.preview(
        workflow_id="wf",
        group_id="process",
        child_workflow_id="extracted",
        display_name="Extracted",
        expected_revision=3,
    )


def apply(service, result):
    return service.apply(
        workflow_id="wf",
        group_id="process",
        child_workflow_id="extracted",
        display_name="Extracted",
        expected_revision=3,
        preview_digest=result["preview_digest"],
        author="test",
    )


def test_preview_writes_nothing_and_apply_reuses_existing_definition_writers(service):
    service, store = service
    original = copy.deepcopy(store.workflows)
    result = preview(service)
    assert result["can_convert"]
    assert store.workflows == original
    assert not store.bindings
    saved = apply(service, result)
    assert saved["child_workflow_id"] == "extracted"
    assert saved["revision"] == 4
    child = store.workflows["extracted"]
    assert child["account_id"] == "account-a"
    assert child["enabled"] is True
    assert child["settings"]["callable"]["trigger_channels"] == ["parent_workflow"]
    assert "canvas_annotations" not in child["settings"]
    assert store.workflows["wf"]["groups"] == []
    assert store.bindings == [("project-a", "extracted")]


def test_parent_failure_rolls_back_child_creation(service):
    service, store = service
    result = preview(service)
    original = copy.deepcopy(store.workflows)
    store.fail_parent = True
    with pytest.raises(RuntimeError, match="parent save failed"):
        apply(service, result)
    assert store.workflows == original
    assert store.bindings == []


def test_disabled_parent_copies_active_project_bindings_only(service, monkeypatch):
    service, store = service
    store.workflows["wf"]["enabled"] = False
    rows = [
        {"workflow_id": "wf", "project_id": "active", "enabled": True},
        {"workflow_id": "wf", "project_id": "inactive", "enabled": False},
        {"workflow_id": "wf", "project_id": "deleted", "enabled": True, "deleted_at": "date"},
    ]

    def list_bindings(*, enabled_only=True):
        # The registry query filters both the binding and workflow activation.
        return [] if enabled_only else rows

    monkeypatch.setattr(store, "list_project_workflows", list_bindings)
    result = preview(service)
    assert result["can_convert"]
    apply(service, result)
    assert store.bindings == [("active", "extracted")]


def test_stale_parent_revision_refuses_before_creating_child(service):
    service, store = service
    result = preview(service)
    store.workflows["wf"]["revision"] = 4
    with pytest.raises(WorkflowRevisionConflictError):
        apply(service, result)
    assert "extracted" not in store.workflows


def test_changed_plan_or_reactions_requires_new_preview(service):
    service, store = service
    result = preview(service)
    service.reactions = lambda *_args: [{"reaction_key": "group:process:error"}]
    with pytest.raises(GroupExtractionError):
        apply(service, result)
    assert "extracted" not in store.workflows
    assert store.workflows["wf"]["revision"] == 3


def test_child_id_collision_never_overwrites_an_existing_workflow(service):
    service, store = service
    store.workflows["extracted"] = {
        "workflow_id": "extracted",
        "account_id": "foreign",
        "revision": 9,
    }
    original = copy.deepcopy(store.workflows)
    result = preview(service)
    assert not result["can_convert"]
    assert "foreign" not in str(result)
    with pytest.raises(GroupExtractionError):
        apply(service, result)
    assert store.workflows == original


def test_child_readiness_failure_does_not_leave_a_disabled_broken_child(service):
    service, store = service
    service.validate = lambda *_args: {
        "errors": [
            {
                "code": "missing_connection",
                "message": "Unavailable connection",
                "path": "steps.fetch",
            }
        ]
    }
    result = preview(service)
    assert not result["can_convert"]
    assert any(issue["code"] == "missing_connection" for issue in result["issues"])
    assert set(store.workflows) == {"wf"}


def test_cross_account_source_is_refused(service):
    service, store = service
    store.workflows["wf"]["account_id"] = "other"
    with pytest.raises(GroupExtractionError) as error:
        preview(service)
    assert error.value.status_code == 404
    assert set(store.workflows) == {"wf"}


def test_invalid_preview_digest_is_not_accepted(service):
    service, store = service
    result = preview(service)
    result["preview_digest"] = "stale"
    with pytest.raises(GroupExtractionError) as error:
        apply(service, result)
    assert error.value.status_code == 409
    assert set(store.workflows) == {"wf"}


@pytest.mark.parametrize("message_only", [True, False])
def test_source_type_evidence_cannot_hide_messages_or_conflicting_producers(
    service, monkeypatch, message_only
):
    service, store = service
    from core.application.workflows import workflow_group_extraction_service as module

    io = module.IOResolver(store)
    io.get_step_outputs = lambda _wf, step: (
        [{"name": "items", "type": "array"}] if step == "prepare" else []
    )
    io.get_step_state_outputs = lambda _wf, step: (
        ([] if message_only else [{"name": "items", "type": "number"}])
        if step == "prepare"
        else [{"name": "response", "type": "object"}]
    )
    # Use two outside producers with contradictory states in the second case.
    store.workflows["wf"]["steps"].append(
        {"step_id": "other", "step_kind": "set_variable", "group_id": None}
    )
    original = io.get_step_state_outputs
    io.get_step_state_outputs = lambda wf, step: (
        [{"name": "items", "type": "array"}] if step == "other" else original(wf, step)
    )
    monkeypatch.setattr(module, "IOResolver", lambda _db: io)
    result = preview(service)
    assert not result["can_convert"]
    assert any(
        issue["code"] in {"message_state_fallback", "ambiguous_input_type"}
        for issue in result["issues"]
    )


def test_preview_can_validate_an_existing_unrelated_parallel_child_chain(service):
    service, store = service
    store.workflows["existing_child"] = {
        "workflow_id": "existing_child",
        "revision": 1,
        "account_id": "account-a",
        "steps": [{"step_id": "end", "step_kind": "stop_success"}],
        "groups": [],
        "settings": {
            "execution_graph": {
                "schema_version": 1,
                "entry": {"kind": "step", "id": "end"},
                "edges": [],
            }
        },
    }
    parent = store.workflows["wf"]
    parent["groups"].append(
        {
            "group_id": "other_group",
            "body": {
                "kind": "lanes",
                "lanes": [{"lane_id": "a", "entry": {"kind": "step", "id": "other_invoke"}}],
            },
        }
    )
    parent["steps"].append(
        {
            "step_id": "other_invoke",
            "step_kind": "invoke_workflow",
            "group_id": "other_group",
            "lane_id": "a",
            "override": {"invoke_workflow": {"workflow_id": "existing_child"}},
        }
    )
    graph = parent["settings"]["execution_graph"]
    graph["edges"][-1]["target"] = {"kind": "group", "id": "other_group"}
    graph["edges"].extend(
        [
            {
                "source": {"kind": "step", "id": "other_invoke"},
                "outcome": {"name": "success"},
                "target": {"kind": "group_exit", "group_id": "other_group", "lane_id": "a"},
            },
            {
                "source": {"kind": "group", "id": "other_group"},
                "outcome": {"name": "success"},
                "target": {"kind": "step", "id": "done"},
            },
        ]
    )
    result = preview(service)
    assert result["can_convert"], result["issues"]


def test_extracted_child_keeps_account_ownership_and_parent_project_bindings(service):
    extraction, store = service
    store.workflows["wf"]["scope_kind"] = "account"
    result = preview(extraction)
    assert result["can_convert"], result["issues"]
    apply(extraction, result)
    child = store.workflows["extracted"]
    assert child["scope_kind"] == "account"
    assert store.bindings == [("project-a", "extracted")]


def test_retained_parent_output_preview_and_apply_preserve_parent_contract(service, monkeypatch):
    extraction, store = service
    from core.application.workflows import workflow_group_extraction_service as module
    from tests.unit.test_workflow_group_extraction_retained_outputs import retained_source

    workflow, rows = retained_source()
    workflow["account_id"] = "account-a"
    store.workflows["wf"] = workflow
    io = module.IOResolver(store)
    io.get_step_state_outputs = lambda _wf, step_id: rows.get(step_id, [])
    io.get_step_state_writes = lambda step, **_kwargs: rows.get(step["step_id"], [])
    io.get_step_outputs = lambda _wf, step_id: rows.get(step_id, [])
    monkeypatch.setattr(module, "IOResolver", lambda _db: io)
    result = preview(extraction)
    assert result["can_convert"], result["issues"]
    apply(extraction, result)
    parent = store.workflows["wf"]
    assert parent["settings"]["callable"]["expected_outputs"] == [
        {"name": "items", "type": "array", "required": True}
    ]
    assert parent["steps"][0]["override"]["set_variable"]["assignments"]["items"] == [
        {"name": "one"},
        {"name": "two"},
    ]
    assert store.workflows["extracted"]["settings"]["callable"]["expected_outputs"] == [
        {"name": "normalized", "type": "object", "required": False}
    ]


def test_a_child_structural_issue_is_listed_once_not_again_from_readiness(service):
    """A Parallel lanes group with an empty lane: the dialog lists the reason once."""
    service, store = service
    workflow = source_workflow(parallel=True)
    workflow["account_id"] = "account-a"
    workflow["settings"].pop("canvas_annotations", None)
    workflow["steps"][1]["override"] = {"http_request": {"url": "https://example.test/records"}}
    workflow["groups"][0]["body"]["lanes"].append({"lane_id": "second"})
    store.workflows["wf"] = workflow

    def readiness(view, workflow_id):
        # The real readiness adapter reports the structural issues again, under "workflow.".
        candidate = definition_from_workflow(view.get_workflow(workflow_id))
        return {
            "errors": [
                {**issue, "path": f"workflow.{issue['path']}"}
                for issue in validate_workflow_definition(candidate)
            ]
        }

    service.validate = readiness
    result = preview(service)
    assert not result["can_convert"]
    codes = [issue["code"] for issue in result["issues"]]
    assert codes.count("workflow.group.lane_entry_missing") == 1, result["issues"]


def test_a_group_holding_a_for_each_converts_with_the_loop_and_its_body(service):
    """Group… may wrap a For each, so Preview + Create must carry it to the child whole."""
    extraction, store = service
    result = preview(extraction)
    assert result["can_convert"], result["issues"]
    saved = apply(extraction, result)
    child = store.workflows["extracted"]
    # The For each, its body step and the Group that holds it all moved to the child.
    assert [item["for_each_id"] for item in child["for_each"]] == ["records"]
    assert child["for_each"][0]["group_id"] == "process"
    assert child["for_each"][0]["body"]["entry"] == {"kind": "step", "id": "fetch"}
    assert [
        (s["step_id"], s.get("for_each_id")) for s in child["steps"] if s["step_id"] == "fetch"
    ] == [("fetch", "records")]
    assert [g["group_id"] for g in child["groups"]] == ["process"]
    assert not validate_workflow_definition(definition_from_workflow(child))
    # The parent keeps neither, and calls the child through an Invoke workflow step in the Group's place.
    parent = store.workflows["wf"]
    assert parent["for_each"] == []
    assert all(s["step_id"] != "fetch" for s in parent["steps"])
    invoke_id = saved["invocation_step_id"]
    invoke = next(s for s in parent["steps"] if s["step_id"] == invoke_id)
    assert invoke["step_kind"] == "invoke_workflow"
    assert invoke["override"]["invoke_workflow"]["workflow_id"] == "extracted"
    edges = parent["settings"]["execution_graph"]["edges"]
    assert any(
        e["source"]["id"] == "prepare" and e["target"] == {"kind": "step", "id": invoke_id}
        for e in edges
    )
    assert any(e["source"]["id"] == invoke_id and e["target"]["id"] == "done" for e in edges)
    assert not validate_workflow_definition(definition_from_workflow(parent))
