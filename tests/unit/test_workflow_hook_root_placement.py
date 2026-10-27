"""Hook declarations live at the author root; connected children keep normal containers."""

from __future__ import annotations

import copy

import pytest
import yaml

from core.application.extensions.extension_descriptor import (
    ExtensionDescriptorArtifactEntry,
    _refuse_non_final_workflow,
)
from core.application.registry.workflow_definition_rows import _definition_payload
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    apply_workflow_changes,
    create_workflow_with_definition,
)
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from core.application.workflows.workflow_portability import (
    SCHEMA_VERSION,
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from tests._workflow_definitions import edge, node
from tests.unit.test_workflow_definition_service import _CreateStore, _Store
from tests.unit.test_workflow_hook_binding_postgres import hooked as hooked
from tests.unit.test_workflow_hook_binding_postgres import request
from tests.unit.test_workflow_hook_contract import declaration
from tests.unit.test_workflow_portability import _resolved_db

ROOT_ONLY = "workflow.hook.root_only"
HOOK_PATH = "steps.hook.override.invoke_workflow.hook"
PLACEMENTS = [
    ("group", "sequence"),
    ("group", "lanes"),
    ("for_each", "sequence"),
    ("for_each", "lanes"),
]


def _work(step_id: str) -> dict:
    return {
        "step_id": step_id,
        "step_kind": "http_request",
        "override": {"http_request": {"method": "GET", "url": "https://example.test"}},
    }


def _hook(step_id: str = "hook") -> dict:
    return {
        "step_id": step_id,
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": {"hook": declaration(hook_id=step_id)}},
    }


def _container_definition(kind: str, body_kind: str, *, hook: bool = True) -> dict:
    owner_key = f"{kind}_id"
    lane = "first" if body_kind == "lanes" else None
    member = (
        _hook()
        if hook
        else {
            "step_id": "hook",
            "step_kind": "invoke_workflow",
            "override": {"invoke_workflow": {"workflow_id": "ordinary_child"}},
        }
    )
    member.update({owner_key: "container", "lane_id": lane})
    body = {"kind": "sequence", "entry": node("hook")}
    exit_ref = {"kind": f"{kind}_exit", owner_key: "container", "lane_id": lane}
    steps = [_work("start"), member, {"step_id": "done", "step_kind": "stop_success"}]
    edges = [
        edge("start", node("container", kind)),
        edge("hook", exit_ref),
        edge("container", node("done"), source_kind=kind),
    ]
    if body_kind == "lanes":
        body = {
            "kind": "lanes",
            "lanes": [
                {"lane_id": "first", "entry": node("hook")},
                {"lane_id": "second", "entry": node("peer")},
            ],
            "concurrency_limit": 2,
        }
        steps.append({**_work("peer"), owner_key: "container", "lane_id": "second"})
        edges.append(edge("peer", {**exit_ref, "lane_id": "second"}))
    container = {owner_key: "container", "body": body}
    if kind == "for_each":
        container.update({"over": "state.items", "item_name": "item"})
    return {
        "steps": steps,
        "groups": [container] if kind == "group" else [],
        "for_each": [container] if kind == "for_each" else [],
        "execution_graph": {"schema_version": 1, "entry": node("start"), "edges": edges},
    }


def _root_hooks_around_container(kind: str, body_kind: str) -> dict:
    definition = _container_definition(kind, body_kind, hook=False)
    member = definition["steps"][1]
    member.update(_work("member"))
    container = (definition["groups"] or definition["for_each"])[0]
    if body_kind == "sequence":
        container["body"]["entry"] = node("member")
    else:
        container["body"]["lanes"][0]["entry"] = node("member")
    for link in definition["execution_graph"]["edges"]:
        if link["source"] == node("hook"):
            link["source"] = node("member")
    definition["steps"].extend([_hook("before"), _hook("after")])
    links = definition["execution_graph"]["edges"]
    links[0]["target"] = node("before")
    links[2]["target"] = node("after")
    links.extend([edge("before", node("container", kind)), edge("after", node("done"))])
    return definition


def _hook_issues(definition: dict) -> list[dict]:
    return [
        issue for issue in validate_workflow_definition(definition) if issue["code"] == ROOT_ONLY
    ]


@pytest.mark.parametrize(("kind", "body_kind"), PLACEMENTS)
def test_hook_declaration_with_container_ownership_is_refused(kind, body_kind):
    issues = _hook_issues(_container_definition(kind, body_kind))

    assert [(issue["code"], issue["path"]) for issue in issues] == [(ROOT_ONLY, HOOK_PATH)]


@pytest.mark.parametrize(("kind", "body_kind"), PLACEMENTS)
def test_body_entry_cannot_hide_a_nested_hook_by_omitting_its_owner(kind, body_kind):
    definition = _container_definition(kind, body_kind)
    definition["steps"][1].pop(f"{kind}_id")
    definition["steps"][1].pop("lane_id")

    assert [(issue["code"], issue["path"]) for issue in _hook_issues(definition)] == [
        (ROOT_ONLY, HOOK_PATH)
    ]


@pytest.mark.parametrize(("kind", "body_kind"), PLACEMENTS)
def test_body_edge_cannot_enter_a_hook_with_root_ownership(kind, body_kind):
    definition = _container_definition(kind, body_kind)
    owner_key = f"{kind}_id"
    lane = "first" if body_kind == "lanes" else None
    definition["steps"][1].pop(owner_key)
    definition["steps"][1].pop("lane_id")
    definition["steps"].append({**_work("member"), owner_key: "container", "lane_id": lane})
    container = (definition["groups"] or definition["for_each"])[0]
    if body_kind == "sequence":
        container["body"]["entry"] = node("member")
    else:
        container["body"]["lanes"][0]["entry"] = node("member")
    definition["execution_graph"]["edges"].append(edge("member", node("hook")))

    assert [(issue["code"], issue["path"]) for issue in _hook_issues(definition)] == [
        (ROOT_ONLY, HOOK_PATH)
    ]


@pytest.mark.parametrize(("kind", "body_kind"), PLACEMENTS)
def test_root_hooks_before_and_after_containers_remain_valid(kind, body_kind):
    assert validate_workflow_definition(_root_hooks_around_container(kind, body_kind)) == []


@pytest.mark.parametrize(("kind", "body_kind"), PLACEMENTS)
def test_plain_invoke_inside_a_container_remains_valid(kind, body_kind):
    assert validate_workflow_definition(_container_definition(kind, body_kind, hook=False)) == []


@pytest.mark.parametrize(("kind", "body_kind"), PLACEMENTS)
def test_ordinary_child_with_containers_can_still_be_connected_to_a_root_hook(
    hooked, kind, body_kind
):
    ctx = hooked
    child = ctx["store"].get_workflow(ctx["child_id"])
    definition = _container_definition(kind, body_kind, hook=False)
    # The child performs ordinary work, rather than invoking a fixture dependency.
    definition["steps"][1].update(_work("hook"))
    child.update({key: definition[key] for key in ("steps", "groups", "for_each")})
    child["settings"] = {**child["settings"], "execution_graph": definition["execution_graph"]}
    ctx["store"].save_workflow(child, expected_revision=int(child["revision"]))

    saved = ctx["service"].save(request(ctx))

    assert saved.target_workflow_id == ctx["child_id"]
    assert validate_workflow_definition(definition) == []


@pytest.mark.parametrize("kind", ["group", "for_each"])
@pytest.mark.parametrize("enabled", [False, True])
def test_create_boundary_refuses_nested_hook_even_for_a_disabled_draft(kind, enabled):
    store = _CreateStore()
    payload = {"workflow_id": "wf", "enabled": enabled, **_container_definition(kind, "sequence")}

    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        create_workflow_with_definition(store, payload, author="test")

    assert ROOT_ONLY in {issue["code"] for issue in raised.value.issues}
    assert store.saved == []


@pytest.mark.parametrize("kind", ["group", "for_each"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_disabled_change_set_cannot_move_a_root_hook_inside_a_container(kind, dry_run):
    definition = _root_hooks_around_container(kind, "sequence")
    workflow = {
        "workflow_id": "wf",
        "revision": 3,
        "enabled": False,
        "settings": {"execution_graph": definition.pop("execution_graph")},
        **definition,
    }
    store = _Store(workflow)
    arguments = {
        "expected_revision": 3,
        "operations": [{"op": "move_step", "step_id": "before", f"{kind}_id": "container"}],
        "author": "test",
        "dry_run": dry_run,
    }

    if dry_run:
        result = apply_workflow_changes(store, "wf", **arguments)
        assert ROOT_ONLY in {issue["code"] for issue in result.blocking_issues}
    else:
        with pytest.raises(WorkflowDefinitionChangeError) as raised:
            apply_workflow_changes(store, "wf", **arguments)
        assert ROOT_ONLY in {issue["code"] for issue in raised.value.issues}
    assert store.saves == []
    assert store.get_workflow("wf") == workflow


@pytest.mark.parametrize(("kind", "body_kind"), PLACEMENTS)
def test_registry_definition_write_boundary_rejects_nested_hook(kind, body_kind):
    definition = _container_definition(kind, body_kind)
    payload = {**definition, "settings": {"execution_graph": definition["execution_graph"]}}

    with pytest.raises(ValueError, match=ROOT_ONLY):
        _definition_payload(payload)


@pytest.mark.parametrize(("kind", "body_kind"), PLACEMENTS)
def test_extension_workflow_boundary_rejects_nested_hook(kind, body_kind):
    definition = _container_definition(kind, body_kind)
    snapshot = {**definition, "settings": {"execution_graph": definition["execution_graph"]}}
    entry = ExtensionDescriptorArtifactEntry(workflow_id="wf", snapshot=snapshot)

    with pytest.raises(ValueError, match=ROOT_ONLY):
        _refuse_non_final_workflow(entry)


@pytest.mark.parametrize("kind", ["group", "for_each"])
def test_import_preflight_and_apply_refuse_nested_hook_without_writes(kind):
    definition = _container_definition(kind, "sequence")
    bundle = yaml.safe_dump(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "workflow_bundle",
            "metadata": {},
            "workflows": [{"workflow_id": "wf", "version": "1.0.0", "settings": {}, **definition}],
            "dependencies": {},
            "import_hints": {},
        }
    )
    store = _resolved_db()
    before = copy.deepcopy(store.workflows)
    service = WorkflowPortabilityService(store)

    preflight = service.preflight_import(bundle)

    assert preflight["can_apply"] is False
    assert any(
        issue["code"] == ROOT_ONLY and issue["blocking"] for issue in preflight["topology_issues"]
    )
    with pytest.raises(WorkflowPortabilityError) as raised:
        service.apply_import(bundle, target_workflow_id="imported")
    assert ROOT_ONLY in {issue["code"] for issue in raised.value.errors}
    assert store.workflows == before
    assert store.save_calls == []
