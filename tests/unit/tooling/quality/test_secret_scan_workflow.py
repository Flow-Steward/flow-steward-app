from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
MEASUREMENT_REF = "codex/secret-scan-live"


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/remote-quality.yml").read_text())


@pytest.mark.parametrize(
    ("ref", "event", "mode", "expected"),
    [
        (MEASUREMENT_REF, "push", "", ["secret_scan"]),
        (MEASUREMENT_REF, "workflow_dispatch", "tier2", ["secret_scan"]),
        (MEASUREMENT_REF, "workflow_dispatch", "release", ["secret_scan"]),
        (MEASUREMENT_REF, "workflow_dispatch", "release-sonar", ["secret_scan"]),
        (MEASUREMENT_REF, "workflow_dispatch", "parallel-diagnostic", ["secret_scan"]),
        ("feature/ordinary-change", "workflow_dispatch", "secret-scan-only", ["secret_scan"]),
        ("feature/ordinary-change", "push", "", ["quality"]),
        ("feature/ordinary-change", "workflow_dispatch", "tier2", ["quality"]),
    ],
)
def test_scanner_measurement_cannot_select_a_full_gate(
    ref: str, event: str, mode: str, expected: list[str]
) -> None:
    workflow = _workflow()
    conditions = {name: job["if"] for name, job in workflow["jobs"].items()}
    context = {
        "github": {
            "repository": "Flow-Steward/flow-steward-app",
            "ref_name": ref,
            "ref": f"refs/heads/{ref}",
            "event_name": event,
            "event": {"repository": {"private": False}, "head_commit": {"message": ""}},
        },
        "inputs": {"mode": mode, "reuse_run_id": ""},
        "needs": {"quality": {"result": "success", "outputs": {"evidence_imported": "false"}}},
    }
    script = r"""
const {conditions, context} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const selected = Object.entries(conditions).filter(([name, condition]) => {
  const evaluate = new Function('github', 'inputs', 'needs', 'always', 'cancelled',
    'contains', 'startsWith', `return (${condition});`);
  return evaluate(context.github, context.inputs, context.needs, () => true, () => false,
    (value, part) => value.includes(part), (value, prefix) => value.startsWith(prefix));
}).map(([name]) => name);
process.stdout.write(JSON.stringify(selected));
"""
    result = subprocess.check_output(
        ["node", "-e", script],
        input=json.dumps({"conditions": conditions, "context": context}),
        text=True,
    )
    assert json.loads(result) == expected


@pytest.mark.parametrize(
    ("message", "exit_code", "scanned", "total"),
    [
        ("[lint] detect-secrets: scanning 2 of 4760 files", 0, 2, 4760),
        (
            "[lint] detect-secrets: all 4760 files already passed with this content; nothing to rescan.",
            0,
            0,
            4760,
        ),
        ("[lint] detect-secrets: scanning 1 of 4760 files", 1, 1, 4760),
    ],
)
def test_scanner_measurement_reports_actual_counts_and_preserves_failure(
    tmp_path: Path, message: str, exit_code: int, scanned: int, total: int
) -> None:
    steps = _workflow()["jobs"]["secret_scan"]["steps"]
    step = next(
        step for step in steps if step.get("name") == "Measure only the canonical file detector"
    )
    body = step["run"].split("\n", 2)[2].rsplit("PYCODE", 1)[0]
    binary = tmp_path / "bash"
    binary.write_text(f"#!{sys.executable}\nprint({message!r})\nraise SystemExit({exit_code})\n")
    binary.chmod(0o755)
    (tmp_path / "logs/quality/hosted").mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "20",
        "SCANNER_SOURCE_RUN_ID": "10",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }
    result = subprocess.run(
        [sys.executable, "-c", body], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    report = json.loads((tmp_path / "logs/quality/hosted/secret-scan.json").read_text())
    assert report["scanned_files"] == scanned
    assert report["total_files"] == total
    assert report["reused_files"] == total - scanned
    assert report["command_seconds"] > 0
    assert report["exit_code"] == exit_code
    assert report["source_run_id"] == "10"
    assert report["release_evidence"] is False
    assert report["command"] == [
        "bash",
        "core/entrypoints/cli/scripts/lint.sh",
        "--checks",
        "detect-secrets",
        "--fingerprint-source",
        "commit",
        "--commit-sha",
        "a" * 40,
    ]
