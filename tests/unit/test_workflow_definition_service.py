from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any

import pytest

from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.authoring.schema import WorkflowAuthoringSchema
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    apply_workflow_changes,
    preserve_service_owned_settings,
)
from core.application.workflows.workflow_portability import WorkflowPortabilityService


class _Cursor:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self._row: dict | None = None

    def execute(self, sql: str, params: tuple) -> None:
        if "SELECT reaction_key FROM workflow_reactions" in sql:
            self._row = None
            return
        assert "FOR UPDATE" in sql
        workflow = self.store.workflows.get(params[0])
        self._row = {"revision": workflow["revision"]} if workflow else None

    def fetchone(self) -> dict | None:
        return self._row

    def fetchall(self) -> list[dict]:
        # This in-memory fixture has no persisted reaction rules.
        return []


class _Store:
    """The slice of the registry store the service uses, with revision CAS."""

    def __init__(self, workflow: dict) -> None:
        self.workflows = {workflow["workflow_id"]: copy.deepcopy(workflow)}
        self.saves: list[dict] = []

    @contextmanager
    def _transaction(self):
        snapshot = copy.deepcopy(self.workflows)
        try:
            yield _Cursor(self)
        except Exception:
            self.workflows = snapshot
            raise

    def get_workflow(self, workflow_id: str) -> dict | None:
        workflow = self.workflows.get(workflow_id)
        return copy.deepcopy(workflow) if workflow else None

    def save_workflow(self, data: dict, author: str = "admin", *, expected_revision=None) -> int:
        current = self.workflows[data["workflow_id"]]
        if expected_revision is not None and current["revision"] != expected_revision:
            raise WorkflowRevisionConflictError(data["workflow_id"], expected_revision)
        self.saves.append(copy.deepcopy(data))
        current.update(copy.deepcopy(data))
        current["revision"] += 1
        return current["revision"]


def ref(node_id: str, kind: str = "step") -> dict:
    return {"kind": kind, "id": node_id}


def edge(source: str, target: dict, outcome: str = "success", **extra: Any) -> dict:
    return {"source": ref(source), "outcome": {"name": outcome}, "target": target, **extra}


def step(step_id: str, kind: str = "http_request", **extra: Any) -> dict:
    return {"step_id": step_id, "step_kind": kind, **extra}


def workflow(
    *,
    enabled: bool,
    steps: list | None = None,
    graph: dict | None = None,
    groups=None,
    for_each=None,
) -> dict:
    settings: dict = {"participants": []}
    if graph is not None:
        settings["execution_graph"] = graph
    return {
        "workflow_id": "wf",
        "display_name": "WF",
        "enabled": enabled,
        "revision": 3,
        "settings": settings,
        "steps": steps or [],
        "groups": groups or [],
        "for_each": for_each or [],
    }


def runnable() -> dict:
    return workflow(
        enabled=True,
        steps=[step("a"), step("end", "stop_success")],
        graph={"schema_version": 1, "entry": ref("a"), "edges": [edge("a", ref("end"))]},
    )


def codes(issues: list[dict]) -> list[str]:
    return [issue["code"] for issue in issues]


def test_first_root_node_becomes_entry_and_disabled_keeps_issues() -> None:
    store = _Store(workflow(enabled=False))
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[{"op": "create_step", "step": step("a")}],
        author="t",
    )
    assert result.revision == 4
    assert codes(result.issues) == ["workflow.graph.outcome_missing"]
    saved = store.saves[-1]
    assert saved["settings"]["execution_graph"]["entry"] == ref("a")
    assert saved["steps"][0]["step_id"] == "a"


def test_enabled_batch_create_and_connect_commits_one_revision() -> None:
    store = _Store(runnable())
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[
            {"op": "create_step", "step": step("b", "transform")},
            {"op": "connect", "edge": edge("a", ref("b")), "replace": True},
            {"op": "connect", "edge": edge("b", ref("end"))},
        ],
        author="t",
    )
    assert result.valid
    assert result.revision == 4
    assert len(store.saves) == 1
    edges = store.saves[0]["settings"]["execution_graph"]["edges"]
    assert edge("a", ref("b"), rework=False) in edges
    assert edge("b", ref("end"), rework=False) in edges


def test_enabled_set_with_blocking_result_is_refused_whole() -> None:
    store = _Store(runnable())
    operations = [
        {"op": "create_step", "step": step("b")},
        {"op": "connect", "edge": edge("a", ref("b")), "replace": True},
    ]
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=operations,
            author="t",
        )
    assert "workflow.graph.outcome_missing" in codes(raised.value.issues)
    assert store.saves == []
    assert store.workflows["wf"]["revision"] == 3


def test_a_dry_run_reports_what_apply_would_refuse_and_writes_nothing() -> None:
    store = _Store(runnable())
    operations = [
        {"op": "create_step", "step": step("b")},
        {"op": "connect", "edge": edge("a", ref("b")), "replace": True},
    ]

    result = apply_workflow_changes(
        store, "wf", expected_revision=3, operations=operations, author="t", dry_run=True
    )

    assert result.applied is False
    assert result.revision == 3
    assert "workflow.graph.outcome_missing" in codes(result.blocking_issues)
    assert set(codes(result.blocking_issues)) <= set(codes(result.issues))
    assert [item["step_id"] for item in result.workflow["steps"]][-1] == "b"
    assert store.saves == []
    assert store.workflows["wf"]["revision"] == 3


def test_a_dry_run_of_an_acceptable_set_matches_apply() -> None:
    operations = [{"op": "update_step_config", "step_id": "a", "patch": {"display_name": "A"}}]
    dry_store = _Store(runnable())
    dry = apply_workflow_changes(
        dry_store, "wf", expected_revision=3, operations=operations, author="t", dry_run=True
    )
    real = apply_workflow_changes(
        _Store(runnable()), "wf", expected_revision=3, operations=operations, author="t"
    )

    assert (dry.applied, dry.blocking_issues, dry_store.saves) == (False, [], [])
    assert dry.issues == real.issues
    assert real.applied is True


