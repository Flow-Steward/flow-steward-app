"""A workflow's file outputs come from its steps' "Return from workflow" settings."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.registry.workflow_contract import interface_from_definition
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.registry.workflow_returned_files import (
    captured_returned_files,
    derive_returned_files,
    step_file_results,
)


def _save_file(
    step_id: str,
    target: str,
    *,
    returned: list[dict[str, Any]] | None = None,
    name: str = "",
) -> dict[str, Any]:
    override: dict[str, Any] = {
        "file_from_base64": {"source": "state.inputs.body", "result_target": target}
    }
    if returned is not None:
        override["returned_files"] = returned
    return {
        "step_id": step_id,
        "display_name": name or step_id,
        "step_kind": "file_from_base64",
        "override": override,
    }


def _workflow(*steps: dict[str, Any], **callable_fields: Any) -> dict[str, Any]:
    return {
        "workflow_id": "wf_child",
        "settings": {
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                **callable_fields,
            }
        },
        "steps": list(steps),
    }


def test_a_ticked_file_result_appears_in_outputs_with_its_source():
    interface = interface_from_definition(
        _workflow(
            _save_file(
                "render",
                "state.invoice_pdf",
                returned=[{"result": "file", "key": "invoice_pdf"}],
                name="Render invoice",
            )
        )
    )

    assert [row.dump() for row in interface.returned_files] == [
        {
            "name": "invoice_pdf",
            "type": "file",
            "required": False,
            "source": {
                "step_id": "render",
                "step_name": "Render invoice",
                "result": "file",
                "target": "state.invoice_pdf",
            },
        }
    ]
    assert interface.output_mode == "artifacts"
    assert interface.definition_issues() == []
    schema = WorkflowInterfaceService().output_json_schema(interface)
    assert schema["properties"]["invoice_pdf"]["x-fs-type"] == "file"
    assert "invoice_pdf" not in schema.get("required", [])


def test_an_unticked_result_is_not_in_the_contract():
    interface = interface_from_definition(
        _workflow(_save_file("render", "state.invoice_pdf", returned=[]))
    )

    assert interface.returned_files == []
    assert interface.output_mode == "none"


@pytest.mark.parametrize(
    ("returned_files", "expected_path"),
    [
        ({"result": "file", "key": "invoice_pdf"}, "workflow.steps.render.override.returned_files"),
        ([None], "workflow.steps.render.override.returned_files[0]"),
        ([{}], "workflow.steps.render.override.returned_files[0].result"),
    ],
)
def test_malformed_returned_file_declarations_are_setup_issues(
    returned_files: Any, expected_path: str
) -> None:
    returned, issues = derive_returned_files(
        [_save_file("render", "state.invoice_pdf", returned=returned_files)]
    )

    assert returned == []
    assert len(issues) == 1
    assert issues[0]["code"].startswith("workflow.interface.returned_file")
    assert issues[0]["path"] == expected_path


def test_the_key_survives_a_step_rename_and_a_new_variable():
    before = interface_from_definition(
        _workflow(
            _save_file(
                "render",
                "state.invoice_pdf",
                returned=[{"result": "file", "key": "invoice_pdf"}],
                name="Render invoice",
            )
        )
    )
    after = interface_from_definition(
        _workflow(
            _save_file(
                "render",
                "state.invoice_v2",
                returned=[{"result": "file", "key": "invoice_pdf"}],
                name="Render the customer invoice",
            )
        )
    )

    assert before.returned_files[0].name == after.returned_files[0].name == "invoice_pdf"
    assert after.returned_files[0].source.step_name == "Render the customer invoice"
    assert after.returned_files[0].source.target == "state.invoice_v2"


def test_a_tick_without_a_key_gets_the_variable_name():
    returned, issues = derive_returned_files(
        [_save_file("render", "state.files_out.report-2026", returned=[{"result": "file"}])]
    )

    assert issues == []
    assert returned[0].key == "report_2026"


def test_a_tick_on_a_result_the_step_no_longer_saves_is_a_setup_issue():
    step = {
        "step_id": "download",
        "step_kind": "http_request",
        "override": {
            "http_request": {"response_mode": "inline", "response_target": "state.page"},
            "returned_files": [{"result": "response", "key": "page"}],
        },
    }

    interface = interface_from_definition(_workflow(step))

    assert interface.returned_files == []
    assert [issue["code"] for issue in interface.definition_issues()] == [
        "workflow.interface.returned_file_source_missing"
    ]


def test_two_steps_cannot_return_files_under_one_key():
    interface = interface_from_definition(
        _workflow(
            _save_file("a", "state.a", returned=[{"result": "file", "key": "report"}]),
            _save_file("b", "state.b", returned=[{"result": "file", "key": "report"}]),
        )
    )

    assert [row.source.step_id for row in interface.returned_files] == ["a"]
    assert [issue["code"] for issue in interface.definition_issues()] == [
        "workflow.interface.duplicate_name"
    ]


def test_a_returned_file_may_not_share_a_variable_output_name():
    interface = interface_from_definition(
        _workflow(
            _save_file("a", "state.a", returned=[{"result": "file", "key": "summary"}]),
            expected_outputs=[{"name": "summary", "type": "string"}],
        )
    )

    assert interface.returned_files == []
    assert interface.output_mode == "variables"
    assert "workflow.interface.duplicate_name" in [
        issue["code"] for issue in interface.definition_issues()
    ]


def test_file_results_of_each_producing_step_kind():
    steps = [
        _save_file("save", "state.saved"),
        {
            "step_id": "fetch",
            "step_kind": "http_request",
            "override": {
                "http_request": {"response_mode": "artifact", "response_target": "state.download"}
            },
        },
        {
            "step_id": "child",
            "step_kind": "invoke_workflow",
            "override": {
                "invoke_workflow": {
                    "artifact_mapping": {"report": "state.child_report", "log": "log_note"}
                }
            },
        },
        {
            "step_id": "export",
            "step_kind": "connector",
            "output_mapping_jsonb": {"attachments": "state.attachments"},
        },
    ]

    results = [(row.step_id, row.result, row.target) for s in steps for row in step_file_results(s)]

    assert results == [
        ("save", "file", "state.saved"),
        ("fetch", "response", "state.download"),
        ("child", "report", "state.child_report"),
        ("export", "attachments", "state.attachments"),
    ]


def test_a_legacy_declaration_links_to_the_one_result_it_can_mean():
    interface = interface_from_definition(
        _workflow(
            _save_file("render", "state.report"),
            _save_file("draft", "state.draft"),
            expected_artifacts=[{"name": "report", "type": "file", "required": True}],
        )
    )

    assert [row.dump() for row in interface.returned_files] == [
        {
            "name": "report",
            "type": "file",
            "required": True,
            "source": {
                "step_id": "render",
                "step_name": "render",
                "result": "file",
                "target": "state.report",
            },
            "origin": "legacy",
        }
    ]
    assert interface.definition_issues() == []


def test_an_ambiguous_legacy_declaration_is_a_setup_issue_not_a_guess():
    interface = interface_from_definition(
        _workflow(
            _save_file("first", "state.report"),
            _save_file("second", "state.report"),
            expected_artifacts=[{"name": "report", "type": "file"}],
        )
    )

    assert interface.returned_files == []
    issues = interface.definition_issues()
    assert [(issue["code"], issue["severity"]) for issue in issues] == [
        ("workflow.interface.legacy_artifact_ambiguous", "error")
    ]
    assert "first, second" in issues[0]["message"]


def test_an_unmatched_legacy_declaration_is_reported():
    interface = interface_from_definition(
        _workflow(
            _save_file("render", "state.report"),
            expected_artifacts=[
                {"name": "summary_pdf", "type": "file", "required": True},
                {"name": "optional_log", "type": "file"},
            ],
        )
    )

    assert interface.returned_files == []
    assert [(issue["code"], issue["severity"]) for issue in interface.definition_issues()] == [
        ("workflow.interface.legacy_artifact_unlinked", "error"),
        ("workflow.interface.legacy_artifact_unlinked", "warning"),
    ]


def test_a_step_saved_with_the_setting_is_no_longer_linked_by_name():
    interface = interface_from_definition(
        _workflow(
            _save_file("render", "state.report", returned=[]),
            expected_artifacts=[{"name": "report", "type": "file"}],
        )
    )

    assert interface.returned_files == []
    assert [issue["code"] for issue in interface.definition_issues()] == [
        "workflow.interface.legacy_artifact_unlinked"
    ]


def test_external_changes_and_output_mode_are_no_longer_part_of_the_contract():
    interface = interface_from_definition(
        _workflow(
            output_mode="side_effect",
            side_effects=[{"kind": "external_update", "target": "CRM"}],
        )
    )

    dumped = interface.dump()
    assert "side_effects" not in dumped
    assert "expected_artifacts" not in dumped
    assert dumped["output_mode"] == "none"
    assert interface.definition_issues() == []


def test_a_capture_keeps_each_steps_file_under_its_own_key():
    returned, _ = derive_returned_files(
        [
            _save_file("first", "state.document", returned=[{"result": "file", "key": "report"}]),
            _save_file("second", "state.document", returned=[{"result": "file", "key": "receipt"}]),
        ]
    )
    state = {"artifacts": {}}

    after_first = captured_returned_files(
        state=state,
        update={"workflow_data": {"document": "artifact:aaa"}, "document": "artifact:aaa"},
        returned=[row for row in returned if row.step_id == "first"],
    )
    after_second = captured_returned_files(
        state={"artifacts": after_first},
        update={"workflow_data": {"document": "artifact:bbb"}, "document": "artifact:bbb"},
        returned=[row for row in returned if row.step_id == "second"],
    )

    assert after_second == {"report": "artifact:aaa", "receipt": "artifact:bbb"}


def test_a_capture_keeps_a_collection_whole_and_ignores_an_unwritten_result():
    returned, _ = derive_returned_files(
        [
            {
                "step_id": "export",
                "step_kind": "connector",
                "output_mapping_jsonb": {"attachments": "state.attachments"},
                "override": {"returned_files": [{"result": "attachments", "key": "attachments"}]},
            }
        ]
    )

    assert (
        captured_returned_files(state={}, update={"agent_messages": []}, returned=returned) is None
    )
    assert captured_returned_files(
        state={},
        update={"workflow_data": {"attachments": ["artifact:a", "artifact:b", "artifact:c"]}},
        returned=returned,
    ) == {"attachments": ["artifact:a", "artifact:b", "artifact:c"]}


def test_a_returned_file_is_read_only_from_the_captured_result():
    interface = interface_from_definition(
        _workflow(
            _save_file("render", "state.report", returned=[{"result": "file", "key": "report"}])
        )
    )
    state = {
        "workflow_data": {"report": "artifact:from_variable"},
        "agent_messages": [{"artifacts": {"report": "artifact:from_message"}}],
        "artifacts": {"report": "artifact:captured", "other": "artifact:stray"},
    }

    result = WorkflowInterfaceService().validate_outputs(interface, state)

    assert result.ok is True
    assert result.artifacts == {"report": "artifact:captured"}
