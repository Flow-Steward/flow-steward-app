from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SELECTOR = ROOT / "scripts/release/select-verified-quality.cjs"
SHA = "a" * 40


def test_release_falls_back_to_full_gate_if_lookup_fails_or_finds_nothing() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-images.yml").read_text())
    quality = workflow["jobs"]["quality"]
    steps = quality["steps"]
    lookup = next(step for step in steps if step.get("id") == "previous-quality")
    assert lookup["continue-on-error"] is True
    gate = next(step for step in steps if "lint.sh --full" in step.get("run", ""))
    assert gate["if"] == (
        "needs.resolve.outputs.tag != 'v0.9.9' && ("
        "steps.previous-quality.outcome != 'success' || "
        "steps.previous-quality.outputs.reuse != 'true')"
    )
    assert "needs.resolve.outputs.revision" in gate["env"]["RELEASE_REVISION"]


@pytest.mark.parametrize("tag", ["v0.9.9", "v0.9.10"])
@pytest.mark.parametrize("outcome", ["success", "failure", "skipped"])
@pytest.mark.parametrize("reuse", ["true", "false"])
def test_release_gate_conditions_preserve_focused_exception_and_reuse(
    tag: str, outcome: str, reuse: str
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-images.yml").read_text())
    steps = workflow["jobs"]["quality"]["steps"]
    names = {
        "lookup": lambda step: step.get("id") == "previous-quality",
        "prepare": lambda step: step.get("uses") == "./.github/actions/prepare-quality",
        "postgres": lambda step: (
            step.get("name") == "Build the canonical disposable PostgreSQL image"
        ),
        "full": lambda step: "lint.sh --full" in step.get("run", ""),
        "focused": lambda step: step.get("name") == "Focused acceptance (full suite not rerun)",
    }
    expressions = {
        name: next(step["if"] for step in steps if predicate(step))
        for name, predicate in names.items()
    }
    command = (
        "const x=JSON.parse(process.argv[1]);const needs={resolve:{outputs:{tag:x.tag}}};"
        "const steps={'previous-quality':{outcome:x.outcome,outputs:{reuse:x.reuse}}};"
        "process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(x.expressions)"
        ".map(([name,expression])=>[name,Function('needs','steps','return ('+expression.replaceAll('steps.previous-quality',\"steps['previous-quality']\")+')')(needs,steps)]))));"
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            command,
            json.dumps(
                {"tag": tag, "outcome": outcome, "reuse": reuse, "expressions": expressions}
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = json.loads(result.stdout)
    focused = tag == "v0.9.9"
    proven = outcome == "success" and reuse == "true"
    assert actual == {
        "lookup": not focused,
        "prepare": focused or not proven,
        "postgres": focused or not proven,
        "full": not focused and not proven,
        "focused": focused,
    }


def _select(run: dict, jobs: list[dict]) -> bool:
    command = (
        "const choose=require(process.argv[1]);"
        "const input=JSON.parse(process.argv[2]);"
        "process.stdout.write(JSON.stringify(choose(input.run,input.jobs,input.sha,42,99)));"
    )
    result = subprocess.run(
        ["node", "-e", command, str(SELECTOR), json.dumps({"run": run, "jobs": jobs, "sha": SHA})],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _valid() -> tuple[dict, list[dict]]:
    run = {
        "head_sha": SHA,
        "workflow_id": 99,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "repository": {"id": 42},
        "head_repository": {"id": 42},
    }
    steps = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in (
            "Require a clean exact-SHA candidate including current main",
            "Run canonical Tier 2",
            "Recheck current main before starting Tier 3",
            "Run cumulative gate with Tier 3",
            "Reject results if main advanced beyond the candidate",
        )
    ]
    return run, [
        {"name": "quality", "status": "completed", "conclusion": "success", "steps": steps}
    ]


def test_matching_complete_hosted_gate_can_be_reused() -> None:
    run, jobs = _valid()
    assert _select(run, jobs) is True
    run["event"] = "workflow_dispatch"
    assert _select(run, jobs) is True


@pytest.mark.parametrize(
    "fault",
    (
        "sha",
        "workflow",
        "event",
        "repository",
        "head_repository",
        "run_failed",
        "job_failed",
        "missing_step",
        "step_skipped",
    ),
)
def test_unproven_hosted_gate_cannot_be_reused(fault: str) -> None:
    run, jobs = _valid()
    if fault == "sha":
        run["head_sha"] = "b" * 40
    elif fault == "workflow":
        run["workflow_id"] = 100
    elif fault == "event":
        run["event"] = "pull_request"
    elif fault == "repository":
        run["repository"] = {"id": 43}
    elif fault == "head_repository":
        run["head_repository"] = {"id": 43}
    elif fault == "run_failed":
        run["conclusion"] = "failure"
    elif fault == "job_failed":
        jobs[0]["conclusion"] = "failure"
    elif fault == "missing_step":
        jobs[0]["steps"].pop(3)
    elif fault == "step_skipped":
        jobs[0]["steps"][3]["conclusion"] = "skipped"
    assert _select(run, jobs) is False