def test_a_dry_run_of_a_stale_revision_is_still_a_conflict() -> None:
    store = _Store(runnable())
    with pytest.raises(WorkflowRevisionConflictError):
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=2,
            operations=[{"op": "update_step_config", "step_id": "a", "patch": {"x": 1}}],
            author="t",
            dry_run=True,
        )


def test_stale_revision_is_a_conflict_without_write() -> None:
    store = _Store(runnable())
    with pytest.raises(WorkflowRevisionConflictError):
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=2,
            operations=[
                {"op": "update_step_config", "step_id": "a", "patch": {"display_name": "A"}}
            ],
            author="t",
        )
    assert store.saves == []


def test_connect_needs_explicit_replace_for_a_taken_outcome() -> None:
    store = _Store(runnable())
    operations = [{"op": "connect", "edge": edge("a", ref("a"))}]
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=operations,
            author="t",
        )
    assert codes(raised.value.issues) == ["workflow.change.outcome_taken"]


def test_insert_between_keeps_rework_on_the_first_half() -> None:
    graph = {
        "schema_version": 1,
        "entry": ref("draft"),
        "edges": [
            edge("draft", ref("check")),
            edge("check", ref("end"), "true"),
            edge("check", ref("draft"), "false", rework=True, rework_limit=3),
        ],
    }
    store = _Store(
        workflow(
            enabled=True,
            steps=[
                step("draft", "agent"),
                step("check", "if", override={"if": {"condition": "c"}}),
                step("end", "stop_success"),
            ],
            graph=graph,
        )
    )
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[
            {
                "op": "insert_step_between",
                "source": ref("check"),
                "outcome": {"name": "false"},
                "step": step("note", "set_variable"),
            }
        ],
        author="t",
    )
    assert result.valid
    edges = store.saves[0]["settings"]["execution_graph"]["edges"]
    assert edge("check", ref("note"), "false", rework=True, rework_limit=3) in edges
    assert edge("note", ref("draft"), rework=False) in edges


def test_insert_between_refuses_a_branching_step() -> None:
    store = _Store(runnable())
    operations = [
        {
            "op": "insert_step_between",
            "source": ref("a"),
            "outcome": {"name": "success"},
            "step": step("gate", "approval"),
        }
    ]
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=operations,
            author="t",
        )
    assert codes(raised.value.issues) == ["workflow.change.insert_kind_unsupported"]


def test_delete_builds_no_bypass_and_enabled_needs_replacement_edges() -> None:
    store = _Store(
        workflow(
            enabled=True,
            steps=[step("a"), step("b"), step("end", "stop_success")],
            graph={
                "schema_version": 1,
                "entry": ref("a"),
                "edges": [edge("a", ref("b")), edge("b", ref("end"))],
            },
        )
    )
    with pytest.raises(WorkflowDefinitionChangeError):
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=[{"op": "delete_step", "step_id": "b"}],
            author="t",
        )
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[
            {"op": "delete_step", "step_id": "b"},
            {"op": "connect", "edge": edge("a", ref("end"))},
        ],
        author="t",
    )
    assert result.valid
    assert [s["step_id"] for s in store.saves[-1]["steps"]] == ["a", "end"]


def test_move_step_drops_its_edges_and_needs_explicit_reconnection() -> None:
    groups = [
        {
            "group_id": "g",
            "body": {"kind": "sequence", "entry": ref("l1")},
        }
    ]
    store = _Store(
        workflow(
            enabled=False,
            steps=[step("a"), {**step("l1"), "group_id": "g"}, step("end", "stop_success")],
            groups=groups,
            graph={
                "schema_version": 1,
                "entry": ref("a"),
                "edges": [
                    edge("a", ref("g", "group")),
                    edge("l1", {"kind": "group_exit", "group_id": "g", "lane_id": None}),
                    {
                        "source": ref("g", "group"),
                        "outcome": {"name": "success"},
                        "target": ref("end"),
                    },
                ],
            },
        )
    )
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[{"op": "move_step", "step_id": "a", "group_id": "g"}],
        author="t",
    )
    saved = store.saves[-1]
    assert all(e["source"] != ref("a") for e in saved["settings"]["execution_graph"]["edges"])
    assert saved["settings"]["execution_graph"]["entry"] is None
    assert "workflow.graph.entry_missing" in codes(result.issues)


def test_group_delete_needs_with_contents() -> None:
    store = _Store(workflow(enabled=False, groups=[{"group_id": "g", "body": {}}]))
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=[{"op": "delete_group", "group_id": "g"}],
            author="t",
        )
    assert codes(raised.value.issues) == ["workflow.change.group_delete_contents"]


def test_config_operation_cannot_change_structure() -> None:
    store = _Store(runnable())
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=[{"op": "update_step_config", "step_id": "a", "patch": {"group_id": "g"}}],
            author="t",
        )
    assert codes(raised.value.issues) == ["workflow.change.structure_in_config"]


def test_config_operation_rejects_unknown_config_keys() -> None:
    store = _Store(runnable())
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=[
                {
                    "op": "update_step_config",
                    "step_id": "a",
                    "patch": {
                        "override": {"http_request": {"url": "https://x", "target_phase": "p"}}
                    },
                }
            ],
            author="t",
        )
    assert codes(raised.value.issues) == ["workflow.step_config_key_unknown"]


def test_set_entry_of_a_lane() -> None:
    groups = [
        {
            "group_id": "p",
            "body": {"kind": "lanes", "lanes": [{"lane_id": "left", "entry": ref("x1")}]},
        }
    ]
    store = _Store(
        workflow(
            enabled=False,
            steps=[
                {**step("x1"), "group_id": "p", "lane_id": "left"},
                {**step("x2"), "group_id": "p", "lane_id": "left"},
            ],
            groups=groups,
        )
    )
    apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[{"op": "set_entry", "group_id": "p", "lane_id": "left", "target": ref("x2")}],
        author="t",
    )
    assert store.saves[-1]["groups"][0]["body"]["lanes"][0]["entry"] == ref("x2")


