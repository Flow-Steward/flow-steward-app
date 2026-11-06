"""``update_interface``: the AI Builder sets ``report_output`` in one change set.

ADR 0059 recommendation 2: one canonical, atomic Interface operation in
``workflow_apply_changes``, judged with the rest of the result and written in
the same revision; a broken Interface is never stored.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from core.application.workflow_documents.report_examples import TEMPLATE_REPORT_EXAMPLE
from core.application.workflows.authoring.schema import WorkflowAuthoringSchema
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    apply_workflow_changes,
)
from core.application.workflows.workflow_edit_session_contract import authoring_content
from tests.unit.test_workflow_definition_service import _Store, edge, ref, step, workflow

REPORT_OUTPUTS = [{"name": "sales_report", "type": "object", "required": True}]


def _report_workflow(*, enabled: bool, callable_obj: dict[str, Any] | None = None) -> dict:
    wf = workflow(
        enabled=enabled,
        steps=[
            step(
                "build", "template", override={"template": copy.deepcopy(TEMPLATE_REPORT_EXAMPLE)}
            ),
            step("end", "stop_success"),
        ],
        graph={"schema_version": 1, "entry": ref("build"), "edges": [edge("build", ref("end"))]},
    )
    if callable_obj is not None:
        wf["settings"]["callable"] = callable_obj
    return wf


def _apply(store: _Store, operations: list[dict[str, Any]], **kwargs: Any) -> Any:
    return apply_workflow_changes(
        store, "wf", expected_revision=3, operations=operations, author="t", **kwargs
    )


def test_interface_and_report_selection_are_written_in_one_revision() -> None:
    store = _Store(_report_workflow(enabled=False))
    result = _apply(
        store,
        [
            {
                "op": "update_interface",
                "patch": {
                    "enabled": True,
                    "trigger_channels": ["api", "mcp"],
                    "expected_outputs": REPORT_OUTPUTS,
                    "report_output": "sales_report",
                },
            }
        ],
    )
    assert result.revision == 4
    assert len(store.saves) == 1
    saved = store.saves[0]["settings"]
    assert saved["callable"]["report_output"] == "sales_report"
    assert saved["execution_graph"]["entry"] == ref("build")


@pytest.mark.parametrize("enabled", [False, True])
def test_a_broken_selection_is_refused_even_on_a_disabled_workflow(enabled: bool) -> None:
    store = _Store(_report_workflow(enabled=enabled))
    with pytest.raises(WorkflowDefinitionChangeError) as caught:
        _apply(
            store,
            [
                {
                    "op": "update_interface",
                    "patch": {
                        "enabled": True,
                        "trigger_channels": ["api"],
                        "expected_outputs": [
                            {"name": "sales_report", "type": "string", "required": True}
                        ],
                        "report_output": "sales_report",
                    },
                }
            ],
        )
    assert "workflow.interface.report_output_not_object" in {
        issue["code"] for issue in caught.value.issues
    }
    assert store.saves == []


def test_a_dry_run_reports_interface_issues_without_writing() -> None:
    store = _Store(_report_workflow(enabled=False))
    result = _apply(
        store,
        [{"op": "update_interface", "patch": {"enabled": True, "report_output": "missing"}}],
        dry_run=True,
    )
    assert "workflow.interface.report_output_unknown" in {
        issue["code"] for issue in result.blocking_issues
    }
    assert store.saves == []


def test_null_removes_a_key_and_other_keys_are_kept() -> None:
    stored = {
        "enabled": True,
        "trigger_channels": ["api"],
        "expected_outputs": REPORT_OUTPUTS,
        "report_output": "sales_report",
    }
    store = _Store(_report_workflow(enabled=False, callable_obj=stored))
    _apply(store, [{"op": "update_interface", "patch": {"report_output": None}}])
    saved = store.saves[0]["settings"]["callable"]
    assert "report_output" not in saved
    assert saved["expected_outputs"] == REPORT_OUTPUTS


def test_unknown_interface_fields_are_refused() -> None:
    store = _Store(_report_workflow(enabled=False))
    with pytest.raises(WorkflowDefinitionChangeError) as caught:
        _apply(store, [{"op": "update_interface", "patch": {"execution_graph": {}}}])
    assert caught.value.issues[0]["code"] == "workflow.change.field_unknown"


SAMPLE = {"artifact_id": "art-1", "filename": "sample.csv", "size_bytes": 10}
STORED_INPUTS = [
    {"name": "api", "type": "secret", "required": True, "default_secret_ref": "ref-api"},
    {"name": "upload", "type": "file", "required": False, "sample_file": SAMPLE},
    {"name": "region", "type": "string", "required": False},
]
PUBLIC_INPUTS = [
    {"name": "api", "type": "secret", "required": True, "default_configured": True},
    {"name": "upload", "type": "file", "required": False},
    {"name": "region", "type": "string", "required": True},
]


def _authority_store(*, enabled: bool = False) -> _Store:
    stored = {"enabled": True, "trigger_channels": ["api"], "expected_inputs": STORED_INPUTS}
    return _Store(_report_workflow(enabled=enabled, callable_obj=copy.deepcopy(stored)))


def _inputs_patch(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"op": "update_interface", "patch": {"expected_inputs": rows}}]


def _saved_inputs(store: _Store) -> dict[str, dict[str, Any]]:
    rows = store.saves[-1]["settings"]["callable"]["expected_inputs"]
    return {row["name"]: row for row in rows}


@pytest.mark.parametrize("enabled", [False, True])
def test_a_plaintext_secret_default_is_refused_without_a_write(enabled: bool) -> None:
    store = _Store(_report_workflow(enabled=False))
    with pytest.raises(WorkflowDefinitionChangeError) as caught:
        _apply(
            store,
            [
                {
                    "op": "update_interface",
                    "patch": {
                        "enabled": enabled,
                        "expected_inputs": [
                            {"name": "api", "type": "secret", "default": "plaintext"}
                        ],
                    },
                }
            ],
        )
    assert caught.value.issues[0]["code"] == "workflow.interface.secret_default_plaintext"
    assert store.saves == []
    assert store.workflows["wf"]["revision"] == 3
    assert "plaintext" not in repr(store.workflows["wf"])


def test_a_public_round_trip_keeps_secret_references_and_test_files() -> None:
    store = _authority_store()
    _apply(store, _inputs_patch(copy.deepcopy(PUBLIC_INPUTS)))
    saved = _saved_inputs(store)
    assert saved["api"]["default_secret_ref"] == "ref-api"
    assert saved["upload"]["sample_file"] == SAMPLE
    assert saved["region"]["required"] is True
    assert "default_configured" not in saved["api"]


@pytest.mark.parametrize(
    ("row", "field"),
    [
        (
            {"name": "api", "type": "secret", "default_secret_ref": "ref-other"},
            "default_secret_ref",
        ),
        (
            {"name": "upload", "type": "file", "sample_file": {"artifact_id": "art-2"}},
            "sample_file",
        ),
        (
            {"name": "fresh", "type": "secret", "default_secret_ref": "ref-api"},
            "default_secret_ref",
        ),
    ],
)
def test_supplied_input_authority_is_refused(row: dict[str, Any], field: str) -> None:
    store = _authority_store()
    with pytest.raises(WorkflowDefinitionChangeError) as caught:
        _apply(store, _inputs_patch([row]))
    issue = caught.value.issues[0]
    assert issue["code"] == "workflow.change.input_authority_forbidden"
    assert issue["path"].endswith(f".{field}")
    assert store.saves == []


def test_echoing_the_stored_authority_is_accepted() -> None:
    store = _authority_store()
    _apply(store, _inputs_patch(copy.deepcopy(STORED_INPUTS)))
    assert _saved_inputs(store)["api"]["default_secret_ref"] == "ref-api"


@pytest.mark.parametrize(
    ("rows", "absent"),
    [
        ([{"name": "api", "type": "string"}], "api"),
        ([{"name": "api_key", "type": "secret"}], "api_key"),
        ([{"name": "region", "type": "string"}], "api"),
    ],
    ids=["type_change", "rename", "delete"],
)
def test_authority_follows_only_the_same_field_and_type(rows: list, absent: str) -> None:
    store = _authority_store()
    _apply(store, _inputs_patch(rows))
    saved = _saved_inputs(store)
    assert "default_secret_ref" not in saved.get(absent, {})
    assert all(row.get("default_secret_ref") != "ref-api" for row in saved.values())


def test_a_refused_interface_change_refuses_the_whole_set() -> None:
    store = _authority_store()
    with pytest.raises(WorkflowDefinitionChangeError):
        _apply(
            store,
            [
                {"op": "create_step", "step": step("extra", "transform")},
                *_inputs_patch([{"name": "api", "type": "secret", "default_secret_ref": "x"}]),
            ],
        )
    assert store.saves == []
    assert store.workflows["wf"]["revision"] == 3


def test_other_change_sets_leave_the_interface_untouched() -> None:
    stored = {"enabled": True, "trigger_channels": ["api"], "expected_outputs": REPORT_OUTPUTS}
    store = _Store(_report_workflow(enabled=False, callable_obj=stored))
    _apply(store, [{"op": "create_step", "step": step("extra", "transform")}])
    assert store.saves[0]["settings"]["callable"] == stored


def test_the_operation_is_published_to_agents() -> None:
    guidance = WorkflowAuthoringSchema().get()["definition_guidance"]["change_set"]
    assert "update_interface" in guidance["operation_names"]


def test_an_edit_session_carries_the_report_selection() -> None:
    stored = {
        "enabled": True,
        "trigger_channels": ["api"],
        "expected_outputs": REPORT_OUTPUTS,
        "report_output": "sales_report",
    }
    content = authoring_content(_report_workflow(enabled=False, callable_obj=stored))
    assert content["settings"]["callable"]["report_output"] == "sales_report"
