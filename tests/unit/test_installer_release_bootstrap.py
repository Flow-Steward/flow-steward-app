from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/Flow-Steward/flow-steward-app"


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _init(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "installer-test@example.com")
    _git(path, "config", "user.name", "Installer test")


@pytest.fixture
def bootstrap_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin"
    _init(origin)
    _script(
        origin / "install.sh",
        'cd "$(dirname "$0")"\n'
        '[ "${TEST_REFUSE_INSTALL:-}" != yes ] || exit 9\n'
        "mkdir -p .flow-steward\n"
        'printf "FS_COMPOSE_PROJECT_NAME=%s\\n" "$FS_COMPOSE_PROJECT_NAME" > .flow-steward/compose.env\n'
        'printf "%s\\n" "$PWD" "$FS_COMPOSE_PROJECT_NAME" '
        '"$(git describe --exact-match --tags)" "$@"',
    )
    _script(origin / "flow-steward", 'printf "%s\\n" "$FS_COMPOSE_PROJECT_NAME" "managed $*"')
    for version in ("1.2.3", "1.2.4"):
        (origin / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", f"Release {version}")
        _git(origin, "tag", "-a", f"v{version}", "-m", f"Release {version}")

    checkout = tmp_path / "demo.flowsteward.com"
    _init(checkout)
    for name in ("install.sh", "flow-steward"):
        shutil.copy2(ROOT / name, checkout / name)
    scripts = ROOT / "core/entrypoints/cli/scripts"
    for helper in scripts.glob("*managed_release*.sh"):
        dest = checkout / helper.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(helper, dest)
    (checkout / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "Unreleased source branch")

    latest = tmp_path / "latest"
    latest.write_text(f"{REPOSITORY}/releases/tag/v1.2.3", encoding="utf-8")
    shims = tmp_path / "bin"
    shims.mkdir()
    _script(shims / "id", "printf '1000\\n'")
    _script(shims / "curl", 'cat "$TEST_LATEST_RELEASE"')
    real_git = shutil.which("git")
    assert real_git is not None
    _script(
        shims / "git",
        'if [ "${1:-}" = clone ] && [ "${TEST_FAIL_RELEASE_CLONE:-}" = yes ]; then exit 1; fi\n'
        'exec "$TEST_REAL_GIT" "$@"',
    )
    docker_commands = tmp_path / "docker-commands"
    _script(shims / "docker", 'printf "%s\\n" "$*" >> "$TEST_DOCKER_COMMANDS"\nexit 7')
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TEST_LATEST_RELEASE", str(latest))
    monkeypatch.setenv("TEST_DOCKER_COMMANDS", str(docker_commands))
    monkeypatch.setenv("TEST_REAL_GIT", real_git)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"{REPOSITORY}.git")
    for key in ("FS_COMPOSE_PROJECT_NAME", "FS_INSTALLER_STATE_DIR", "FS_RELEASE_REPOSITORY"):
        monkeypatch.delenv(key, raising=False)
    return checkout, latest, docker_commands


def _run(checkout: Path, command: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(checkout / command), *args],
        cwd=checkout,
        check=False,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_first_install_pins_stable_release_and_routes_management(bootstrap_host) -> None:
    checkout, latest, docker_commands = bootstrap_host
    source_head = _git(checkout, "rev-parse", "HEAD")
    (checkout / "VERSION").write_text("9.9.10\n", encoding="utf-8")

    first = _run(checkout, "install.sh", "--confirm")

    assert first.returncode == 0, first.stderr
    deployment, project, tag, flag = first.stdout.splitlines()
    assert Path(deployment).is_relative_to(checkout / ".flow-steward")
    assert project.startswith("flow-steward-")
    assert tag == "v1.2.3"
    assert flag == "--confirm"
    assert _git(checkout, "rev-parse", "HEAD") == source_head
    assert _git(checkout, "branch", "--show-current") == "main"
    assert (checkout / "VERSION").read_text(encoding="utf-8") == "9.9.10\n"
    assert not docker_commands.exists()

    latest.write_text(f"{REPOSITORY}/releases/tag/v1.2.4", encoding="utf-8")
    resumed = _run(checkout, "install.sh", "--confirm")
    status = _run(checkout, "flow-steward", "status")

    assert resumed.returncode == 0, resumed.stderr
    assert resumed.stdout == first.stdout
    assert status.returncode == 0, status.stderr
    assert status.stdout.splitlines() == [project, "managed status"]


