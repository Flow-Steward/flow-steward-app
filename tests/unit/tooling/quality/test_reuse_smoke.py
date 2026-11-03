from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from core.application.tooling.quality.reuse_smoke import CHECK_ID, run_smoke, smoke_definition

ROOT = Path(__file__).resolve().parents[4]


class ObservedRegression:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, check, *, timeout_seconds: int) -> int:
        del timeout_seconds
        assert check.id == CHECK_ID
        self.calls += 1
        return 0


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _repo(root: Path) -> tuple[Path, str]:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Quality smoke test")
    _git(root, "config", "user.email", "quality@example.invalid")
    (root / ".gitignore").write_text("logs/\n")
    (root / "assets").mkdir()
    (root / "assets/source.js").write_text("const value = 1;\n")
    registry = root / "infra/tooling/quality/check-registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checks": [
                    {
                        "id": "frontend-tests",
                        "owner_tier": 3,
                        "command": ["never-executed-frontend-command"],
                        "inputs": ["assets/**"],
                        "reuse_scope": "inputs",
                    }
                ],
            }
        )
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Producer")
    return root, _git(root, "rev-parse", "HEAD")


def _consumer_commit(root: Path) -> str:
    control = root / ".github/quality-reuse-smoke-source.json"
    control.parent.mkdir(exist_ok=True)
    control.write_text('{"source_run_id":"123"}\n')
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Consumer source selector")
    return _git(root, "rev-parse", "HEAD")


def test_smoke_runs_only_one_real_canonical_regression_with_a_separate_identity() -> None:
    check = smoke_definition()
    assert check.id == "quality-reuse-smoke"
    assert check.owner_tier == 1
    assert check.reuse_scope == "inputs"
    assert check.command[:2] == ("env", "PYTEST_ADDOPTS=--noconftest")
    assert "core/entrypoints/cli/scripts/lint.sh" in check.command
    assert check.command[check.command.index("--checks") + 1] == "targeted-tests"
    selected = check.command[check.command.index("--test-paths") + 1]
    assert selected.endswith("::test_input_scope_never_overrides_a_fingerprint_mismatch")
    assert "--full" not in check.command and "--fast" not in check.command
    assert "tests/unit/tooling/quality/test_evidence.py" in check.inputs
    assert "core/application/tooling/quality/**" in check.inputs


def test_smoke_consumer_reuses_old_commit_record_without_running_a_product_suite(
    tmp_path: Path,
) -> None:
    root, producer_sha = _repo(tmp_path / "repo")
    regression = ObservedRegression()
    producer = run_smoke(root, commit_sha=producer_sha, run_id="123", runner=regression)
    assert producer["status"] == "passed"
    assert producer["runner_calls"] == 1
    previous = tmp_path / "producer.json"
    previous.write_text(json.dumps(producer))
    consumer_sha = _consumer_commit(root)
    consumer = run_smoke(
        root,
        commit_sha=consumer_sha,
        run_id="456",
        source_sha=producer_sha,
        source_run_id="123",
        previous_report=previous,
        runner=regression,
    )
    assert consumer_sha != producer_sha
    assert consumer["status"] == "passed"
    assert consumer["verification"]["reused"] == (CHECK_ID,)
    assert consumer["verification"]["executed"] == ()
    assert consumer["runner_calls"] == 0
    assert regression.calls == 1
    assert consumer["evidence_commit_sha"] == producer_sha
    assert consumer["source_sha"] == producer_sha
    assert consumer["frontend_fingerprint"] == producer["frontend_fingerprint"]
    assert consumer["frontend_assets_digest"] == producer["frontend_assets_digest"]
    assert consumer["frontend_suite_executed"] is False
    assert consumer["full_gate"] is False
    assert consumer["official_suite_pass_claimed"] is False
    assert {item.name for item in (root / "logs/quality/evidence").iterdir()} == {CHECK_ID}


@pytest.mark.parametrize("fault", ["candidate_sha", "run_id", "frontend_fingerprint"])
def test_smoke_refuses_mismatched_source_report_before_running_any_regression(
    tmp_path: Path, fault: str
) -> None:
    root, producer_sha = _repo(tmp_path / "repo")
    regression = ObservedRegression()
    producer = run_smoke(root, commit_sha=producer_sha, run_id="123", runner=regression)
    producer[fault] = "invalid-source"
    previous = tmp_path / "producer.json"
    previous.write_text(json.dumps(producer))
    consumer_sha = _consumer_commit(root)
    with pytest.raises(ValueError, match=r"source|fingerprint"):
        run_smoke(
            root,
            commit_sha=consumer_sha,
            run_id="456",
            source_sha=producer_sha,
            source_run_id="123",
            previous_report=previous,
            runner=regression,
        )
    assert regression.calls == 1


def test_smoke_consumer_does_not_execute_a_cache_miss_as_an_unrequested_check(
    tmp_path: Path,
) -> None:
    root, producer_sha = _repo(tmp_path / "repo")
    regression = ObservedRegression()
    producer = run_smoke(root, commit_sha=producer_sha, run_id="123", runner=regression)
    previous = tmp_path / "producer.json"
    previous.write_text(json.dumps(producer))
    test = root / "tests/unit/tooling/quality/test_evidence.py"
    test.parent.mkdir(parents=True)
    test.write_text("# Changed regression invalidates the smoke fingerprint.\n")
    consumer_sha = _consumer_commit(root)
    consumer = run_smoke(
        root,
        commit_sha=consumer_sha,
        run_id="456",
        source_sha=producer_sha,
        source_run_id="123",
        previous_report=previous,
        runner=regression,
    )
    assert consumer["status"] == "failed"
    assert regression.calls == 1


def test_smoke_workflow_skips_all_full_tiers_and_imports_the_real_source_artifact() -> None:
    source_root = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", str(ROOT)))
    workflow = yaml.safe_load((source_root / ".github/workflows/remote-quality.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    assert "reuse-smoke" in events["workflow_dispatch"]["inputs"]["mode"]["options"]
    assert "github.ref_name == 'codex/quality-reuse-live'" in workflow["env"]["QUALITY_MODE"]
    steps = workflow["jobs"]["quality"]["steps"]
    tier_two = next(step for step in steps if step.get("name") == "Run canonical Tier 2")
    assert tier_two["if"] == "env.QUALITY_MODE != 'reuse-smoke'"
    for step in steps:
        if "lint.sh --full" in step.get("run", "") or "PostgreSQL image" in step.get("name", ""):
            assert (
                step["if"] == "env.QUALITY_MODE == 'release' || env.QUALITY_MODE == 'release-sonar'"
            )
    for job_id in ("parallel_tier_three", "sonar"):
        condition = workflow["jobs"][job_id]["if"]
        assert "github.ref_name != 'codex/quality-reuse-live'" in condition
        assert "inputs.mode != 'reuse-smoke'" in condition
    smoke = next(step for step in steps if "reuse_smoke" in step.get("run", ""))
    assert smoke["if"] == "env.QUALITY_MODE == 'reuse-smoke'"
    assert "fs_compose run --no-deps --rm -T" in smoke["run"]
    assert "source-quality/quality/hosted/reuse-smoke.json" in smoke["run"]
    assert "--fast" not in smoke["run"] and "--full" not in smoke["run"]
    validator = next(step for step in steps if step.get("id") == "validate-reuse-source")
    assert "steps.smoke-source.outputs.run_id != ''" in validator["if"]
    assert "remote-quality.yml" in validator["with"]["script"]
