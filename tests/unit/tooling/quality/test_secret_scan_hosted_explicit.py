from __future__ import annotations

import copy
import json
import subprocess

import pytest

from tests.unit.tooling.quality.test_secret_scan_hosted_cache import (
    HELPER,
    SHA,
    _artifact,
    _run_source,
)


def _explicit(requested: object = "10", *, run=None, artifacts=None, **overrides) -> dict:
    payload = {
        "requested": requested,
        "run": _run_source() if run is None else run,
        "artifacts": [_artifact()] if artifacts is None else artifacts,
        **overrides,
    }
    script = r"""
const select = require(process.argv[1]);
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const calls = [];
const github = {rest: {actions: {
  async listArtifactsForRepo() {
    calls.push(['discovery']);
    return {data: {artifacts: [], total_count: 0}};
  },
  async getWorkflowRun(args) {
    calls.push(['run', args.run_id]);
    if (data.fail === 'run') throw new Error('Requested run unavailable');
    return {data: data.run};
  },
  async listWorkflowRunArtifacts(args) {
    calls.push(['artifacts', args.run_id]);
    if (data.fail === 'artifacts') throw new Error('Requested artifacts unavailable');
    return {data: {artifacts: data.artifacts,
      total_count: data.total_count ?? data.artifacts.length}};
  },
}}};
(async () => {
  try {
    const selected = await select({github, owner: data.owner ?? 'Flow-Steward',
      repo: 'flow-steward-app', repositoryId: 99, workflowId: 44,
      branch: 'feature', currentRunId: 123, requestedRunId: data.requested});
    process.stdout.write(JSON.stringify({selected, calls}));
  } catch (error) {
    process.stdout.write(JSON.stringify({error: String(error), calls}));
  }
})();
"""
    result = subprocess.run(
        ["node", "-e", script, str(HELPER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("event", "conclusion"), [("push", "success"), ("workflow_dispatch", "failure")]
)
def test_explicit_scanner_source_accepts_exact_official_artifact(
    event: str, conclusion: str
) -> None:
    run = _run_source()
    run.update(event=event, conclusion=conclusion)
    result = _explicit(run=run)
    assert result.get("selected") == {
        "run_id": 10,
        "artifact_id": 100,
        "artifact_name": f"secret-scan-files-{SHA}-2",
        "source_sha": SHA,
    }
    assert result["calls"] == [["run", 10], ["artifacts", 10]]


@pytest.mark.parametrize("requested", ["0", "-1", "01", " 10", "10x", "9007199254740992", 10, None])
def test_explicit_scanner_source_rejects_invalid_id_without_discovery(requested: object) -> None:
    result = _explicit(requested)
    assert result.get("error"), result
    assert result["calls"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 11),
        ("workflow_id", 45),
        ("repository", {"id": 1}),
        ("head_repository", {"id": 1}),
        ("head_sha", "invalid"),
        ("run_attempt", 0),
        ("event", "pull_request"),
        ("status", "in_progress"),
        ("conclusion", "cancelled"),
    ],
)
def test_explicit_scanner_source_provenance_failure_is_not_a_cache_miss(
    field: str, value: object
) -> None:
    run = _run_source()
    run[field] = value
    result = _explicit(run=run)
    assert result.get("error"), result
    assert result["calls"] == [["run", 10]]


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate",
        "expired",
        "bad_id",
        "wrong_sha",
        "wrong_attempt",
        "wrong_run",
        "whole_gate",
        "incomplete",
    ],
)
def test_explicit_scanner_source_rejects_bad_artifact_without_fallback(fault: str) -> None:
    artifact = _artifact()
    artifacts = [artifact]
    extra = {}
    if fault == "missing":
        artifacts = []
    elif fault == "duplicate":
        duplicate = copy.deepcopy(artifact)
        duplicate["id"] = 101
        artifacts.append(duplicate)
    elif fault == "expired":
        artifact["expired"] = True
    elif fault == "bad_id":
        artifact["id"] = 0
    elif fault == "wrong_sha":
        artifact["workflow_run"]["head_sha"] = "b" * 40
    elif fault == "wrong_attempt":
        artifact["name"] = f"secret-scan-files-{SHA}-1"
    elif fault == "wrong_run":
        artifact["workflow_run"]["id"] = 11
    elif fault == "whole_gate":
        artifact["name"] = f"quality-{SHA}-2"
    else:
        extra["total_count"] = 101
    result = _explicit(artifacts=artifacts, **extra)
    assert result.get("error"), result
    assert result["calls"] == [["run", 10], ["artifacts", 10]]


@pytest.mark.parametrize("failure", ["run", "artifacts"])
def test_explicit_scanner_source_api_failure_never_falls_back(failure: str) -> None:
    result = _explicit(fail=failure)
    assert result.get("error"), result
    assert not any(call[0] == "discovery" for call in result["calls"])


def test_empty_explicit_source_preserves_automatic_discovery() -> None:
    result = _explicit("")
    assert result.get("selected") is None
    assert result["calls"] == [["discovery"]]