def test_service_owned_graph_survives_a_metadata_save() -> None:
    stored = {"settings": {"execution_graph": {"schema_version": 1}}}
    payload = {"settings": {"execution_graph": {"forged": True}, "participants": []}}
    assert preserve_service_owned_settings(payload, stored)["settings"]["execution_graph"] == {
        "schema_version": 1
    }
    assert (
        "execution_graph"
        not in preserve_service_owned_settings(payload, {"settings": {}})["settings"]
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_enabled_edit_refuses_preexisting_readiness_issue(dry_run: bool) -> None:
    store = _Store(runnable())
    original = copy.deepcopy(store.workflows["wf"])
    issues = [{"code": "workflow.connection_missing", "path": "steps.a", "message": "gone"}]
    kwargs = {
        "expected_revision": 3,
        "operations": [
            {"op": "update_step_config", "step_id": "a", "patch": {"display_name": "A"}}
        ],
        "author": "t",
        "readiness": lambda candidate: list(issues),
        "dry_run": dry_run,
    }
    if dry_run:
        result = apply_workflow_changes(store, "wf", **kwargs)
        assert result.blocking_issues == issues
    else:
        with pytest.raises(WorkflowDefinitionChangeError) as raised:
            apply_workflow_changes(store, "wf", **kwargs)
        assert raised.value.issues == issues
    assert store.saves == []
    assert store.workflows["wf"] == original


def test_enabled_edit_can_repair_all_readiness_issues() -> None:
    store = _Store(runnable())
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[
            {"op": "update_step_config", "step_id": "a", "patch": {"display_name": "Repaired"}}
        ],
        author="t",
        readiness=lambda candidate: (
            [] if candidate["steps"][0].get("display_name") == "Repaired" else [{"code": "missing"}]
        ),
    )
    assert result.valid
    assert result.revision == 4


def test_enabled_refuses_a_structural_issue_it_already_had() -> None:
    broken = runnable()
    broken["steps"].append(step("orphan", "stop_fail"))
    store = _Store(broken)
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=[
                {"op": "update_step_config", "step_id": "a", "patch": {"display_name": "A"}}
            ],
            author="t",
        )
    assert codes(raised.value.issues) == ["workflow.graph.unreachable"]
    assert store.saves == []
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[{"op": "delete_step", "step_id": "orphan"}],
        author="t",
    )
    assert result.valid


class _CreateStore:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    def save_workflow(self, data: dict, author: str = "admin", *, create_only: bool = False) -> int:
        assert create_only is True
        self.saved.append(copy.deepcopy(data))
        return 1


def test_create_with_definition_refuses_an_enabled_broken_definition() -> None:
    from core.application.workflows.workflow_definition_service import (
        create_workflow_with_definition,
    )

    store = _CreateStore()
    broken = {
        "workflow_id": "wf",
        "enabled": True,
        "settings": {},
        "steps": [step("a")],
        "groups": [],
    }
    with pytest.raises(WorkflowDefinitionChangeError):
        create_workflow_with_definition(store, broken, author="import")
    assert store.saved == []

    result = create_workflow_with_definition(store, {**broken, "enabled": False}, author="import")
    assert "workflow.graph.missing" not in codes(result.issues)
    assert store.saved[0]["settings"]["execution_graph"]["entry"] is None
    assert "execution_graph" not in store.saved[0]


def test_create_with_definition_writes_the_graph_into_settings() -> None:
    from core.application.workflows.workflow_definition_service import (
        create_workflow_with_definition,
    )

    store = _CreateStore()
    graph = {"schema_version": 1, "entry": ref("a"), "edges": [edge("a", ref("end"))]}
    result = create_workflow_with_definition(
        store,
        {
            "workflow_id": "wf",
            "enabled": True,
            "settings": {},
            "steps": [step("a"), step("end", "stop_success")],
            "groups": [],
            "execution_graph": graph,
        },
        author="import",
    )
    assert result.valid
    assert store.saved[0]["settings"]["execution_graph"] == graph


def _kind_change_store() -> _Store:
    return _Store(
        workflow(
            enabled=False,
            steps=[
                step(
                    "a",
                    display_name="Fetch",
                    error_policy={"on_error": "route_to_step"},
                    override={"http_request": {"url": "https://example.test"}},
                ),
                step("end", "stop_success"),
                step("failed", "stop_fail"),
            ],
            graph={
                "schema_version": 1,
                "entry": ref("a"),
                "edges": [edge("a", ref("end")), edge("a", ref("failed"), "error")],
            },
        )
    )


def test_a_kind_change_keeps_identity_and_drops_only_ports_the_new_kind_lacks() -> None:
    store = _kind_change_store()

    apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[{"op": "change_step_kind", "step_id": "a", "step": {"step_kind": "if"}}],
        author="t",
    )

    saved = store.saves[-1]
    changed = next(item for item in saved["steps"] if item["step_id"] == "a")
    assert changed["step_kind"] == "if"
    assert changed["display_name"] == "Fetch"
    assert changed["error_policy"] == {"on_error": "route_to_step"}
    assert "override" not in changed, "the old kind's configuration does not survive"
    edges = saved["settings"]["execution_graph"]["edges"]
    assert [(item["outcome"]["name"], item["target"]["id"]) for item in edges] == [
        ("error", "failed")
    ]


def test_a_step_that_becomes_a_stop_loses_its_error_edge_too() -> None:
    store = _kind_change_store()

    apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[
            {"op": "change_step_kind", "step_id": "a", "step": {"step_kind": "stop_success"}}
        ],
        author="t",
    )

    assert store.saves[-1]["settings"]["execution_graph"]["edges"] == []


def test_a_config_change_disables_and_enables_a_step() -> None:
    store = _Store(runnable())

    disabled = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[{"op": "update_step_config", "step_id": "a", "patch": {"disabled": True}}],
        author="t",
    )

    assert disabled.issues == []
    assert store.saves[-1]["steps"][0]["disabled"] is True

    apply_workflow_changes(
        store,
        "wf",
        expected_revision=4,
        operations=[{"op": "update_step_config", "step_id": "a", "patch": {"disabled": False}}],
        author="t",
    )

    assert store.saves[-1]["steps"][0]["disabled"] is False


def test_an_enabled_workflow_refuses_a_disabled_stop() -> None:
    store = _Store(runnable())
    with pytest.raises(WorkflowDefinitionChangeError) as refused:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=[
                {"op": "update_step_config", "step_id": "end", "patch": {"disabled": True}}
            ],
            author="t",
        )
    issue = next(
        item for item in refused.value.issues if item["code"] == "workflow.step.disabled_needs_next"
    )
    assert issue["message"] == "Only a step with one Next outcome can be disabled."
    assert store.saves == []


def test_a_created_step_carries_its_disabled_flag() -> None:
    store = _Store(workflow(enabled=False))
    apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[{"op": "create_step", "step": step("a", disabled=True)}],
        author="t",
    )
    assert store.saves[-1]["steps"][0]["disabled"] is True


def test_an_inserted_step_carries_its_disabled_flag() -> None:
    store = _Store(runnable())
    apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[
            {
                "op": "insert_step_between",
                "source": ref("a"),
                "outcome": {"name": "success"},
                "step": step("mid", "set_variable", disabled=True),
            }
        ],
        author="t",
    )
    inserted = next(item for item in store.saves[-1]["steps"] if item["step_id"] == "mid")
    assert inserted["disabled"] is True


def test_a_kind_change_keeps_the_disabled_flag() -> None:
    store = _kind_change_store()
    store.workflows["wf"]["steps"][0]["disabled"] = True

    apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[{"op": "change_step_kind", "step_id": "a", "step": {"step_kind": "transform"}}],
        author="t",
    )

    changed = next(item for item in store.saves[-1]["steps"] if item["step_id"] == "a")
    assert changed["step_kind"] == "transform"
    assert changed["disabled"] is True


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "workflow.change.step_kind_missing"),
        ({"step_kind": "if", "group_id": "g"}, "workflow.change.structure_in_config"),
        ({"step_kind": "if", "colour": "red"}, "workflow.change.field_unknown"),
    ],
)
def test_a_kind_change_cannot_move_the_step_or_carry_unknown_fields(payload, code) -> None:
    store = _kind_change_store()
    with pytest.raises(WorkflowDefinitionChangeError) as refused:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=[{"op": "change_step_kind", "step_id": "a", "step": payload}],
            author="t",
        )
    assert code in codes(refused.value.issues)
    assert store.saves == []


def test_an_issue_reported_by_both_validator_and_readiness_is_listed_once() -> None:
    def readiness(candidate: dict) -> list[dict]:
        from core.application.workflows.workflow_definition_service import (
            definition_from_workflow,
        )
        from core.application.workflows.workflow_definition_validation import (
            validate_workflow_definition,
        )

        return list(validate_workflow_definition(definition_from_workflow(candidate)))

    store = _Store(runnable())
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[
            {"op": "create_step", "step": step("b")},
            {"op": "connect", "edge": edge("a", ref("b")), "replace": True},
        ],
        author="t",
        readiness=readiness,
        dry_run=True,
    )

    keys = [(item["code"], item["path"]) for item in result.issues]
    assert keys
    assert len(keys) == len(set(keys))
    assert len(result.blocking_issues) == len(
        {(item["code"], item["path"]) for item in result.blocking_issues}
    )


# --- For each container (ADR 0038) -------------------------------------------


def _exit(kind: str, owner: str) -> dict:
    key = "group_id" if kind == "group_exit" else "for_each_id"
    return {"kind": kind, key: owner, "lane_id": None}


def _connect(source: str, target: dict, outcome: str = "success", kind: str = "step") -> dict:
    return {
        "op": "connect",
        "edge": {"source": ref(source, kind), "outcome": {"name": outcome}, "target": target},
    }


def build_group_for_each_collect() -> list[dict]:
    """Group → For each → If → Collect | Continue, created as one change set."""
    return [
        {"op": "create_group", "group": {"group_id": "g", "body": {"kind": "sequence"}}},
        {"op": "create_step", "step": {**step("prepare", "set_variable"), "group_id": "g"}},
        {"op": "set_entry", "group_id": "g", "target": ref("prepare")},
        {
            "op": "create_for_each",
            "for_each": {
                "for_each_id": "fe",
                "group_id": "g",
                "over": "state.orders",
                "item_name": "order",
                "index_name": "order_index",
                "body": {"kind": "sequence"},
            },
        },
        {
            "op": "create_step",
            "step": {
                **step("check", "if", override={"if": {"condition": "state.order.ok"}}),
                "for_each_id": "fe",
            },
        },
        {
            "op": "create_step",
            "step": {
                **step(
                    "keep",
                    "collect",
                    override={"collect": {"target": "state.emails", "source": "state.order.email"}},
                ),
                "for_each_id": "fe",
            },
        },
        {"op": "create_step", "step": {**step("skip", "continue"), "for_each_id": "fe"}},
        {"op": "create_step", "step": step("done", "stop_success")},
        {"op": "set_entry", "for_each_id": "fe", "target": ref("check")},
        _connect("prepare", ref("fe", "for_each")),
        _connect("fe", _exit("group_exit", "g"), kind="for_each"),
        _connect("check", ref("keep"), "true"),
        _connect("check", ref("skip"), "false"),
        _connect("keep", _exit("for_each_exit", "fe")),
        _connect("g", ref("done"), kind="group"),
    ]


def test_group_for_each_and_controls_are_one_valid_change_set() -> None:
    store = _Store(workflow(enabled=False))
    result = apply_workflow_changes(
        store, "wf", expected_revision=3, operations=build_group_for_each_collect(), author="t"
    )
    assert result.issues == []
    saved = store.saves[-1]
    assert saved["settings"]["execution_graph"]["entry"] == ref("g", "group")
    assert saved["groups"] == [
        {"group_id": "g", "body": {"kind": "sequence", "entry": ref("prepare")}}
    ]
    assert [item["for_each_id"] for item in saved["for_each"]] == ["fe"]
    assert saved["for_each"][0]["group_id"] == "g"
    owners = {s["step_id"]: (s["group_id"], s["for_each_id"]) for s in saved["steps"]}
    assert owners["prepare"] == ("g", None)
    assert owners["keep"] == (None, "fe")


