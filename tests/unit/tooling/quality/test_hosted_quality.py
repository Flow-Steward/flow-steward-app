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


@pytest.mark.skipif(
    os.geteuid() != 0, reason="mixed-UID file-access regression needs root in fs-audit"
)
def test_cumulative_reader_can_read_both_container_writers() -> None:
    import pwd
    import sys
    import tempfile

    fs = pwd.getpwnam("fs")
    source = (ROOT / "core/entrypoints/cli/scripts/lint.sh").read_text()
    dispatch = source.split("python -m core.application.tooling.quality.release_gate", 1)[0]
    dispatch = dispatch.rsplit("fs_compose run", 1)[1]
    reader_uid = 0 if "--user 0:0" in dispatch else fs.pw_uid
    reader_gid = 0 if reader_uid == 0 else fs.pw_gid
    with tempfile.TemporaryDirectory(prefix="fs-mixed-evidence-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        for name, uid, gid in (("root.json", 0, 0), ("fs.json", fs.pw_uid, fs.pw_gid)):
            record = root / name
            record.write_text('{"status":"passed"}\n')
            record.chmod(0o600)
            os.chown(record, uid, gid)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; [p.read_bytes() for p in pathlib.Path(sys.argv[1]).iterdir()]",
                str(root),
            ],
            user=reader_uid,
            group=reader_gid,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr
