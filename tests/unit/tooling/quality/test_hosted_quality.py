from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "core/entrypoints/cli/scripts/hosted-quality.sh"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.name", "Quality test")
    _git(origin, "config", "user.email", "quality@example.invalid")
    (origin / "source.py").write_text("VALUE = 1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "Initial main")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(origin), str(checkout)], check=True, capture_output=True)
    _git(checkout, "config", "user.name", "Quality test")
    _git(checkout, "config", "user.email", "quality@example.invalid")
    env = {
        **os.environ,
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_REPOSITORY": "Flow-Steward/flow-steward-app",
        "GITHUB_WORKSPACE": str(checkout),
        "GITHUB_SHA": _git(checkout, "rev-parse", "HEAD"),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "quality",
    }
    return origin, env


def _run(env: dict[str, str], command: str = "check-candidate") -> subprocess.CompletedProcess[str]:
    assert SCRIPT.is_file(), "hosted quality adapter is missing"
    return subprocess.run(["bash", str(SCRIPT), command], env=env, text=True, capture_output=True)


def test_current_clean_candidate_is_accepted(candidate: tuple[Path, dict[str, str]]) -> None:
    _, env = candidate
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert env["GITHUB_SHA"] in result.stdout


def test_main_is_fetched_again_and_stale_candidate_is_rejected(
    candidate: tuple[Path, dict[str, str]],
) -> None:
    origin, env = candidate
    assert _run(env).returncode == 0
    (origin / "source.py").write_text("VALUE = 2\n")
    _git(origin, "commit", "-am", "Advance main during verification")
    result = _run(env)
    assert result.returncode != 0
    assert "Update this branch from origin/main" in result.stderr


@pytest.mark.parametrize("change", ["sha", "tracked", "untracked"])
def test_wrong_or_dirty_candidate_is_rejected(
    candidate: tuple[Path, dict[str, str]], change: str
) -> None:
    _, env = candidate
    root = Path(env["GITHUB_WORKSPACE"])
    if change == "sha":
        env["GITHUB_SHA"] = "a" * 40
    elif change == "tracked":
        (root / "source.py").write_text("VALUE = 3\n")
    else:
        (root / "untracked.py").write_text("VALUE = 3\n")
    result = _run(env)
    assert result.returncode != 0
    assert "candidate" in result.stderr


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_ACTIONS", "false"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("GITHUB_REPOSITORY", "other/repository"),
        ("GITHUB_RUN_ID", "../other"),
    ],
)
def test_cleanup_refuses_local_or_unidentified_runner(
    candidate: tuple[Path, dict[str, str]], key: str, value: str
) -> None:
    _, env = candidate
    env[key] = value
    result = _run(env, "cleanup")
    assert result.returncode != 0
    assert "hosted" in result.stderr