def test_a_loop_group_is_refused_even_on_a_disabled_workflow() -> None:
    store = _Store(workflow(enabled=False))
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=[
                {
                    "op": "create_group",
                    "group": {
                        "group_id": "g",
                        "kind": "loop",
                        "loop": {"over": "state.x"},
                        "body": {"kind": "sequence"},
                    },
                }
            ],
            author="t",
        )
    assert codes(raised.value.issues) == ["workflow.group.loop_removed"]
    assert store.saves == []


def test_a_stored_inline_for_each_step_blocks_every_change() -> None:
    legacy = workflow(
        enabled=False,
        steps=[step("a", "for_each", override={"for_each": {"source_path": "state.x"}})],
    )
    store = _Store(legacy)
    operations = [{"op": "create_step", "step": step("b")}]
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=3,
            operations=operations,
            author="t",
        )
    assert codes(raised.value.issues) == ["workflow.step.for_each_inline_removed"]


def test_deleting_a_group_removes_its_for_each_and_body() -> None:
    store = _Store(workflow(enabled=False))
    apply_workflow_changes(
        store, "wf", expected_revision=3, operations=build_group_for_each_collect(), author="t"
    )
    apply_workflow_changes(
        store,
        "wf",
        expected_revision=4,
        operations=[{"op": "delete_group", "group_id": "g", "with_contents": True}],
        author="t",
    )
    saved = store.saves[-1]
    assert saved["for_each"] == []
    assert [s["step_id"] for s in saved["steps"]] == ["done"]
    assert saved["settings"]["execution_graph"]["edges"] == []


def test_moving_a_for_each_to_the_root_drops_its_edges() -> None:
    store = _Store(workflow(enabled=False))
    apply_workflow_changes(
        store, "wf", expected_revision=3, operations=build_group_for_each_collect(), author="t"
    )
    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=4,
        operations=[{"op": "move_for_each", "for_each_id": "fe", "group_id": None}],
        author="t",
    )
    saved = store.saves[-1]
    assert saved["for_each"][0]["group_id"] is None
    edges = saved["settings"]["execution_graph"]["edges"]
    assert all(ref("fe", "for_each") not in (e["source"], e["target"]) for e in edges)
    assert "workflow.graph.unreachable" in codes(result.issues)


def test_for_each_config_cannot_move_it_and_rejects_unknown_fields() -> None:
    store = _Store(workflow(enabled=False))
    apply_workflow_changes(
        store, "wf", expected_revision=3, operations=build_group_for_each_collect(), author="t"
    )
    for patch, code in (
        ({"group_id": None}, "workflow.change.structure_in_config"),
        ({"loop": {}}, "workflow.change.field_unknown"),
    ):
        with pytest.raises(WorkflowDefinitionChangeError) as raised:
            apply_workflow_changes(
                store,
                "wf",
                expected_revision=4,
                operations=[{"op": "update_for_each_config", "for_each_id": "fe", "patch": patch}],
                author="t",
            )
        assert codes(raised.value.issues) == [code]


def test_a_step_cannot_be_moved_into_two_containers() -> None:
    store = _Store(workflow(enabled=False))
    apply_workflow_changes(
        store, "wf", expected_revision=3, operations=build_group_for_each_collect(), author="t"
    )
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=4,
            operations=[
                {"op": "move_step", "step_id": "done", "group_id": "g", "for_each_id": "fe"}
            ],
            author="t",
        )
    assert codes(raised.value.issues) == ["workflow.step.owner_conflict"]


# --- builder change sets that an in-place edit left stale ---------------------------
# The builder stages each operation against its preview of the set so far. Deleting a
# step the set created removes its create and its connections, and editing a staged step
# can change its kind; operations staged later still name the edges those removed. The
# engine refuses such a set whole, so the builder restates it (reconcileChangeOperations
# in assets/frontend/src/pages/workflows/builder/change-set.ts). Each case pairs the
# sequence the builder used to send with the one it sends now; the frontend test
# "builder change sets that an in-place edit left stale" pins the same sequences.


def _in_group(step_id: str, kind: str) -> dict:
    return {**step(step_id, kind), "group_id": "test_group", "for_each_id": None, "lane_id": None}


def _group_with_if_and_http() -> list[dict]:
    return [
        {
            "op": "create_group",
            "group": {
                "group_id": "test_group",
                "display_name": "Test Group",
                "body": {"kind": "sequence"},
            },
        },
        {"op": "create_step", "step": _in_group("if", "if")},
        {"op": "set_entry", "target": ref("if"), "group_id": "test_group"},
        {"op": "create_step", "step": _in_group("http_request", "http_request")},
        _connect("if", ref("http_request"), "true"),
    ]


def _stale_change_sets() -> list[tuple[str, dict, list[dict], str, list[dict]]]:
    """(case, workflow, stale set, refusal code, the set the builder sends instead)."""
    group_build = [
        *_group_with_if_and_http(),
        {"op": "create_step", "step": _in_group("approval", "approval")},
        _connect("http_request", ref("approval")),
    ]
    insert_note = {
        "op": "insert_step_between",
        "source": ref("http_request"),
        "outcome": {"name": "success"},
        "step": step("note", "set_variable"),
    }
    insert_gate = {
        "op": "insert_step_between",
        "source": ref("if"),
        "outcome": {"name": "true"},
        "step": step("gate", "approval"),
    }
    return [
        (
            # Stand repro: Add after Approval → Tmp, disconnect Approved, delete Tmp.
            "disconnect of an edge to a deleted new step",
            workflow(enabled=False),
            [
                *group_build,
                {"op": "disconnect", "source": ref("approval"), "outcome": {"name": "approved"}},
            ],
            "workflow.change.edge_unknown",
            group_build,
        ),
        (
            # Insert a step on HTTP → Approval, then delete the new Approval.
            "insert on an edge to a deleted new step",
            workflow(enabled=False),
            [*_group_with_if_and_http(), insert_note],
            "workflow.change.edge_unknown",
            [
                *_group_with_if_and_http(),
                {"op": "create_step", "step": _in_group("note", "set_variable")},
                _connect("http_request", ref("note")),
            ],
        ),
        (
            # Insert a step on If.true → HTTP, then give the new step a branching kind.
            "insert of a step whose kind became branching",
            workflow(enabled=False),
            [*_group_with_if_and_http(), insert_gate],
            "workflow.change.insert_kind_unsupported",
            [
                *_group_with_if_and_http(),
                {"op": "create_step", "step": _in_group("gate", "approval")},
                {**_connect("if", ref("gate"), "true"), "replace": True},
            ],
        ),
        (
            # Change a saved step's kind, disconnect its Next, then re-edit the kind to If.
            "disconnect of an edge a re-edited kind change drops",
            runnable(),
            [
                {"op": "change_step_kind", "step_id": "a", "step": step("a", "if")},
                {"op": "disconnect", "source": ref("a"), "outcome": {"name": "success"}},
            ],
            "workflow.change.edge_unknown",
            [{"op": "change_step_kind", "step_id": "a", "step": step("a", "if")}],
        ),
    ]


