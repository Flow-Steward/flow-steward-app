from __future__ import annotations

import copy

import pytest

from core.application.workflows.workflow_definition_service import apply_workflow_changes
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from core.application.workflows.workflow_group_extraction_structure import extract_group_structure
from tests.unit.test_workflow_definition_service import _Store, edge, ref, step, workflow


def source_workflow(*, parallel: bool = False, group_entry: bool = False) -> dict:
    """prepare → Group 'process' → done.

    Sequential: the Group runs a For each 'records' over state.items whose body
    is 'fetch'. Parallel: the Group has one lane with 'fetch'.
    """
    group = {
        "group_id": "process",
        "display_name": "Process records",
        "body": (
            {"kind": "lanes", "lanes": [{"lane_id": "first", "entry": ref("fetch")}]}
            if parallel
            else {"kind": "sequence", "entry": ref("records", "for_each")}
        ),
        "error_policy": {"on_error": "retry", "max_retries": 2},
    }
    for_each = (
        []
        if parallel
        else [
            {
                "for_each_id": "records",
                "group_id": "process",
                "over": "state.items",
                "item_name": "record",
                "index_name": None,
                "body": {"kind": "sequence", "entry": ref("fetch")},
            }
        ]
    )
    fetch_exit = (
        {"kind": "group_exit", "group_id": "process", "lane_id": "first"}
        if parallel
        else {"kind": "for_each_exit", "for_each_id": "records", "lane_id": None}
    )
    edges = [
        edge("prepare", ref("process", "group")),
        edge("fetch", fetch_exit),
        {
            "source": ref("process", "group"),
            "outcome": {"name": "success"},
            "target": ref("done"),
        },
    ]
    if not parallel:
        edges.insert(
            2,
            {
                "source": ref("records", "for_each"),
                "outcome": {"name": "success"},
                "target": {"kind": "group_exit", "group_id": "process", "lane_id": None},
            },
        )
    result = workflow(
        enabled=False,
        groups=[group],
        for_each=for_each,
        steps=[
            step("prepare"),
            step(
                "fetch",
                group_id="process" if parallel else None,
                for_each_id=None if parallel else "records",
                lane_id="first" if parallel else None,
            ),
            step("done", "stop_success"),
        ],
        graph={
            "schema_version": 1,
            "entry": ref("process", "group") if group_entry else ref("prepare"),
            "edges": edges,
        },
    )
    result["settings"]["canvas_annotations"] = [{"id": "parent-note"}]
    return result


def invocation() -> dict:
    return step(
        "process_child",
        "invoke_workflow",
        override={
            "invoke_workflow": {"workflow_id": "child", "input_mapping": {}, "output_mapping": {}}
        },
    )


@pytest.mark.parametrize("parallel", [False, True])
def test_container_and_internal_edges_move_but_parent_connections_are_preserved(parallel):
    source = source_workflow(parallel=parallel)
    original = copy.deepcopy(source)
    plan = extract_group_structure(source, "process", invocation())

    assert source == original
    child = plan.child_definition
    assert child["groups"] == source["groups"]
    assert child["steps"][0] == source["steps"][1]
    assert child["steps"][-1]["step_kind"] == "stop_success"
    assert child["execution_graph"]["entry"] == ref("process", "group")
    assert source["settings"]["execution_graph"]["edges"][1] in child["execution_graph"]["edges"]
    assert "settings" not in child
    assert not validate_workflow_definition(child)

    store = _Store(source)
    parent = apply_workflow_changes(
        store, "wf", expected_revision=3, operations=plan.parent_operations, author="audit"
    ).workflow
    assert parent["groups"] == []
    assert {s["step_id"] for s in parent["steps"]} == {"prepare", "process_child", "done"}
    assert parent["settings"]["canvas_annotations"] == [{"id": "parent-note"}]
    assert (
        edge("prepare", ref("process_child"), rework=False)
        in parent["settings"]["execution_graph"]["edges"]
    )
    assert (
        edge("process_child", ref("done"), rework=False)
        in parent["settings"]["execution_graph"]["edges"]
    )


def test_group_entry_becomes_invocation_entry():
    source = source_workflow(group_entry=True)
    plan = extract_group_structure(source, "process", invocation())
    parent = apply_workflow_changes(
        _Store(source), "wf", expected_revision=3, operations=plan.parent_operations, author="audit"
    ).workflow
    assert parent["settings"]["execution_graph"]["entry"] == ref("process_child")


def test_early_stop_is_refused_without_reinterpreting_its_scope():
    source = source_workflow()
    source["steps"][1]["step_kind"] = "stop_success"
    child_invocation = invocation()
    with pytest.raises(ValueError, match="Stop"):
        extract_group_structure(source, "process", child_invocation)


def test_error_exit_and_rework_are_preserved_without_a_second_retry():
    source = source_workflow()
    source["groups"][0]["error_policy"] = {"on_error": "route_to_step"}
    source["settings"]["execution_graph"]["edges"].append(
        {
            "source": ref("process", "group"),
            "outcome": {"name": "error"},
            "target": ref("prepare"),
            "rework": True,
        }
    )
    plan = extract_group_structure(source, "process", invocation())
    assert {s["step_kind"] for s in plan.child_definition["steps"]} == {
        "http_request",
        "stop_success",
        "stop_fail",
    }
    create = next(op for op in plan.parent_operations if op["op"] == "create_step")
    assert create["step"]["error_policy"] == {"on_error": "route_to_step"}
    assert any(op.get("edge", {}).get("rework") is True for op in plan.parent_operations)
    assert not validate_workflow_definition(plan.child_definition)


def test_generated_terminal_names_do_not_collide_with_member_ids():
    source = source_workflow()
    source["steps"][1]["step_id"] = "child_done"
    source["for_each"][0]["body"]["entry"] = ref("child_done")
    source["settings"]["execution_graph"]["edges"][1]["source"] = ref("child_done")
    child = extract_group_structure(source, "process", invocation()).child_definition
    assert len({s["step_id"] for s in child["steps"]}) == len(child["steps"])
    assert not validate_workflow_definition(child)


def test_the_whole_group_moves_with_its_for_each_body():
    source = source_workflow()
    structure = extract_group_structure(source, "process", invocation())
    child = structure.child_definition
    assert [item["for_each_id"] for item in child["for_each"]] == ["records"]
    assert {(s["step_id"], s.get("for_each_id")) for s in child["steps"]} >= {("fetch", "records")}
    assert not validate_workflow_definition(child)
    store = _Store(source)
    apply_workflow_changes(
        store, "wf", expected_revision=3, operations=structure.parent_operations, author="t"
    )
    parent = store.saves[-1]
    assert parent["for_each"] == []
    assert all(s["step_id"] != "fetch" for s in parent["steps"])
