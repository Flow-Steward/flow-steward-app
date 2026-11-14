"""Hosted runs import the evidence of their own history (ADR 0068)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HELPER = ROOT / "core/application/tooling/quality/hosted_evidence_source.cjs"


def _run(run_id: int, sha: str, branch: str) -> dict:
    return {
        "id": run_id,
        "workflow_id": 44,
        "repository": {"id": 99},
        "head_repository": {"id": 99},
        "head_sha": sha,
        "head_branch": branch,
        "event": "push",
        "status": "completed",
    }


def _artifact(artifact_id: int, name: str, run: dict, created: str) -> dict:
    return {
        "id": artifact_id,
        "name": f"{name}-{run['head_sha']}-1",
        "expired": False,
        "created_at": created,
        "workflow_run": {
            "id": run["id"],
            "head_branch": run["head_branch"],
            "head_sha": run["head_sha"],
        },
    }


def _select(artifacts: list[dict], runs: list[dict], ancestors: set[str]) -> list[dict]:
    script = """
const select = require(process.argv[1]);
const input = JSON.parse(process.argv[2]);
const runs = Object.fromEntries(input.runs.map(run => [run.id, run]));
const github = {rest: {actions: {
  listArtifactsForRepo: async () => ({data: {artifacts: input.artifacts}}),
  getWorkflowRun: async ({run_id}) => ({data: runs[run_id]}),
}}};
select({github, owner: 'Flow-Steward', repo: 'flow-steward-app', repositoryId: 99,
  workflowId: 44, currentRunId: 1, isAncestor: sha => input.ancestors.includes(sha)})
  .then(result => process.stdout.write(JSON.stringify(result)));
"""
    payload = json.dumps({"artifacts": artifacts, "runs": runs, "ancestors": sorted(ancestors)})
    output = subprocess.check_output(["node", "-e", script, str(HELPER), payload], text=True)
    return json.loads(output)


def test_any_branch_of_this_history_is_imported_newest_first() -> None:
    release = _run(10, "a" * 40, "claude/release-batch-0.9.18")
    verify = _run(11, "b" * 40, "verify/release-batch-0.9.18")
    sibling = _run(12, "c" * 40, "feature/elsewhere")
    artifacts = [
        _artifact(100, "quality", release, "2026-11-14T08:00:00Z"),
        _artifact(101, "tier3-backend", verify, "2026-11-14T09:00:00Z"),
        _artifact(102, "quality", sibling, "2026-11-14T10:00:00Z"),
        _artifact(103, "secret-scan-files", verify, "2026-11-14T09:00:00Z"),
    ]

    selected = _select(artifacts, [release, verify, sibling], {"a" * 40, "b" * 40})

    assert [item["artifact_id"] for item in selected] == [101, 100]


def test_at_most_three_runs_are_imported() -> None:
    runs = [_run(20 + index, f"{index:x}" * 40, "main") for index in range(5)]
    artifacts = [
        _artifact(200 + index, "quality", run, f"2026-11-14T0{index}:00:00Z")
        for index, run in enumerate(runs)
    ]

    selected = _select(artifacts, runs, {run["head_sha"] for run in runs})

    assert [item["run_id"] for item in selected] == [24, 23, 22]