@pytest.mark.parametrize(
    ("stored", "stale", "code", "restated"),
    [case[1:] for case in _stale_change_sets()],
    ids=[case[0] for case in _stale_change_sets()],
)
def test_a_stale_builder_set_is_refused_and_its_restated_form_applies(
    stored: dict, stale: list[dict], code: str, restated: list[dict]
) -> None:
    stored = {**stored, "enabled": False}
    stale_store = _Store(stored)
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            stale_store, "wf", expected_revision=3, operations=stale, author="t", dry_run=True
        )
    assert codes(raised.value.issues) == [code]

    dry = apply_workflow_changes(
        _Store(stored), "wf", expected_revision=3, operations=restated, author="t", dry_run=True
    )
    assert dry.blocking_issues == []
    store = _Store(stored)
    result = apply_workflow_changes(
        store, "wf", expected_revision=3, operations=restated, author="t"
    )
    # A disabled draft is stored with the issues it keeps.
    assert result.revision == 4
    assert len(store.saves) == 1


# DEV-HOOK-02: mode is a server policy, independent of enabled state and writer.
_DECLARATION_MODE_CODE = "workflow.hook.declaration_requires_development"


def _mode_hook_step(step_id="before", **changes):
    from tests.unit.test_workflow_hook_contract import declaration

    contract = declaration(
        input_schema={"type": "object", "properties": {}},
        input_mapping={},
        mutable_fields=[],
        additional_outputs={},
    )
    contract.update(changes)
    return step(step_id, "invoke_workflow", override={"invoke_workflow": {"hook": contract}})


def _assert_declaration_mode_issue(issues):
    matches = [issue for issue in issues if issue.get("code") == _DECLARATION_MODE_CODE]
    assert matches, issues
    assert all(issue.get("severity") == "error" and issue.get("path") for issue in matches)


def _mode_candidate(shape):
    original = runnable()
    original["enabled"] = False
    declaration = _mode_hook_step()
    if shape == "insert_step_between":
        operations = [
            {"op": shape, "source": ref("a"), "outcome": {"name": "success"}, "step": declaration}
        ]
    elif shape == "create_step":
        operations = [
            {"op": shape, "step": declaration},
            {"op": "connect", "edge": edge("a", ref("before")), "replace": True},
            {"op": "connect", "edge": edge("before", ref("end"))},
        ]
    elif shape == "change_step_kind":
        operations = [
            {
                "op": shape,
                "step_id": "a",
                "step": {key: value for key, value in declaration.items() if key != "step_id"},
            }
        ]
    else:
        original["steps"][0] = step(
            "a", "invoke_workflow", override={"invoke_workflow": {"workflow_id": "ordinary_child"}}
        )
        operations = [
            {
                "op": "update_step_config",
                "step_id": "a",
                "patch": {"override": declaration["override"]},
            }
        ]
    return original, operations


@pytest.mark.parametrize(
    "shape", ["create_step", "insert_step_between", "change_step_kind", "update_step_config"]
)
def test_production_refuses_new_hook_declaration_through_changes(monkeypatch, shape):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "production")
    original, operations = _mode_candidate(shape)
    store = _Store(original)
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store, "wf", expected_revision=3, operations=operations, author="admin"
        )
    _assert_declaration_mode_issue(raised.value.issues)
    assert store.saves == []
    assert store.get_workflow("wf") == original


