from __future__ import annotations

from typing import Any

import pytest

from core.application.registry.contract_validation import validate_workflow_readiness
from core.application.registry.io_resolver import IOResolver
from core.application.workflows.canvas_graph_service import build_workflow_canvas_graph
from tests._workflow_definitions import linear_workflow

_WRITE_SUMMARY = {
    "step_id": "write_summary",
    "step_kind": "set_variable",
    "override": {"set_variable": {"target": "task_summary", "value": "Owner-authored summary"}},
}


class _WorkflowDB:
    def __init__(self, *steps: dict[str, Any]) -> None:
        self.workflow: dict[str, Any] = linear_workflow(
            _WRITE_SUMMARY, *steps, workflow_id="wf_step_io_locale"
        )

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return self.workflow if workflow_id == self.workflow["workflow_id"] else None


def _finish_with_entry(source: str) -> dict[str, Any]:
    return {
        "step_id": "finish",
        "step_kind": "stop_success",
        "boundary_contract": {
            "entry": [{"name": "task_summary", "type": "text", "source": source}]
        },
    }


@pytest.mark.parametrize(
    ("source", "issue_type", "severity", "message_key", "message_params", "path"),
    [
        (
            "",
            "expected_input_ambiguous",
            "warning",
            "react.workflows.expected_input_ambiguous",
            {"input_name": "task_summary"},
            "boundary_contract.entry[0]",
        ),
        (
            "message.step.owner_step.artifacts.owner_output",
            "expected_input_source_unavailable",
            "error",
            "react.workflows.expected_input_source_unavailable",
            {
                "input_name": "task_summary",
                "source": "message.step.owner_step.artifacts.owner_output",
            },
            "boundary_contract.entry[0].source",
        ),
    ],
)
def test_step_io_diagnostic_metadata_reaches_readiness_and_canvas(
    source: str,
    issue_type: str,
    severity: str,
    message_key: str,
    message_params: dict[str, str],
    path: str,
) -> None:
    db = _WorkflowDB(_finish_with_entry(source))
    workflow_id = "wf_step_io_locale"

    produced = next(
        issue
        for issue in IOResolver(db).validate_step_io(workflow_id, "finish")
        if issue.get("issue_type") == issue_type
    )
    assert produced["message_key"] == message_key
    assert produced["message_params"] == message_params
    assert produced["severity"] == severity
    assert produced["path"] == path
    if source:
        assert produced["source"] == source
        assert produced["source_step_id"] == "owner_step"
        assert produced["source_name"] == "owner_output"

    validation = validate_workflow_readiness(db, workflow_id)
    bucket = "errors" if severity == "error" else "warnings"
    normalized = next(
        issue for issue in validation[bucket] if issue.get("issue_type") == issue_type
    )
    assert normalized["code"] == f"workflow.step_io.{severity}"
    assert normalized["path"] == f"workflow.steps.finish.{path}"
    assert normalized["step_id"] == "finish"
    assert normalized["input_name"] == "task_summary"
    assert normalized["message_key"] == message_key
    assert normalized["message_params"] == message_params
    assert normalized["message"] == produced["message"]

    graph = build_workflow_canvas_graph(db, workflow_id)
    canvas_issue = next(
        issue
        for issue in graph["validation_issues"]
        if issue.get("step_id") == "finish" and issue.get("code") == f"workflow.step_io.{severity}"
    )
    assert canvas_issue["message_key"] == message_key
    assert canvas_issue["message_params"] == message_params
    assert canvas_issue["title_key"] == message_key
    assert canvas_issue["title_params"] == message_params
    assert canvas_issue["message"] == produced["message"]
    assert canvas_issue["issues"][0]["message_key"] == message_key
    assert canvas_issue["issues"][0]["message_params"] == message_params


def test_transform_source_diagnostic_metadata_reaches_readiness_and_canvas() -> None:
    source = "state.inputs.owner_payload"
    db = _WorkflowDB(
        {
            "step_id": "owner_transform",
            "step_kind": "transform",
            "override": {
                "transform": {
                    "input": {"source": source},
                    "result_target": "state.owner_result",
                    "fields": [{"from": "name", "to": "name", "type": "text"}],
                }
            },
        }
    )
    workflow_id = db.workflow["workflow_id"]
    produced = next(
        issue
        for issue in IOResolver(db).validate_step_io(workflow_id, "owner_transform")
        if issue.get("issue_type") == "transform_input_source_unavailable"
    )
    assert produced["message_key"] == "react.workflows.transform_input_source_unavailable"
    assert produced["message_params"] == {"source": source}
    assert produced["source"] == source
    assert produced["severity"] == "error"

    validation = validate_workflow_readiness(db, workflow_id)
    normalized = next(
        issue
        for issue in validation["errors"]
        if issue.get("issue_type") == "transform_input_source_unavailable"
    )
    assert normalized["path"] == "workflow.steps.owner_transform.override.transform.input.source"
    assert normalized["message_key"] == produced["message_key"]
    assert normalized["message_params"] == {"source": source}
    assert normalized["message"] == produced["message"]

    graph = build_workflow_canvas_graph(db, workflow_id)
    canvas_issue = next(
        issue
        for issue in graph["validation_issues"]
        if issue.get("message_key") == produced["message_key"]
    )
    assert canvas_issue["message_params"] == {"source": source}
    assert canvas_issue["title_key"] == produced["message_key"]
    assert canvas_issue["title_params"] == {"source": source}
    assert canvas_issue["issues"][0]["message_params"] == {"source": source}


@pytest.mark.parametrize("locale", ["en", "lt"])
@pytest.mark.parametrize("source", ["", "message.step.owner_step.artifacts.owner_output"])
def test_step_io_localized_diagnostic_keeps_step_reference_grammar(
    locale: str, source: str
) -> None:
    from core.application.settings.ui_i18n import reset_ui_i18n_cache, translate_ui_text

    reset_ui_i18n_cache()
    db = _WorkflowDB(_finish_with_entry(source))
    issue_type = "expected_input_source_unavailable" if source else "expected_input_ambiguous"
    produced = next(
        issue
        for issue in IOResolver(db).validate_step_io(db.workflow["workflow_id"], "finish")
        if issue.get("issue_type") == issue_type
    )
    message = translate_ui_text(
        produced["message_key"], locale=locale, params=produced["message_params"]
    )
    assert "task_summary" in message
    assert "upstream step" in message if locale == "en" else "ankstesnių žingsnių" in message
    assert "phase." not in message
    assert "prior phase" not in message
    assert "fazių" not in message
    if source:
        assert source in message
    else:
        assert "message.step.<step_id>.artifacts.task_summary" in message


@pytest.mark.parametrize(
    "locale, expected",
    [
        ("en", "Open this step or group and align its input/output contract."),
        ("lt", "Atverkite šį žingsnį arba grupę ir suderinkite jo įvesties bei išvesties sutartį."),
    ],
)
def test_step_io_suggested_action_uses_current_scope_terms(locale: str, expected: str) -> None:
    from core.application.settings.ui_i18n import reset_ui_i18n_cache, translate_ui_text
    from core.application.workflows.canvas_graph_summaries import (
        _suggested_action_metadata_for_issue,
    )

    reset_ui_i18n_cache()
    key, default = _suggested_action_metadata_for_issue(
        "step", "expected_input_source_unavailable", ""
    )
    assert translate_ui_text(key, locale=locale, default=default) == expected
