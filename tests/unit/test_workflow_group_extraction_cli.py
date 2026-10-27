"""The public CLI delegates extraction with the reviewed revision and digest."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.application.management.command_specs.workflows import WorkflowConvertGroupCommandInput
from core.entrypoints.cli.platform.service_bindings.workflows import workflows_convert_group


def test_cli_preview_and_apply_preserve_arguments_and_admin_scope():
    calls = []
    runtime = SimpleNamespace(
        workflow_operator_service=SimpleNamespace(
            convert_group=lambda **kwargs: calls.append(kwargs) or {"can_convert": True}
        )
    )
    body = {
        "project_id": "p",
        "workflow_id": "wf",
        "group_id": "g",
        "child_workflow_id": "child",
        "display_name": "Child",
        "expected_revision": 3,
    }
    asyncio.run(workflows_convert_group(runtime, body))
    assert calls[-1]["dry_run"] is True
    assert calls[-1]["scope"].principal_type == "admin"
    asyncio.run(
        workflows_convert_group(runtime, {**body, "apply": True, "preview_digest": "digest"})
    )
    assert calls[-1]["dry_run"] is False
    assert calls[-1]["preview_digest"] == "digest"
    assert calls[-1]["expected_revision"] == 3


def test_cli_apply_requires_the_reviewed_preview():
    with pytest.raises(ValidationError, match="preview"):
        WorkflowConvertGroupCommandInput(
            project_id="p",
            workflow_id="wf",
            group_id="g",
            child_workflow_id="child",
            display_name="Child",
            expected_revision=3,
            apply=True,
        )


def test_public_cli_preview_and_apply_are_available_from_command_registry(monkeypatch):
    from click.testing import CliRunner

    from tests.unit.cli_platform.test_workflows_commands import _app

    app, operator = _app()
    calls = []

    def convert(**kwargs):
        calls.append(kwargs)
        return {
            "can_convert": True,
            "child_workflow_id": "child",
            "revision": 3,
            "preview_digest": "digest",
            "issues": [],
        }

    monkeypatch.setattr(operator, "convert_group", convert, raising=False)
    args = [
        "workflows",
        "convert-group",
        "wf",
        "--project-id",
        "p",
        "--group-id",
        "g",
        "--child-workflow-id",
        "child",
        "--display-name",
        "Child",
        "--expected-revision",
        "3",
        "--json",
        "--confirm",
    ]
    runner = CliRunner()
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert calls[-1]["dry_run"] is True
    result = runner.invoke(app, [*args, "--apply", "--preview-digest", "digest"])
    assert result.exit_code == 0, result.output
    assert calls[-1]["dry_run"] is False


def test_converter_default_readiness_works_in_a_fresh_cli_process():
    """Web imports must not be required to initialize the CLI's validator."""
    import subprocess
    import sys

    script = """
from core.application.workflows.workflow_group_extraction_service import GroupExtractionService
class Store:
    def get_workflow(self, workflow_id):
        workflow = {
            "workflow_id": workflow_id, "enabled": True,
            "settings": {"callable": {"enabled": True, "allow_empty_inputs": True,
                "trigger_channels": ["parent_workflow"], "output_mode": "none",
                "expected_inputs": [], "expected_outputs": []},
                "execution_graph": {"schema_version": 1, "entry": {"kind": "step", "id": "done"}, "edges": []}},
            "steps": [{"step_id": "done", "step_kind": "stop_success"}], "groups": [],
        }
        if workflow_id == "wf":
            workflow["steps"].insert(0, {"step_id": "invoke", "step_kind": "invoke_workflow", "override": {"invoke_workflow": {"workflow_id": "child"}}})
            workflow["settings"]["execution_graph"] = {"schema_version": 1,
                "entry": {"kind": "step", "id": "invoke"}, "edges": [{
                    "source": {"kind": "step", "id": "invoke"}, "outcome": {"name": "success"},
                    "target": {"kind": "step", "id": "done"}}]}
        return workflow
service = GroupExtractionService(Store(), account_id="a", reference_access=None,
    project_visible=lambda _: True, reactions=lambda *_: [])
result = service.validate(service.db, "wf")
assert isinstance(result["valid"], bool)
assert result["valid"], result["errors"]
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