def test_toolchain_metadata_does_not_need_docker() -> None:
    source_root = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", str(ROOT)))
    result = subprocess.run(
        [
            "bash",
            str(source_root / "core/entrypoints/cli/scripts/lint.sh"),
            "--audit-toolchain-digest",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    digest = result.stdout.strip()
    assert len(digest) == 40
    assert all(char in "0123456789abcdef" for char in digest)


@pytest.mark.parametrize(("owned", "auto_removed"), [(True, False), (True, True), (False, False)])
def test_cleanup_checks_identity_before_removing_a_leftover_one_off(
    candidate: tuple[Path, dict[str, str]], owned: bool, auto_removed: bool
) -> None:
    _, env = candidate
    root = Path(env["GITHUB_WORKSPACE"])
    fake_bin = root / "bin"
    fake_bin.mkdir()
    calls = root / "docker-calls.jsonl"
    removed = root / "removed"
    project = "fs-quality-123-1-quality" if owned else "another-project"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"calls = pathlib.Path({str(calls)!r})\n"
        f"removed = pathlib.Path({str(removed)!r})\n"
        "args = sys.argv[1:]\n"
        "with calls.open('a') as out: out.write(json.dumps(args) + '\\n')\n"
        "if args[0] == 'ps' and not removed.exists(): print('abc123')\n"
        f"if args[0] == 'inspect': print({project!r})\n"
        "if args[:2] == ['rm', '-f']:\n"
        "    removed.touch()\n"
        f"    sys.exit({1 if auto_removed else 0})\n"
    )
    docker.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = _run(env, "cleanup")
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    if owned:
        assert result.returncode == 0, result.stderr
        assert ["rm", "-f", "abc123"] in recorded
    else:
        assert result.returncode != 0
        assert not any(call[0] == "rm" for call in recorded)
    assert not any("prune" in call for call in recorded)


def test_artifact_ownership_handoff_is_limited_to_owned_outputs(
    candidate: tuple[Path, dict[str, str]],
) -> None:
    _, env = candidate
    root = Path(env["GITHUB_WORKSPACE"])
    for path in ("logs/quality", "logs/coverage", "runner-temp/fs-quality-packages", "bin"):
        (root / path).mkdir(parents=True)
    env["RUNNER_TEMP"] = str(root / "runner-temp")
    calls = root / "ownership.jsonl"
    sudo = root / "bin/sudo"
    sudo.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys\n"
        f"with pathlib.Path({str(calls)!r}).open('a') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    sudo.chmod(0o755)
    env["PATH"] = f"{root / 'bin'}:{env['PATH']}"
    result = _run(env, "prepare-artifacts")
    assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    identity = f"{os.getuid()}:{os.getgid()}"
    assert recorded == [
        ["chown", "-R", identity, "logs/quality"],
        ["chown", "-R", identity, "logs/coverage"],
        ["chown", "-R", identity, str(root / "runner-temp/fs-quality-packages")],
    ]


def test_sonar_bootstrap_does_not_trip_the_secret_scanner() -> None:
    from detect_secrets import SecretsCollection
    from detect_secrets.settings import default_settings

    secrets = SecretsCollection()
    with default_settings():
        secrets.scan_file(str(ROOT / ".github/workflows/remote-quality.yml"))
    assert not secrets.json(), secrets.json()


def test_sonar_bootstrap_generated_password_satisfies_server_policy() -> None:
    source = (ROOT / ".github/workflows/remote-quality.yml").read_text()
    assignment = next(
        line.strip() for line in source.splitlines() if line.strip().startswith("password=")
    )
    result = subprocess.run(
        ["bash", "-euc", assignment + '\n printf "%s" "$password"'],
        capture_output=True,
        text=True,
        check=True,
    )
    # Sonar's bootstrap API rejects a long hexadecimal password: length alone
    # is insufficient; all four character classes are mandatory.
    value = result.stdout
    assert len(value) >= 12
    assert any(char.islower() for char in value)
    assert any(char.isupper() for char in value)
    assert any(char.isdigit() for char in value)
    assert any(not char.isalnum() for char in value)


def test_cumulative_reader_is_dispatched_with_privileged_identity() -> None:
    import shlex

    source = (ROOT / "core/entrypoints/cli/scripts/lint.sh").read_text()
    dispatch = source.split("python -m core.application.tooling.quality.release_gate", 1)[0]
    dispatch = dispatch.rsplit("fs_compose run", 1)[1]
    arguments = shlex.split(dispatch)
    assert arguments[arguments.index("--user") + 1] == "0:0"


def test_retry_restores_previous_attempt_evidence_only_from_same_run() -> None:
    import yaml

    source_root = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", str(ROOT)))
    workflow = yaml.safe_load((source_root / ".github/workflows/remote-quality.yml").read_text())
    quality = workflow["jobs"]["quality"]
    steps = quality["steps"]
    retry = next(step for step in steps if step.get("id") == "previous-attempt")
    assert "GITHUB_RUN_ATTEMPT" in retry["run"]
    restore = next(step for step in steps if step.get("id") == "restore-prior-evidence")
    assert restore["continue-on-error"] is True
    assert restore["with"]["run-id"] == "${{ github.run_id }}"
    assert "github.sha" in restore["with"]["name"]
    assert "previous-attempt" in restore["with"]["name"]
    assert "run_attempt" in restore["if"]
    assert quality["permissions"]["actions"] == "read"


def _remote_quality_workflow() -> dict:
    import yaml

    source_root = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", str(ROOT)))
    return yaml.safe_load((source_root / ".github/workflows/remote-quality.yml").read_text())


def test_explicit_cross_run_import_is_optional_and_fails_closed_before_building() -> None:
    workflow = _remote_quality_workflow()
    # PyYAML's YAML 1.1 loader treats the Actions `on` key as True.
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["reuse_run_id"]["type"] == "string"
    assert inputs["reuse_run_id"]["required"] is False
    quality = workflow["jobs"]["quality"]
    steps = quality["steps"]
    validate = next(step for step in steps if step.get("id") == "validate-reuse-source")
    download = next(step for step in steps if step.get("id") == "restore-source-evidence")
    assert "inputs.reuse_run_id != ''" in validate["if"]
    assert validate["env"]["REUSE_RUN_ID"] == (
        "${{ inputs.reuse_run_id || steps.smoke-source.outputs.run_id }}"
    )
    assert validate.get("continue-on-error", False) is False
    assert download.get("continue-on-error", False) is False
    assert download["with"]["run-id"] == "${{ steps.validate-reuse-source.outputs.run_id }}"
    assert download["with"]["name"] == "${{ steps.validate-reuse-source.outputs.artifact_name }}"
    assert download["with"]["repository"] == "${{ github.repository }}"
    assert download["with"]["github-token"] == "${{ github.token }}"
    assert download["with"]["path"] == "${{ runner.temp }}/source-quality"
    prepare = next(
        step for step in steps if step.get("uses") == "./.github/actions/prepare-quality"
    )
    tier_two = next(step for step in steps if "lint.sh --fast" in step.get("run", ""))
    assert (
        steps.index(validate) < steps.index(download) < steps.index(prepare) < steps.index(tier_two)
    )
    assert quality["permissions"] == {"contents": "read", "actions": "read"}
    assert "needs.quality.outputs.evidence_imported == 'true'" in workflow["jobs"]["sonar"]["if"]


def _validate_reuse_source(run_id: str, run: dict, artifacts: list[dict]) -> dict:
    steps = _remote_quality_workflow()["jobs"]["quality"]["steps"]
    script = next(step for step in steps if step.get("id") == "validate-reuse-source")["with"][
        "script"
    ]
    command = """
const input = JSON.parse(process.argv[1]);
const outputs = {};
const github = {
  rest: {actions: {
    getWorkflow: async () => ({data: {id: 99}}),
    getWorkflowRun: async ({run_id}) => {
      if (String(run_id) !== input.run_id) throw new Error('Wrong requested run');
      return {data: input.run};
    },
    listWorkflowRunArtifacts: () => {},
  }},
  paginate: async (_method, {run_id}) => {
    if (String(run_id) !== input.run_id) throw new Error('Wrong artifact source');
    return input.artifacts;
  },
};
const core = {
  setOutput: (name, value) => { outputs[name] = String(value); },
  info: () => {},
};
const context = {repo: {owner: 'Flow-Steward', repo: 'flow-steward-app'}};
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
new AsyncFunction('github', 'context', 'core', input.script)(github, context, core)
  .then(() => process.stdout.write(JSON.stringify({ok: true, outputs})))
  .catch(error => process.stdout.write(JSON.stringify({ok: false, error: error.message, outputs})));
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            command,
            json.dumps({"script": script, "run_id": run_id, "run": run, "artifacts": artifacts}),
        ],
        env={
            **os.environ,
            "REUSE_RUN_ID": run_id,
            "GITHUB_REPOSITORY": "Flow-Steward/flow-steward-app",
            "GITHUB_REPOSITORY_ID": "42",
            "GITHUB_RUN_ID": "999",
            "GITHUB_SHA": "b" * 40,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _reusable_source() -> tuple[dict, list[dict]]:
    run = {
        "id": 321,
        "head_sha": "a" * 40,
        "run_attempt": 2,
        "workflow_id": 99,
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "repository": {"id": 42},
        "head_repository": {"id": 42},
    }
    artifacts = [{"name": f"quality-{'a' * 40}-2", "expired": False}]
    return run, artifacts


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_cross_sha_import_accepts_official_completed_run_checks(
    event: str, conclusion: str
) -> None:
    run, artifacts = _reusable_source()
    run.update(event=event, conclusion=conclusion)
    result = _validate_reuse_source("321", run, artifacts)
    assert result["ok"] is True, result
    assert result["outputs"] == {
        "run_id": "321",
        "artifact_name": f"quality-{'a' * 40}-2",
        "source_sha": "a" * 40,
    }


@pytest.mark.parametrize(
    "run_id", ["0", "-1", "321junk", "3.21", " 321", "0321", "1e3", "9007199254740992"]
)
def test_cross_run_import_rejects_non_decimal_or_unsafe_run_id(run_id: str) -> None:
    run, artifacts = _reusable_source()
    result = _validate_reuse_source(run_id, run, artifacts)
    assert result["ok"] is False, result
    assert "run ID" in result["error"]
    assert not result["outputs"]


@pytest.mark.parametrize(
    "fault",
    [
        "run_id",
        "workflow",
        "repository",
        "head_repository",
        "missing_head_repository",
        "event",
        "in_progress",
        "cancelled",
        "sha",
        "attempt",
        "missing_artifact",
        "wrong_artifact_sha",
        "wrong_artifact_attempt",
        "expired_artifact",
        "duplicate_artifact",
    ],
)
def test_cross_run_import_rejects_wrong_or_missing_source(fault: str) -> None:
    run, artifacts = _reusable_source()
    if fault == "run_id":
        run["id"] = 322
    elif fault == "workflow":
        run["workflow_id"] = 100
    elif fault in {"repository", "head_repository"}:
        run[fault] = {"id": 43}
    elif fault == "missing_head_repository":
        run["head_repository"] = None
    elif fault == "event":
        run["event"] = "pull_request"
    elif fault == "in_progress":
        run["status"] = "in_progress"
    elif fault == "cancelled":
        run["conclusion"] = "cancelled"
    elif fault == "sha":
        run["head_sha"] = "../bad-sha"
    elif fault == "attempt":
        run["run_attempt"] = 0
    elif fault == "missing_artifact":
        artifacts.clear()
    elif fault == "wrong_artifact_sha":
        artifacts[0]["name"] = f"quality-{'b' * 40}-2"
    elif fault == "wrong_artifact_attempt":
        artifacts[0]["name"] = f"quality-{'a' * 40}-1"
    elif fault == "expired_artifact":
        artifacts[0]["expired"] = True
    elif fault == "duplicate_artifact":
        artifacts.append(artifacts[0].copy())
    result = _validate_reuse_source("321", run, artifacts)
    assert result["ok"] is False, result
    assert not result["outputs"]


def test_import_copies_evidence_unchanged_and_prefers_same_run_attempt() -> None:
    steps = _remote_quality_workflow()["jobs"]["quality"]["steps"]
    restore = next(step for step in steps if step.get("id") == "copy-restored-evidence")
    assert "steps.restore-source-evidence.outcome == 'success'" in restore["if"]
    assert "steps.restore-prior-evidence.outcome == 'success'" in restore["if"]
    script = restore["run"]
    assert script.index('"$RUNNER_TEMP/source-quality"') < script.index(
        '"$RUNNER_TEMP/previous-quality"'
    )
    assert 'cp -R "$previous/quality/evidence/." logs/quality/evidence/' in script
    assert 'cp -R "$previous/coverage/." logs/coverage/' in script
    assert restore["env"]["SOURCE_SHA"] == "${{ steps.validate-reuse-source.outputs.source_sha }}"
    assert restore["env"]["GITHUB_SHA"] == "${{ github.sha }}"
    assert '"$SOURCE_SHA" == "$GITHUB_SHA"' in script


@pytest.mark.parametrize("has_evidence", [False, True])
def test_requested_source_content_is_required_and_copied_without_promoting_commit(
    tmp_path: Path, has_evidence: bool
) -> None:
    steps = _remote_quality_workflow()["jobs"]["quality"]["steps"]
    script = next(step for step in steps if step.get("id") == "copy-restored-evidence")["run"]
    runner_temp = tmp_path / "runner-temp"
    source = runner_temp / "source-quality"
    source.mkdir(parents=True)
    record = '{"commit_sha": "' + "a" * 40 + '", "status": "passed"}\n'
    if has_evidence:
        for name in ("source-quality", "previous-quality"):
            artifact = runner_temp / name
            evidence = artifact / "quality/evidence/check/fingerprint"
            evidence.mkdir(parents=True)
            (evidence / "evidence.json").write_text(record)
            coverage = artifact / "coverage"
            coverage.mkdir()
            (coverage / "coverage.xml").write_text(name)
        reports = source / "quality/hosted"
        reports.mkdir()
        (reports / "old-hosted.log").write_text("prior hosted diagnostics")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text("#!/usr/bin/env bash\nexit 0\n")
    sudo.chmod(0o755)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "RUNNER_TEMP": str(runner_temp),
            "SOURCE_RESTORED": "true",
            "SOURCE_SHA": "a" * 40,
            "GITHUB_SHA": "b" * 40,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if has_evidence:
        assert result.returncode == 0, result.stderr
        assert (
            tmp_path / "logs/quality/evidence/check/fingerprint/evidence.json"
        ).read_text() == record
        assert (tmp_path / "logs/coverage/coverage.xml").read_text() == "previous-quality"
        assert not (tmp_path / "logs/quality/hosted/old-hosted.log").exists()
    else:
        assert result.returncode != 0
        assert "refusing an implicit full run" in result.stdout
        assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("same_sha", [False, True])
def test_source_coverage_is_restored_only_for_exact_current_sha(
    tmp_path: Path, same_sha: bool
) -> None:
    steps = _remote_quality_workflow()["jobs"]["quality"]["steps"]
    script = next(step for step in steps if step.get("id") == "copy-restored-evidence")["run"]
    runner_temp = tmp_path / "runner-temp"
    source = runner_temp / "source-quality"
    evidence = source / "quality/evidence/check/fingerprint"
    evidence.mkdir(parents=True)
    record = '{"commit_sha": "' + "a" * 40 + '", "status": "passed"}\n'
    (evidence / "evidence.json").write_text(record)
    coverage = source / "coverage"
    coverage.mkdir()
    (coverage / "coverage.xml").write_text("source coverage")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text("#!/usr/bin/env bash\nexit 0\n")
    sudo.chmod(0o755)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "RUNNER_TEMP": str(runner_temp),
            "SOURCE_RESTORED": "true",
            "SOURCE_SHA": "a" * 40,
            "GITHUB_SHA": ("a" if same_sha else "b") * 40,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        tmp_path / "logs/quality/evidence/check/fingerprint/evidence.json"
    ).read_text() == record
    restored = tmp_path / "logs/coverage/coverage.xml"
    if same_sha:
        assert restored.read_text() == "source coverage"
    else:
        assert not restored.exists()
