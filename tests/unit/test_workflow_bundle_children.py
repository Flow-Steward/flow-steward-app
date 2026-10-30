"""A template carries the workflow and the child workflows it calls, in one file.

A user who picks a workflow from the catalog should not have to hunt for the child
workflows it needs. So export can write a workflow together with every workflow it
invokes, and import treats a child the file carries as part of the import: created
first, under a new id if its own is taken, or reused when the user already has it,
with the calling workflow pointed at wherever the child ended up.
"""

from __future__ import annotations

import pytest
import yaml

from core.application.workflows.workflow_portability import (
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from core.application.workflows.workflow_portability_bundle_children import (
    bundled_child_ids,
    creation_order,
    root_workflow_id,
    workflow_child_refs,
)
from core.application.workflows.workflow_template_catalog import WorkflowTemplateCatalogError
from core.application.workflows.workflow_template_catalog_create import _assert_one_template
from tests._workflow_definitions import linear_workflow
from tests.unit.test_workflow_portability import _PortabilityDB

ACCOUNT = "acc-1"


def _workflow(workflow_id: str, *, calls: tuple[str, ...] = (), account: str = ACCOUNT) -> dict:
    steps = [
        {
            "step_id": f"call_{child}",
            "step_kind": "invoke_workflow",
            "override": {"invoke_workflow": {"workflow_id": child}},
        }
        for child in calls
    ]
    return linear_workflow(
        *steps,
        workflow_id=workflow_id,
        version="1.0.0",
        display_name=workflow_id.replace("_", " ").title(),
        description=f"Sample workflow {workflow_id}.",
        projection_mode="manual",
        account_id=account,
        trigger={},
        tags=[],
    )


def _export(*workflows: dict, root: str, include_children: bool = True) -> str:
    source = _PortabilityDB(workflows={row["workflow_id"]: row for row in workflows})
    return WorkflowPortabilityService(source).export_yaml(root, include_children=include_children)


def _invoked(saved: dict) -> list[str]:
    return [
        ((step.get("override") or {}).get("invoke_workflow") or {}).get("workflow_id")
        for step in saved.get("steps") or []
        if step.get("step_kind") == "invoke_workflow"
    ]


# -- export -------------------------------------------------------------------------


def test_export_with_children_carries_every_workflow_the_root_invokes() -> None:
    bundle = yaml.safe_load(
        _export(
            _workflow("parent_wf", calls=("child_a",)),
            _workflow("child_a", calls=("child_b",)),
            _workflow("child_b"),
            root="parent_wf",
        )
    )

    assert [row["workflow_id"] for row in bundle["workflows"]] == [
        "parent_wf",
        "child_a",
        "child_b",
    ]
    assert bundle["metadata"]["source_workflow_id"] == "parent_wf"
    assert bundle["metadata"]["child_workflow_ids"] == ["child_a", "child_b"]


def test_plain_export_still_carries_one_workflow() -> None:
    bundle = yaml.safe_load(
        _export(
            _workflow("parent_wf", calls=("child_a",)),
            _workflow("child_a"),
            root="parent_wf",
            include_children=False,
        )
    )

    assert [row["workflow_id"] for row in bundle["workflows"]] == ["parent_wf"]
    assert "child_workflow_ids" not in bundle["metadata"]


def test_export_leaves_out_a_child_from_another_account_and_stops_at_a_cycle() -> None:
    bundle = yaml.safe_load(
        _export(
            _workflow("parent_wf", calls=("child_a", "foreign_child")),
            _workflow("child_a", calls=("parent_wf",)),
            _workflow("foreign_child", account="acc-2"),
            root="parent_wf",
        )
    )

    assert [row["workflow_id"] for row in bundle["workflows"]] == ["parent_wf", "child_a"]


# -- bundle structure ----------------------------------------------------------------


def test_children_are_ordered_before_the_workflows_that_call_them() -> None:
    workflows = [
        _workflow("parent_wf", calls=("child_a",)),
        _workflow("child_a", calls=("child_b",)),
        _workflow("child_b"),
    ]

    assert [row["workflow_id"] for row in creation_order(workflows)] == [
        "child_b",
        "child_a",
        "parent_wf",
    ]
    assert bundled_child_ids(workflows) == {"child_a", "child_b"}
    assert root_workflow_id({}, workflows) == "parent_wf"
    assert workflow_child_refs(workflows[0]) == ["child_a"]


def test_a_cycle_does_not_loop() -> None:
    workflows = [_workflow("a_wf", calls=("b_wf",)), _workflow("b_wf", calls=("a_wf",))]

    assert {row["workflow_id"] for row in creation_order(workflows)} == {"a_wf", "b_wf"}


# -- import --------------------------------------------------------------------------


def _bundle() -> str:
    return _export(
        _workflow("parent_wf", calls=("child_a",)),
        _workflow("child_a"),
        root="parent_wf",
    )


def test_a_carried_child_is_not_a_missing_dependency() -> None:
    preflight = WorkflowPortabilityService(_PortabilityDB()).preflight_import(_bundle())

    assert preflight["can_apply"] is True, preflight["dependencies"]
    child_rows = [
        row for row in preflight["dependencies"] if row["dependency_type"] == "child_workflow"
    ]
    assert child_rows
    assert all(row["status"] == "resolved" for row in child_rows)
    assert not any(row["blocking"] for row in child_rows)
    assert preflight["root_workflow_id"] == "parent_wf"
    assert {row["workflow_id"]: row["role"] for row in preflight["workflows"]} == {
        "parent_wf": "root",
        "child_a": "child",
    }


def test_import_creates_the_child_first_and_the_parent_calls_it() -> None:
    target = _PortabilityDB()

    result = WorkflowPortabilityService(target).apply_import(_bundle())

    assert [call["data"]["workflow_id"] for call in target.save_calls] == ["child_a", "parent_wf"]
    assert result["workflow_id"] == "parent_wf"
    assert {row["workflow_id"]: row["role"] for row in result["imported_workflows"]} == {
        "child_a": "child",
        "parent_wf": "root",
    }
    assert _invoked(target.workflows["parent_wf"]) == ["child_a"]


def test_a_renamed_child_is_what_the_parent_calls() -> None:
    target = _PortabilityDB(workflows={"child_a": _workflow("child_a")})
    resolutions = [
        {
            "dependency_type": "workflow",
            "ref": "child_a",
            "action": "rename_workflow",
            "target_workflow_id": "child_a_copy",
        }
    ]

    WorkflowPortabilityService(target).apply_import(_bundle(), resolutions=resolutions)

    assert "child_a_copy" in target.workflows
    assert _invoked(target.workflows["parent_wf"]) == ["child_a_copy"]


def test_an_accepted_child_rename_stays_in_the_preflight_as_resolved() -> None:
    # The catalog set-up re-checks with the choices already made and builds its
    # "Creates X as Y" summary, and its next request, from the rows that come
    # back. A rename to a free id used to drop the child's row, so the rename
    # vanished from the summary and from every later check.
    target = _PortabilityDB(workflows={"child_a": _workflow("child_a")})
    resolutions = [
        {
            "dependency_type": "workflow",
            "ref": "child_a",
            "action": "rename_workflow",
            "target_workflow_id": "child_a_copy",
        }
    ]

    preflight = WorkflowPortabilityService(target).preflight_import(
        _bundle(), resolutions=resolutions
    )

    rows = [
        row
        for row in preflight["dependencies"]
        if row["dependency_type"] == "workflow" and row["ref"] == "child_a"
    ]
    assert len(rows) == 1, preflight["dependencies"]
    assert rows[0]["status"] == "resolved"
    assert rows[0]["blocking"] is False
    assert rows[0]["metadata"]["target_workflow_id"] == "child_a_copy"
    assert preflight["can_apply"] is True


def test_a_free_child_id_has_no_workflow_row() -> None:
    preflight = WorkflowPortabilityService(_PortabilityDB()).preflight_import(_bundle())

    assert not [row for row in preflight["dependencies"] if row["dependency_type"] == "workflow"]


def test_an_existing_child_can_be_reused_instead_of_copied() -> None:
    existing = _workflow("child_a")
    target = _PortabilityDB(workflows={"child_a": existing})
    service = WorkflowPortabilityService(target)

    conflict = next(
        row
        for row in service.preflight_import(_bundle())["dependencies"]
        if row["dependency_type"] == "workflow" and row["ref"] == "child_a"
    )
    assert conflict["status"] == "conflict"
    assert "use_existing" in conflict["resolution_actions"]

    result = service.apply_import(
        _bundle(),
        resolutions=[{"dependency_type": "workflow", "ref": "child_a", "action": "use_existing"}],
    )

    assert [call["data"]["workflow_id"] for call in target.save_calls] == ["parent_wf"]
    assert result["reused_workflows"] == [
        {"workflow_id": "child_a", "source_workflow_id": "child_a"}
    ]
    assert target.workflows["child_a"] == existing
    assert _invoked(target.workflows["parent_wf"]) == ["child_a"]


def test_the_root_cannot_be_reused_only_children_can() -> None:
    target = _PortabilityDB(workflows={"parent_wf": _workflow("parent_wf")})
    service = WorkflowPortabilityService(target)

    conflict = next(
        row
        for row in service.preflight_import(_bundle())["dependencies"]
        if row["dependency_type"] == "workflow" and row["ref"] == "parent_wf"
    )

    assert "use_existing" not in conflict["resolution_actions"]
    bundle = _bundle()
    resolutions = [{"dependency_type": "workflow", "ref": "parent_wf", "action": "use_existing"}]
    with pytest.raises(WorkflowPortabilityError):
        service.apply_import(bundle, resolutions=resolutions)


# -- catalog -------------------------------------------------------------------------


def _preflight_of(bundle_text: str) -> dict:
    return WorkflowPortabilityService(_PortabilityDB()).preflight_import(bundle_text)


def test_a_catalog_template_may_carry_its_children() -> None:
    _assert_one_template(_preflight_of(_bundle()), item_id="parent_wf")


def test_a_catalog_template_is_refused_when_it_carries_an_unrelated_workflow() -> None:
    bundle = yaml.safe_load(_bundle())
    stray = yaml.safe_load(_export(_workflow("stray_wf"), root="stray_wf"))["workflows"][0]
    bundle["workflows"].append(stray)

    preflight = _preflight_of(yaml.safe_dump(bundle))
    with pytest.raises(WorkflowTemplateCatalogError) as raised:
        _assert_one_template(preflight, item_id="parent_wf")

    assert raised.value.code == "workflow_template_multiple_workflows"
    assert "stray_wf" in str(raised.value)


def test_a_reused_child_cannot_be_renamed_to_an_unchecked_id() -> None:
    """The review's probe: reuse the carried child, then rename its call to a missing id."""
    target = _PortabilityDB(workflows={"child_a": _workflow("child_a")})
    service = WorkflowPortabilityService(target)
    resolutions = [
        {"dependency_type": "workflow", "ref": "child_a", "action": "use_existing"},
        {
            "dependency_key": "child_workflow:child_a",
            "action": "rename",
            "target_ref": "no_such_workflow_xyz",
        },
    ]

    preflight = service.preflight_import(_bundle(), resolutions=resolutions)
    row = next(
        row
        for row in preflight["dependencies"]
        if row["dependency_key"] == "child_workflow:child_a"
    )

    assert row["status"] == "conflict"
    assert row["blocking"] is True
    assert preflight["can_apply"] is False
    bundle = _bundle()
    with pytest.raises(WorkflowPortabilityError):
        service.apply_import(bundle, resolutions=resolutions)
    assert "parent_wf" not in target.workflows
