from __future__ import annotations

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