def test_fresh_branch_on_an_older_release_commit_still_selects_latest(bootstrap_host) -> None:
    checkout, _, docker_commands = bootstrap_host
    _git(checkout, "tag", "v9.9.9")

    result = _run(checkout, "install.sh")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[2] == "v1.2.3"
    assert _git(checkout, "branch", "--show-current") == "main"
    assert not docker_commands.exists()


def test_independent_install_directories_get_distinct_default_projects(bootstrap_host) -> None:
    checkout, _, _ = bootstrap_host
    another_checkout = checkout.with_name("another.flowsteward.com")
    shutil.copytree(checkout, another_checkout)

    first = _run(checkout, "install.sh")
    second = _run(another_checkout, "install.sh")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout.splitlines()[1] != second.stdout.splitlines()[1]


def test_management_never_attempts_repair_before_compose_configuration(
    bootstrap_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, _, docker_commands = bootstrap_host
    monkeypatch.setenv("FS_COMPOSE_PROJECT_NAME", "flow-steward")
    monkeypatch.setenv("TEST_REFUSE_INSTALL", "yes")
    assert _run(checkout, "install.sh").returncode == 9

    status = _run(checkout, "flow-steward", "status")

    assert status.returncode != 0
    assert status.stdout == ""
    assert "Rerun ./install.sh" in status.stderr
    assert not docker_commands.exists()


def test_retry_after_download_failure_preserves_release_and_project(
    bootstrap_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, latest, docker_commands = bootstrap_host
    monkeypatch.setenv("TEST_FAIL_RELEASE_CLONE", "yes")
    monkeypatch.setenv("FS_COMPOSE_PROJECT_NAME", "flow-steward-demo")

    failed = _run(checkout, "install.sh")

    assert failed.returncode != 0
    assert "v1.2.3" in failed.stderr
    assert not docker_commands.exists()

    latest.write_text(f"{REPOSITORY}/releases/tag/v1.2.4", encoding="utf-8")
    monkeypatch.delenv("TEST_FAIL_RELEASE_CLONE")
    monkeypatch.delenv("FS_COMPOSE_PROJECT_NAME")
    resumed = _run(checkout, "install.sh")

    assert resumed.returncode == 0, resumed.stderr
    assert resumed.stdout.splitlines()[1:] == ["flow-steward-demo", "v1.2.3"]

    monkeypatch.setenv("FS_COMPOSE_PROJECT_NAME", "flow-steward")
    conflict = _run(checkout, "install.sh")

    assert conflict.returncode != 0
    assert "flow-steward-demo" in conflict.stderr
    assert not docker_commands.exists()


def test_managed_release_receives_input_files_from_launch_directory(bootstrap_host) -> None:
    checkout, _, _ = bootstrap_host
    supplied_file = checkout / "connection.ini"
    supplied_file.write_text("fixture configuration\n", encoding="utf-8")
    supplied_file.chmod(0o600)

    result = _run(checkout, "install.sh", "--s3-credentials-path", supplied_file.name)

    assert result.returncode == 0, result.stderr
    deployment, _, _, option, relative_path = result.stdout.splitlines()
    assert option == "--s3-credentials-path"
    imported_file = Path(deployment) / relative_path
    assert imported_file.is_relative_to(Path(deployment) / ".flow-steward" / "import")
    assert imported_file.read_text(encoding="utf-8") == "fixture configuration\n"
    assert stat.S_IMODE(imported_file.stat().st_mode) == 0o600

    supplied_file.unlink()
    assert _run(checkout, "install.sh").returncode == 0
    assert imported_file.read_text(encoding="utf-8") == "fixture configuration\n"


def test_managed_release_refuses_unsafe_relative_inputs(bootstrap_host) -> None:
    checkout, _, docker_commands = bootstrap_host
    supplied_file = checkout / "public.json"
    supplied_file.touch(mode=0o644)
    supplied_file.chmod(0o644)
    escaped_directory = checkout / "external"
    escaped_directory.symlink_to(checkout.parent, target_is_directory=True)
    external_file = checkout.parent / "outside.pem"
    external_file.touch()

    loose_permissions = _run(checkout, "install.sh", "--s3-credentials-path=public.json")
    escape = _run(
        checkout,
        "install.sh",
        "--tls-mode=provided",
        "--certificate-path=external/outside.pem",
    )

    assert loose_permissions.returncode != 0
    assert "0600" in loose_permissions.stderr
    assert escape.returncode != 0
    assert "inside the launch checkout" in escape.stderr
    assert not docker_commands.exists()


def test_retry_refuses_to_execute_a_tag_moved_after_selection(
    bootstrap_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, latest, docker_commands = bootstrap_host
    monkeypatch.setenv("TEST_FAIL_RELEASE_CLONE", "yes")
    assert _run(checkout, "install.sh").returncode != 0
    monkeypatch.delenv("TEST_FAIL_RELEASE_CLONE")
    origin = latest.parent / "origin"
    _git(origin, "switch", "--detach", "v1.2.3")
    _git(origin, "commit", "--allow-empty", "-m", "Changed release commit")
    _git(origin, "tag", "--force", "v1.2.3")

    result = _run(checkout, "install.sh")

    assert result.returncode != 0
    assert "do not match the recorded release v1.2.3" in result.stderr
    assert not docker_commands.exists()


@pytest.mark.parametrize("original", ["configuration", "source", "exact-release"])
def test_install_preserves_existing_or_explicit_checkout(bootstrap_host, original: str) -> None:
    checkout, _, docker_commands = bootstrap_host
    existing_config = checkout / ".env.prod"
    arguments: tuple[str, ...] = ()
    if original == "configuration":
        existing_config.write_text("FS_COMPOSE_PROJECT_NAME=existing-dev\n", encoding="utf-8")
    elif original == "source":
        arguments = ("--from-source",)
    else:
        _git(checkout, "tag", "v9.9.9")
        _git(checkout, "switch", "--detach", "v9.9.9")

    result = _run(checkout, "install.sh", *arguments)

    assert result.returncode == 7
    assert docker_commands.read_text(encoding="utf-8").startswith("context inspect ")
    assert not (checkout / ".flow-steward" / "bootstrap-release").exists()
    if original == "configuration":
        assert (
            existing_config.read_text(encoding="utf-8") == "FS_COMPOSE_PROJECT_NAME=existing-dev\n"
        )


def test_managed_launch_refuses_configuration_for_another_compose_project(bootstrap_host) -> None:
    checkout, _, docker_commands = bootstrap_host
    installed = _run(checkout, "install.sh")
    assert installed.returncode == 0, installed.stderr
    managed_root = Path(installed.stdout.splitlines()[0])
    state = managed_root / ".flow-steward"
    state.mkdir(exist_ok=True)
    (state / "compose.env").write_text("FS_COMPOSE_PROJECT_NAME=flow-steward\n", encoding="utf-8")

    for command in ("install.sh", "flow-steward"):
        result = _run(checkout, command)

        assert result.returncode != 0
        assert result.stdout == ""
        assert "Compose project" in result.stderr
    assert not docker_commands.exists()


@pytest.mark.parametrize("tag", ["v1.2.4-rc.1", "../other", "v01.2.3"])
def test_first_install_rejects_nonrelease_redirect(bootstrap_host, tag: str) -> None:
    checkout, latest, docker_commands = bootstrap_host
    latest.write_text(f"{REPOSITORY}/releases/tag/{tag}", encoding="utf-8")

    result = _run(checkout, "install.sh")

    assert result.returncode != 0
    assert "stable" in result.stderr.lower()
    assert not docker_commands.exists()
