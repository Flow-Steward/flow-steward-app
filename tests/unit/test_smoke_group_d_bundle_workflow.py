"""The group D scan bundle ships a workflow Core accepts: steps and one graph."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from core.application.registry.workflow_definition_rows import _definition_payload
from core.application.workflows.workflow_definition_changes import definition_from_workflow
from core.application.workflows.workflow_definition_validation import (
    validate_workflow_definition,
)
from core.application.workflows.workflow_step_config_contracts import validate_step_config_keys

REPO_ROOT = Path(__file__).resolve().parents[2]
GROUP_D_COMMON = REPO_ROOT / "scripts" / "smoke" / "groupD" / "smoke_common.sh"


def _bundle_workflow(tmp_path: Path) -> dict:
    bundle = tmp_path / "bundle"
    env = {key: value for key, value in os.environ.items() if not key.startswith("FS_SMOKE_")}
    env.pop("EXT_ROOT", None)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{GROUP_D_COMMON}"; mkbundle "{bundle}" acme.smoke-probe smoke_probe yes',
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    path = bundle / "artifacts" / "workflows" / "smoke_probe_workflow.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_group_d_bundle_workflow_is_steps_and_graph_not_phases(tmp_path: Path) -> None:
    workflow = _bundle_workflow(tmp_path)

    assert "phases" not in workflow
    assert [step["step_id"] for step in workflow["steps"]] == [
        "smoke_probe_http",
        "smoke_probe_done",
    ]
    rows = _definition_payload(workflow)
    assert rows is not None
    assert [step["step_kind"] for step in rows.steps] == ["http_request", "stop_success"]
    assert validate_workflow_definition(definition_from_workflow(workflow)) == []


def test_group_d_bundle_workflow_steps_carry_only_runtime_config_keys(tmp_path: Path) -> None:
    workflow = _bundle_workflow(tmp_path)

    issues = [
        issue
        for index, step in enumerate(workflow["steps"])
        for issue in validate_step_config_keys(
            step_kind=step["step_kind"], override=step.get("override"), step_path=f"steps[{index}]"
        )
    ]
    assert issues == []
    http = workflow["steps"][0]["override"]["http_request"]
    assert http["response_target"] == "state.weather_raw"
    assert workflow["settings"]["callable"]["expected_outputs"][0]["name"] == "weather_raw"
