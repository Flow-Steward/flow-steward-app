from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
HELPER = ROOT / "core/application/tooling/quality/hosted_cache_source.cjs"
SHA = "a" * 40


def _run_source(run_id: int = 10, branch: str = "feature") -> dict:
    return {
        "id": run_id,
        "workflow_id": 44,
        "repository": {"id": 99},
        "head_repository": {"id": 99},
        "head_sha": SHA,
        "head_branch": branch,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 2,
    }


def _artifact(run_id: int = 10, branch: str = "feature", artifact_id: int = 100) -> dict:
    return {
        "id": artifact_id,
        "name": f"secret-scan-files-{SHA}-2",
        "expired": False,
        "created_at": "2026-11-03T10:00:00Z",
        "workflow_run": {"id": run_id, "head_branch": branch, "head_sha": SHA},
    }


def _select(artifacts: list[dict], runs: list[dict], *, run_artifacts=None) -> dict:
    assert HELPER.is_file(), "Hosted scanner-cache source selector is missing"
    payload = {
        "artifacts": artifacts,
        "runs": {str(run["id"]): run for run in runs},
        "run_artifacts": run_artifacts,
    }
    script = r"""
const select = require(process.argv[1]);
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const calls = [];
const github = {rest: {actions: {
  async listArtifactsForRepo(args) {
    calls.push(['list', args]);
    return {data: {artifacts: data.artifacts, total_count: data.artifacts.length}};
  },
  async getWorkflowRun(args) {
    calls.push(['run', args.run_id]);
    const run = data.runs[String(args.run_id)];
    if (!run) throw new Error('missing run');
    return {data: run};
  },
  async listWorkflowRunArtifacts(args) {
    calls.push(['artifacts', args.run_id]);
    const artifacts = data.run_artifacts === null
      ? data.artifacts.filter(a => a?.workflow_run?.id === args.run_id)
      : (data.run_artifacts[String(args.run_id)] || []);
    return {data: {artifacts, total_count: artifacts.length}};
  },
}}};
(async () => {
  const selected = await select({github, owner: 'Flow-Steward', repo: 'flow-steward-app',
    repositoryId: 99, workflowId: 44, branch: 'feature', currentRunId: 123});
  process.stdout.write(JSON.stringify({selected, calls}));
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
    result = subprocess.run(
        ["node", "-e", script, str(HELPER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_source_accepts_completed_official_cache_after_success_or_failure(conclusion: str) -> None:
    run = _run_source()
    run["conclusion"] = conclusion
    result = _select([_artifact()], [run])
    assert result["selected"] == {
        "run_id": 10,
        "artifact_id": 100,
        "artifact_name": f"secret-scan-files-{SHA}-2",
        "source_sha": SHA,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", {"id": 1}),
        ("head_repository", {"id": 1}),
        ("workflow_id", 45),
        ("head_sha", "b" * 40),
        ("head_sha", "not-a-sha"),
        ("run_attempt", 3),
        ("run_attempt", 0),
        ("event", "pull_request"),
        ("status", "in_progress"),
        ("conclusion", "cancelled"),
        ("conclusion", "skipped"),
        ("id", 11),
    ],
)
def test_source_rejects_invalid_run_provenance(field: str, value: object) -> None:
    run = _run_source()
    run[field] = value
    result = _select([_artifact()], [run])
    assert result["selected"] is None


@pytest.mark.parametrize("fault", ["expired", "missing", "duplicate", "whole_gate", "bad_name"])
def test_source_rejects_unusable_or_non_scanner_artifact(fault: str) -> None:
    artifact = _artifact()
    run_artifacts = None
    if fault == "expired":
        artifact["expired"] = True
    elif fault == "missing":
        run_artifacts = {"10": []}
    elif fault == "duplicate":
        duplicate = copy.deepcopy(artifact)
        duplicate["id"] = 101
        run_artifacts = {"10": [artifact, duplicate]}
    elif fault == "whole_gate":
        artifact["name"] = f"quality-{SHA}-2"
    else:
        artifact["name"] = "secret-scan-files-malformed-2"
    result = _select([artifact], [_run_source()], run_artifacts=run_artifacts)
    assert result["selected"] is None


@pytest.mark.parametrize("preferred_branch", ["feature", "main", "another-branch"])
def test_source_prefers_branch_then_main_then_latest_official(preferred_branch: str) -> None:
    branches = ["another-branch"]
    if preferred_branch in {"feature", "main"}:
        branches.append("main")
    if preferred_branch == "feature":
        branches.append("feature")
    artifacts = [_artifact(index, branch, index * 10) for index, branch in enumerate(branches, 10)]
    runs = [_run_source(index, branch) for index, branch in enumerate(branches, 10)]
    # A newer unrelated source must not beat the preferred branch.
    artifacts[0]["created_at"] = "2026-11-03T11:00:00Z"
    selected = _select(artifacts, runs)["selected"]
    assert selected["run_id"] == runs[-1]["id"]


def test_source_missing_cache_lists_once_without_fetching_runs() -> None:
    result = _select([], [])
    assert result["selected"] is None
    assert [call[0] for call in result["calls"]] == ["list"]


def test_source_selects_latest_when_only_other_branches_have_proofs() -> None:
    newer = _artifact(10, "other-one", 100)
    older = _artifact(11, "other-two", 110)
    older["created_at"] = "2026-11-02T10:00:00Z"
    selected = _select([older, newer], [_run_source(10, "other-one"), _run_source(11, "other-two")])
    assert selected["selected"]["run_id"] == 10


def test_source_accepts_official_dispatch_and_ignores_malformed_discovery_rows() -> None:
    run = _run_source()
    run["event"] = "workflow_dispatch"
    assert _select([None, {}, _artifact()], [run])["selected"]["run_id"] == 10


def test_source_never_imports_from_its_own_current_run() -> None:
    result = _select([_artifact(123)], [_run_source(123)])
    assert result["selected"] is None
    assert [call[0] for call in result["calls"]] == ["list"]


def test_source_limits_validation_to_twenty_candidates() -> None:
    artifacts = [_artifact(index, artifact_id=index * 10) for index in range(1, 26)]
    result = _select(artifacts, [])
    assert result["selected"] is None
    assert len([call for call in result["calls"] if call[0] == "list"]) == 1
    assert len([call for call in result["calls"] if call[0] == "run"]) == 20


def test_workflow_carries_only_scanner_proofs_automatically_and_prefers_explicit_source() -> None:
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/remote-quality.yml").read_text())
    steps = workflow["jobs"]["quality"]["steps"]
    selector = next(step for step in steps if step.get("id") == "scanner-cache-source")
    assert "github.event_name == 'push'" in selector["if"]
    download = next(step for step in steps if step.get("id") == "restore-scanner-cache")
    assert download["with"]["name"] == "${{ steps.scanner-cache-source.outputs.artifact_name }}"
    assert download["with"]["path"] == "${{ runner.temp }}/scanner-cache"
    assert download["continue-on-error"] is True
    importer = next(step for step in steps if "file_scan_cache import" in step.get("run", ""))
    assert importer["run"].index("source-quality/quality/file-results/") < importer["run"].index(
        "scanner-cache/detect-secrets.json"
    )
    assert '--cache-root "$RUNNER_TEMP/fs-quality-packages/file-results"' in importer["run"]
    assert "cp -R" not in importer["run"]
    uploader = next(
        step
        for step in steps
        if step.get("with", {}).get("name", "").startswith("secret-scan-files-")
    )
    assert uploader["with"]["path"] == "logs/quality/file-results/detect-secrets.json"
    assert uploader["with"]["retention-days"] == 3
    assert uploader["if"] == "always()"
