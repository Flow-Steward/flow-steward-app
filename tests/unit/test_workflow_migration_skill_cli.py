"""The converter ships with the skill and runs without a platform checkout."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import yaml

from tests.unit.test_workflow_n8n_conversion import source
from tests.unit.workflow_migration_skill_helpers import SKILL_ROOT


def invoke(tmp_path, data, *options):
    scripts = tmp_path / "standalone"
    shutil.copytree(SKILL_ROOT / "scripts", scripts)
    input_file = tmp_path / "source.json"
    input_file.write_text(json.dumps(data))
    output = tmp_path / "candidate.yaml"
    report = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(scripts / "convert_n8n.py"),
            str(input_file),
            "--workflow-id",
            "imported_workflow",
            "--output",
            str(output),
            "--report",
            str(report),
            *options,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, output, report


def test_cli_translates_without_core_or_a_platform_checkout(tmp_path):
    result, output, report = invoke(tmp_path, source(), "--object-response-node", "1")
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(output.read_text())["workflows"][0]["enabled"] is False
    details = json.loads(report.read_text())
    assert details["status"] == "converted"
    assert details["target_validation"] == "required"
    assert details["runtime_verified"] is False
    assert "bundle_yaml" not in details


def test_cli_blocker_writes_report_without_a_partial_yaml(tmp_path):
    result, output, report = invoke(tmp_path, source())
    assert result.returncode == 1
    assert not output.exists()
    assert json.loads(report.read_text())["status"] == "blocked"


def test_cli_never_overwrites_an_existing_candidate(tmp_path):
    output = tmp_path / "candidate.yaml"
    output.write_text("existing draft\n")
    result, _, report = invoke(tmp_path, source(), "--object-response-node", "1")
    assert result.returncode == 2
    assert output.read_text() == "existing draft\n"
    assert not report.exists()
