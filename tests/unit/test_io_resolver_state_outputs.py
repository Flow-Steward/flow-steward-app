"""Extraction needs to distinguish state writes from message-only outputs."""

from types import SimpleNamespace

import pytest

from core.application.registry.io_resolver import IOResolver
from tests._workflow_definitions import linear_workflow


def resolver(monkeypatch, step):
    db = SimpleNamespace(get_workflow=lambda *_args, **_kwargs: {"steps": [step]})
    result = IOResolver(db)
    monkeypatch.setattr(result, "_resolve_published_block_contract", lambda **_kwargs: None)
    monkeypatch.setattr(
        result,
        "_merge_step_output_contract",
        lambda **_kwargs: [{"name": "result", "type": "string", "source": "published_block"}],
    )
    return result


def test_message_output_is_not_mistaken_for_a_state_write(monkeypatch):
    io = resolver(
        monkeypatch,
        {
            "step_id": "produce",
            "step_kind": "template",
            "output_mapping_jsonb": {"result": "state.saved"},
        },
    )
    assert {row["name"] for row in io.get_step_outputs("wf", "produce")} == {"result", "saved"}
    assert {row["name"] for row in io.get_step_state_outputs("wf", "produce")} == {"saved"}


def test_nested_state_targets_keep_root_and_leaf_type_evidence(monkeypatch):
    io = resolver(
        monkeypatch,
        {
            "step_id": "produce",
            "step_kind": "template",
            "output_mapping_jsonb": {"result": "state.customer.result"},
        },
    )
    rows = {row["name"]: row["type"] for row in io.get_step_state_outputs("wf", "produce")}
    assert rows == {"customer": "object", "customer.result": "string"}


def test_child_message_alias_and_state_mapping_remain_distinct(monkeypatch):
    io = resolver(
        monkeypatch,
        {
            "step_id": "produce",
            "step_kind": "invoke_workflow",
            "override": {
                "invoke_workflow": {
                    "workflow_id": "child",
                    "output_mapping": {"result": "message_only", "id": "state.saved_id"},
                }
            },
        },
    )
    monkeypatch.setattr(
        io, "_get_callable_output_types", lambda _workflow: {"result": "string", "id": "number"}
    )
    rows = {row["name"]: row["type"] for row in io.get_step_state_outputs("wf", "produce")}
    assert rows == {"saved_id": "number"}


def _tabular_step(step_id, operation, target):
    return {
        "step_id": step_id,
        "step_kind": "tabular_feed",
        "override": {"tabular_feed": {"operation": operation, "result_target": target}},
    }


def _tabular_resolver(monkeypatch, workflow):
    io = IOResolver(SimpleNamespace(get_workflow=lambda *_args, **_kwargs: workflow))
    # These steps have no agent/skill contract or previously executed job output.
    monkeypatch.setattr(io, "_resolve_published_block_contract", lambda **_kwargs: None)
    monkeypatch.setattr(io, "_merge_step_output_contract", lambda **_kwargs: [])
    return io


@pytest.mark.parametrize(
    ("operation", "members"),
    [
        ("checksum", {"unchanged": "boolean", "committed_snapshot_artifact_id": "artifact_handle"}),
        ("read", {"dataset_handle": "dataset_handle"}),
        ("create_dataset", {"dataset_handle": "dataset_handle"}),
        ("transform", {"dataset_handle": "dataset_handle"}),
        ("concat", {"dataset_handle": "dataset_handle"}),
        ("merge_rows", {"dataset_handle": "dataset_handle"}),
        (
            "diff",
            {"diff_dataset_handle": "dataset_handle", "snapshot_artifact_id": "artifact_handle"},
        ),
        ("read_rows", {"rows": "array"}),
        ("write", {"artifact_handle": "artifact_handle", "sha256": "string"}),
    ],
)
def test_tabular_members_keep_parent_and_actual_operation_types(monkeypatch, operation, members):
    step = _tabular_step("produce", operation, "state.feed.result")
    step["group_id"] = "group-feed"
    io = _tabular_resolver(monkeypatch, {"steps": [step]})

    outputs = io.get_step_state_outputs("wf", "produce")

    assert {row["name"]: row["type"] for row in outputs} == {
        "feed": "object",
        "feed.result": "object",
        **{f"feed.result.{name}": kind for name, kind in members.items()},
    }
    assert all(row["step_id"] == "produce" for row in outputs)
    assert all(row["group_id"] == "group-feed" for row in outputs)
    assert all(row["source"] == "tabular_feed" for row in outputs)
    assert all(row["source_id"] == "produce" for row in outputs)
    assert all(row["required"] is False for row in outputs)
    assert io.get_step_outputs("wf", "produce") == outputs