@pytest.mark.parametrize("mode", ["production", "development"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_hook_declaration_mode_policy_matches_dry_run(monkeypatch, mode, dry_run):
    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    original, operations = _mode_candidate("insert_step_between")
    store = _Store(original)
    if mode == "production" and not dry_run:
        with pytest.raises(WorkflowDefinitionChangeError) as raised:
            apply_workflow_changes(
                store,
                "wf",
                expected_revision=3,
                operations=operations,
                dry_run=dry_run,
                author="admin",
            )
        _assert_declaration_mode_issue(raised.value.issues)
    else:
        result = apply_workflow_changes(
            store, "wf", expected_revision=3, operations=operations, dry_run=dry_run, author="admin"
        )
        if mode == "production":
            _assert_declaration_mode_issue(result.blocking_issues)
            assert result.applied is False
        else:
            assert result.blocking_issues == []
            assert result.applied is (not dry_run)
    assert len(store.saves) == int(mode == "development" and not dry_run)
    if not store.saves:
        assert store.get_workflow("wf") == original


@pytest.mark.parametrize("mode", ["production", "development"])
@pytest.mark.parametrize("enabled", [False, True])
def test_new_workflow_with_hook_requires_development_even_when_disabled(monkeypatch, mode, enabled):
    from core.application.workflows.workflow_definition_service import (
        create_workflow_with_definition,
    )

    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    payload = workflow(enabled=enabled, steps=[_mode_hook_step(), step("end", "stop_success")])
    payload["execution_graph"] = {
        "schema_version": 1,
        "entry": ref("before"),
        "edges": [edge("before", ref("end"))],
    }
    store = _CreateStore()
    if mode == "production":
        with pytest.raises(WorkflowDefinitionChangeError) as raised:
            create_workflow_with_definition(store, payload, author="admin")
        _assert_declaration_mode_issue(raised.value.issues)
        assert store.saved == []
    else:
        result = create_workflow_with_definition(store, payload, author="admin")
        assert result.valid
        assert len(store.saved) == 1
        assert store.saved[0]["enabled"] is enabled


@pytest.mark.parametrize(
    "operation", ["edit_same_identity_contract_and_label", "delete", "move_at_root"]
)
def test_production_keeps_existing_personal_hook_operations(monkeypatch, operation):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "production")
    original = workflow(
        enabled=False,
        steps=[step("a"), _mode_hook_step(), step("end", "stop_success")],
        graph={
            "schema_version": 1,
            "entry": ref("a"),
            "edges": [edge("a", ref("before")), edge("before", ref("end"))],
        },
    )
    if operation == "edit_same_identity_contract_and_label":
        edited = _mode_hook_step(
            display_name="Personal hook",
            description="Edited contract",
            additional_outputs={"flag": {"schema": {"type": "boolean"}, "default": False}},
        )
        requested_hook = copy.deepcopy(edited["override"]["invoke_workflow"]["hook"])
        operations = [
            {
                "op": "update_step_config",
                "step_id": "before",
                "patch": {"override": edited["override"]},
            }
        ]
    elif operation == "delete":
        operations = [
            {"op": "delete_step", "step_id": "before"},
            {"op": "connect", "edge": edge("a", ref("end"))},
        ]
    else:
        operations = [
            {
                "op": "move_step",
                "step_id": "before",
                "group_id": None,
                "for_each_id": None,
                "lane_id": None,
            },
            {"op": "connect", "edge": edge("a", ref("before")), "replace": True},
            {"op": "connect", "edge": edge("before", ref("end")), "replace": True},
        ]
    store = _Store(original)
    result = apply_workflow_changes(
        store, "wf", expected_revision=3, operations=operations, author="personal_owner"
    )
    assert result.applied
    assert result.revision == 4
    assert _DECLARATION_MODE_CODE not in codes(result.issues)
    assert len(store.saves) == 1
    remaining = [item for item in store.saves[0]["steps"] if item["step_id"] == "before"]
    if operation == "delete":
        assert remaining == []
    else:
        assert remaining[0]["override"]["invoke_workflow"]["hook"]["hook_id"] == "before_invoice"
        if operation == "edit_same_identity_contract_and_label":
            saved_hook = remaining[0]["override"]["invoke_workflow"]["hook"]
            assert saved_hook == requested_hook
            stored = store.get_workflow("wf")
            stored_step = next(item for item in stored["steps"] if item["step_id"] == "before")
            assert stored_step["override"]["invoke_workflow"]["hook"] == requested_hook
            assert (
                saved_hook["input_schema"]
                == original["steps"][1]["override"]["invoke_workflow"]["hook"]["input_schema"]
            )
            assert saved_hook["additional_outputs"]["flag"]["default"] is False


@pytest.mark.parametrize(
    "shape",
    [
        "rename_hook_id",
        "delete_old_add_new",
        "copy_same_hook_id_new_carrier",
        "partial_hook_missing_id",
    ],
)
def test_production_refuses_new_hook_identity_hidden_in_candidate(monkeypatch, shape):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "production")
    original = workflow(
        enabled=False,
        steps=[_mode_hook_step(), step("end", "stop_success")],
        graph={"schema_version": 1, "entry": ref("before"), "edges": [edge("before", ref("end"))]},
    )
    if shape == "rename_hook_id":
        operations = [
            {
                "op": "update_step_config",
                "step_id": "before",
                "patch": {"override": _mode_hook_step(hook_id="another_hook")["override"]},
            }
        ]
    elif shape == "delete_old_add_new":
        operations = [
            {"op": "delete_step", "step_id": "before"},
            {"op": "create_step", "step": _mode_hook_step("replacement")},
            {"op": "set_entry", "target": ref("replacement")},
            {"op": "connect", "edge": edge("replacement", ref("end"))},
        ]
    else:
        new_step = _mode_hook_step("copy")
        if shape == "partial_hook_missing_id":
            new_step["override"]["invoke_workflow"]["hook"].pop("hook_id")
        operations = [
            {
                "op": "insert_step_between",
                "source": ref("before"),
                "outcome": {"name": "success"},
                "step": new_step,
            }
        ]
    store = _Store(original)
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        apply_workflow_changes(
            store, "wf", expected_revision=3, operations=operations, author="admin"
        )
    _assert_declaration_mode_issue(raised.value.issues)
    assert store.saves == []
    assert store.get_workflow("wf") == original


# The registry readiness adapter projects structural paths under "workflow.".
# Keep this preview contract after the DEV-HOOK mode guards above.
def _preview_with_projected_structural_issue(
    *, enabled: bool, additional_readiness: dict | None = None
) -> tuple[Any, _Store, dict]:
    from core.application.registry.contract_validation_workflow_readiness import (
        _workflow_issue_path,
    )
    from core.application.workflows.workflow_definition_service import (
        definition_from_workflow,
        readiness_from_validator,
    )
    from core.application.workflows.workflow_definition_validation import (
        validate_workflow_definition,
    )

    store = _Store(workflow(enabled=enabled))
    original = copy.deepcopy(store.workflows["wf"])

    def validate(candidate_store, workflow_id):
        candidate = candidate_store.get_workflow(workflow_id)
        structural = list(validate_workflow_definition(definition_from_workflow(candidate)))
        assert structural == [_preview_missing_outcome_issue()]
        projected = [
            {**issue, "path": _workflow_issue_path(issue.get("path"))} for issue in structural
        ]
        assert projected[0]["path"] == "workflow.steps.a"
        return {
            "errors": projected
            + ([copy.deepcopy(additional_readiness)] if additional_readiness is not None else [])
        }

    result = apply_workflow_changes(
        store,
        "wf",
        expected_revision=3,
        operations=[{"op": "create_step", "step": step("a")}],
        author="t",
        readiness=readiness_from_validator(store, validate),
        dry_run=True,
    )
    return result, store, original


