from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SELECTOR = ROOT / "scripts/release/select-verified-quality.cjs"
SHA = "a" * 40


def test_release_blocks_missing_evidence_without_automatically_running_full() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-images.yml").read_text())
    steps = workflow["jobs"]["quality"]["steps"]
    lookup = next(step for step in steps if step.get("id") == "previous-quality")
    assert lookup["continue-on-error"] is True
    gate = next(
        step
        for step in steps
        if step.get("name", "").startswith("Require existing complete exact-SHA")
    )
    assert gate["if"] == (
        "inputs.require_full_quality == true && needs.resolve.outputs.tag != 'v0.9.9' && needs.resolve.outputs.tag != 'v0.9.10' && ("
        "steps.previous-quality.outcome != 'success' || "
        "steps.previous-quality.outputs.reuse != 'true')"
    )
    assert "exit 1" in gate["run"]
    assert not any("lint.sh --full" in step.get("run", "") for step in steps)


@pytest.mark.parametrize("tag", ["v0.9.9", "v0.9.10", "v0.9.11"])
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
        "blocked": lambda step: step.get("name", "").startswith(
            "Require existing complete exact-SHA"
        ),
        "focused": lambda step: step.get("name") == "Focused acceptance (full suite not rerun)",
    }
    expressions = {
        name: next(step["if"] for step in steps if predicate(step))
        for name, predicate in names.items()
    }
    command = (
        "const x=JSON.parse(process.argv[1]);const needs={resolve:{outputs:{tag:x.tag}}};"
        "const inputs={require_full_quality:true};"
        "const steps={'previous-quality':{outcome:x.outcome,outputs:{reuse:x.reuse}}};"
        "process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(x.expressions)"
        ".map(([name,expression])=>[name,Function('needs','steps','inputs','return ('+expression.replaceAll('steps.previous-quality',\"steps['previous-quality']\")+')')(needs,steps,inputs)]))));"
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
        "lookup": tag not in {"v0.9.9", "v0.9.10"},
        "prepare": focused,
        "postgres": focused,
        "blocked": tag not in {"v0.9.9", "v0.9.10"} and not proven,
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


def _explicit_full() -> tuple[dict, list[dict]]:
    run, jobs = _valid()
    run["event"] = "workflow_dispatch"
    jobs[0]["steps"] = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in (
            "Require a clean exact-SHA candidate including current main",
            "Recheck current main before an explicitly requested full gate",
            "Run explicitly requested cumulative full gate",
            "Reject results if main advanced beyond the candidate",
        )
    ]
    return run, jobs


def test_explicit_full_cumulative_step_is_accepted_with_both_candidate_checks() -> None:
    run, jobs = _explicit_full()
    assert _select(run, jobs) is True


@pytest.mark.parametrize(
    "replacement",
    [
        "Execute only the validated impacted plan",
        "Record the explicit v0.9.10 release exception (no Tier 3 or Sonar)",
    ],
)
def test_impacted_or_exception_step_is_not_full_acceptance(replacement: str) -> None:
    run, jobs = _explicit_full()
    jobs[0]["steps"][2]["name"] = replacement
    assert _select(run, jobs) is False


@pytest.mark.parametrize("index", [0, 1, 2, 3])
@pytest.mark.parametrize("fault", ["missing", "skipped", "failed", "duplicate"])
def test_explicit_full_requires_each_unique_successful_boundary(index: int, fault: str) -> None:
    run, jobs = _explicit_full()
    steps = jobs[0]["steps"]
    if fault == "missing":
        steps.pop(index)
    elif fault == "duplicate":
        steps.append(dict(steps[index]))
    else:
        steps[index]["conclusion"] = "skipped" if fault == "skipped" else "failure"
    assert _select(run, jobs) is False


def test_release_full_receipt_is_required_only_by_explicit_opt_in() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-images.yml").read_text())
    steps = workflow["jobs"]["quality"]["steps"]
    gate = next(
        step
        for step in steps
        if step.get("name", "").startswith("Require existing complete exact-SHA")
    )
    script = """
const expression = process.argv[1].replaceAll('steps.previous-quality',"steps['previous-quality']");
const needs = {resolve:{outputs:{tag:'v0.9.11'}}};
const steps = {'previous-quality':{outcome:'failure',outputs:{reuse:'false'}}};
const check = Function('needs','steps','inputs','return ('+expression+')');
process.stdout.write(JSON.stringify([{}, {require_full_quality:false}, {require_full_quality:true}].map(inputs => Boolean(check(needs,steps,inputs)))));
"""
    selected = subprocess.check_output(["node", "-e", script, gate["if"]], text=True)
    assert json.loads(selected) == [False, False, True], (
        "Missing remote full receipts must block only an explicit full-quality requirement"
    )
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["require_full_quality"]["type"] == "boolean"
    assert inputs["require_full_quality"]["default"] is False
    provenance = next(step for step in steps if step.get("id") == "publication-quality-scope")
    assert provenance["env"]["RELEASE_REVISION"] == "${{ needs.resolve.outputs.revision }}"
    assert "local impacted verification" in provenance["run"]
    assert "not a full quality pass" in provenance["run"]
    assert "exit 1" in gate["run"]