def test_actual_tabular_graph_offers_source_checksum_baseline_and_export_members(monkeypatch):
    workflow = linear_workflow(
        _tabular_step("source", "create_dataset", "state.source"),
        _tabular_step("source_write", "write", "state.source_write"),
        _tabular_step("check", "checksum", "state.check"),
        _tabular_step("baseline", "read", "state.baseline"),
        _tabular_step("prelim", "diff", "state.prelim"),
        _tabular_step("changed_rows", "read_rows", "state.changed_rows"),
        _tabular_step("enriched", "merge_rows", "state.enriched"),
        _tabular_step("final", "diff", "state.final"),
        _tabular_step("export", "write", "state.export"),
        workflow_id="wf_feed",
    )
    workflow["steps"].append(_tabular_step("unconnected", "write", "state.unconnected"))
    io = _tabular_resolver(monkeypatch, workflow)

    before = io.get_available_variables("wf_feed", step_id="check")
    after = io.get_available_variables("wf_feed", step_id="check", position="after")
    final = io.get_available_variables("wf_feed", step_id="done")

    assert {row["name"]: row["type"] for row in before["prior_outputs"]} == {
        "source": "object",
        "source.dataset_handle": "dataset_handle",
        "source_write": "object",
        "source_write.artifact_handle": "artifact_handle",
        "source_write.sha256": "string",
    }
    assert {row["name"] for row in after["prior_outputs"]} == {
        "source",
        "source.dataset_handle",
        "source_write",
        "source_write.artifact_handle",
        "source_write.sha256",
        "check",
        "check.unchanged",
        "check.committed_snapshot_artifact_id",
    }
    downstream = {row["name"]: (row["type"], row["step_id"]) for row in final["prior_outputs"]}
    assert {
        name: downstream[name]
        for name in (
            "source_write.sha256",
            "check.unchanged",
            "check.committed_snapshot_artifact_id",
            "baseline.dataset_handle",
            "prelim.diff_dataset_handle",
            "prelim.snapshot_artifact_id",
            "changed_rows.rows",
            "enriched.dataset_handle",
            "final.diff_dataset_handle",
            "final.snapshot_artifact_id",
            "export.artifact_handle",
            "export.sha256",
        )
    } == {
        "source_write.sha256": ("string", "source_write"),
        "check.unchanged": ("boolean", "check"),
        "check.committed_snapshot_artifact_id": ("artifact_handle", "check"),
        "baseline.dataset_handle": ("dataset_handle", "baseline"),
        "prelim.diff_dataset_handle": ("dataset_handle", "prelim"),
        "prelim.snapshot_artifact_id": ("artifact_handle", "prelim"),
        "changed_rows.rows": ("array", "changed_rows"),
        "enriched.dataset_handle": ("dataset_handle", "enriched"),
        "final.diff_dataset_handle": ("dataset_handle", "final"),
        "final.snapshot_artifact_id": ("artifact_handle", "final"),
        "export.artifact_handle": ("artifact_handle", "export"),
        "export.sha256": ("string", "export"),
    }
    assert all(row["step_id"] != "unconnected" for row in final["prior_outputs"])
    assert all(row["group_id"] is None for row in final["prior_outputs"])


@pytest.mark.parametrize("operation", ["checksum", "diff", "write", "read", "commit_snapshot"])
def test_tabular_picker_does_not_offer_snapshot_bookkeeping_or_unknown_members(
    monkeypatch, operation
):
    io = _tabular_resolver(
        monkeypatch, {"steps": [_tabular_step("produce", operation, "state.feed")]}
    )

    names = {row["name"] for row in io.get_step_state_outputs("wf", "produce")}

    assert not names.intersection(
        {
            "feed.committed_snapshot_ref",
            "feed.revision",
            "feed.expected_snapshot_ref",
            "feed.previous_snapshot_artifact_id",
            "feed.object_key",
            "feed.summary",
            "feed.schema",
            "feed.preview_rows",
            "feed.snapshot_scope",
            "feed.artifact_object_key",
        }
    )
    if operation == "write":
        assert "feed.dataset_handle" not in names
    elif operation == "read":
        assert "feed.sha256" not in names


@pytest.mark.parametrize(
    "operation", ["not_an_operation", "", None, "inspect", "validate", "commit_snapshot"]
)
def test_tabular_incomplete_or_nonprojected_operation_keeps_only_parent(monkeypatch, operation):
    io = _tabular_resolver(
        monkeypatch, {"steps": [_tabular_step("produce", operation, "state.feed")]}
    )

    assert {row["name"]: row["type"] for row in io.get_step_state_outputs("wf", "produce")} == {
        "feed": "object",
    }


@pytest.mark.parametrize("target", [None, "", " "])
def test_tabular_missing_result_target_does_not_invent_child_paths(monkeypatch, target):
    io = _tabular_resolver(monkeypatch, {"steps": [_tabular_step("produce", "write", target)]})

    assert io.get_step_state_outputs("wf", "produce") == []


@pytest.mark.parametrize("reverse", [False, True])
def test_exact_writes_preserve_parent_collision_and_alias_occurrences(monkeypatch, reverse):
    assignments = [("outputs.text", "hello"), ("outputs", {}), ("state.outputs.text", "again")]
    if reverse:
        assignments.reverse()
    step = {
        "step_id": "produce",
        "step_kind": "set_variable",
        "override": {"set_variable": {"assignments": dict(assignments)}},
    }
    io = resolver(monkeypatch, step)
    writes = io.get_step_state_writes(step, workflow_account_id="account-a")
    assert [(row["name"], row["type"]) for row in writes] == [
        (target.removeprefix("state."), "object" if isinstance(value, dict) else "text")
        for target, value in assignments
    ]
    assert (
        len(
            [
                row
                for row in io.get_step_state_outputs("wf", "produce")
                if row["name"] == "outputs.text"
            ]
        )
        == 1
    )


def test_exact_writes_exclude_message_contract_and_tabular_member_suggestions(monkeypatch):
    step = _tabular_step("produce", "read_rows", "state.feed.result")
    io = resolver(monkeypatch, step)
    assert [row["name"] for row in io.get_step_state_writes(step)] == ["feed.result"]
    assert "result" in {row["name"] for row in io.get_step_declared_outputs(step)}
    step["override"] = {}
    assert io.get_step_state_writes(step) == []