def _preview_missing_outcome_issue() -> dict:
    return {
        "code": "workflow.graph.outcome_missing",
        "path": "steps.a",
        "message": "Step 'a' has no edge for its success outcome",
        "severity": "error",
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_preview_lists_a_structural_issue_and_its_readiness_projection_once(enabled: bool) -> None:
    result, store, original = _preview_with_projected_structural_issue(enabled=enabled)

    assert len(result.issues) == 1
    assert result.issues == [_preview_missing_outcome_issue()]
    assert result.blocking_issues == (result.issues if enabled else [])
    assert result.valid is False
    assert result.applied is False
    assert result.revision == 3
    assert [item["step_id"] for item in result.workflow["steps"]] == ["a"]
    assert store.saves == []
    assert store.get_workflow("wf") == original


@pytest.mark.parametrize(
    "different_record",
    [
        pytest.param({"path": "workflow.steps.other"}, id="distinct-owner"),
        pytest.param(
            {"message_key": "workflow.issue_specific_translation"}, id="localization-provenance"
        ),
        pytest.param({"message_params": {"owner": "other"}}, id="localization-parameters"),
    ],
)
def test_preview_keeps_a_distinct_readiness_record_with_the_same_visible_message(
    different_record: dict,
) -> None:
    structural = _preview_missing_outcome_issue()
    distinct = {**structural, "path": "workflow.steps.a", **different_record}
    result, store, original = _preview_with_projected_structural_issue(
        enabled=True, additional_readiness=distinct
    )

    assert len(result.issues) == 2
    assert result.issues == [structural, distinct]
    assert [issue["message"] for issue in result.issues] == [
        structural["message"],
        structural["message"],
    ]
    assert result.blocking_issues == [structural, distinct]
    assert result.valid is False
    assert result.applied is False
    assert result.revision == 3
    assert store.saves == []
    assert store.get_workflow("wf") == original


def test_enabled_local_create_then_delete_applies_one_unchanged_plan_done_graph() -> None:
    """Undoable local Delete may retain its create op until the author applies."""
    store = _Store(
        workflow(
            enabled=True,
            steps=[
                step("plan", display_name="Plan work", optional=False),
                step("done", "stop_success", display_name="Done"),
            ],
            graph={
                "schema_version": 1,
                "entry": ref("plan"),
                "edges": [edge("plan", ref("done"))],
            },
        )
    )
    operations = [
        {
            "op": "create_step",
            "step": step(
                "draft_check",
                "if",
                display_name="Temporary check",
                override={"if": {"condition": "state.order.ok"}},
            ),
        },
        {"op": "delete_step", "step_id": "draft_check"},
    ]

    result = apply_workflow_changes(
        store, "wf", expected_revision=3, operations=operations, author="t"
    )

    assert result.applied is True
    assert result.valid is True
    assert result.revision == 4
    assert len(store.saves) == 1
    saved = store.saves[0]
    assert {item["step_id"] for item in saved["steps"]} == {"plan", "done"}
    plan = next(item for item in saved["steps"] if item["step_id"] == "plan")
    done = next(item for item in saved["steps"] if item["step_id"] == "done")
    assert (plan["step_kind"], plan["display_name"], plan["optional"]) == (
        "http_request",
        "Plan work",
        False,
    )
    assert (done["step_kind"], done["display_name"]) == ("stop_success", "Done")
    graph = saved["settings"]["execution_graph"]
    assert graph["entry"] == ref("plan")
    assert [(item["source"]["id"], item["target"]["id"]) for item in graph["edges"]] == [
        ("plan", "done")
    ]
    assert store.workflows["wf"]["revision"] == 4
    assert {item["step_id"] for item in store.workflows["wf"]["steps"]} == {"plan", "done"}


@pytest.mark.parametrize("example_id", ["group_for_each_atomic", "root_hook_atomic"])
def test_discovery_operation_examples_match_shared_dry_run_and_apply(monkeypatch, example_id):
    if example_id == "root_hook_atomic":
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("FS_STACK_ENVIRONMENT", "development")
    examples = WorkflowAuthoringSchema().get()["definition_guidance"]["change_set"][
        "partial_examples"
    ]
    example = next(row for row in examples if row["id"] == example_id)
    bundle = WorkflowPortabilityService(object()).parse_bundle(example["precondition_bundle_yaml"])
    assert len(bundle["workflows"]) == 1
    candidate = bundle["workflows"][0]
    assert candidate["enabled"] is False
    original = workflow(
        enabled=False,
        steps=candidate["steps"],
        graph=candidate["execution_graph"],
        groups=candidate.get("groups", []),
        for_each=candidate.get("for_each", []),
    )
    store = _Store(original)
    revision = original["revision"]
    assert example["dry_run"] is True
    dry = apply_workflow_changes(
        store,
        "wf",
        expected_revision=revision,
        operations=example["operations"],
        author="t",
        dry_run=True,
    )
    assert dry.applied is False
    assert dry.issues == []
    assert dry.blocking_issues == []
    assert store.saves == []
    assert store.get_workflow("wf") == original

    applied = apply_workflow_changes(
        store,
        "wf",
        expected_revision=revision,
        operations=example["operations"],
        author="t",
    )
    assert applied.applied is True
    assert applied.issues == []
    assert applied.revision == revision + 1
    assert len(store.saves) == 1
    saved = store.saves[0]
    if example_id == "group_for_each_atomic":
        assert [row["group_id"] for row in saved["groups"]] == ["review_group"]
        assert [row["for_each_id"] for row in saved["for_each"]] == ["items_loop"]
        assert saved["groups"][0]["body"]["entry"] == ref("items_loop", "for_each")
        assert saved["for_each"][0]["body"]["entry"] == ref("item_work")
    else:
        carriers = [step for step in saved["steps"] if step["step_id"] == "review"]
        assert len(carriers) == 1
        assert carriers[0]["override"]["invoke_workflow"]["hook"]["hook_id"] == "review_extension"
        assert "workflow_id" not in carriers[0]["override"]["invoke_workflow"]
    with pytest.raises(WorkflowRevisionConflictError):
        apply_workflow_changes(
            store,
            "wf",
            expected_revision=revision,
            operations=example["operations"],
            author="t",
            dry_run=True,
        )
    assert len(store.saves) == 1
