"""Secret files under the host's .flow-steward/secret-input/ reach the CLI (#32).

`users create --help` names that directory, but on Scalable the command runs in the
application container and lifecycle commands in fs-cli; neither sees the host directory
at the path the operator typed, so `--password-file .flow-steward/secret-input/<name>`
was refused as `unsafe_secret_file` while `--password-stdin < <file>` worked. The
launcher now checks the file on the host with the CLI's own rules and hands it to the
command on stdin; a file that fails any rule is passed on unchanged for the CLI to
refuse as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.cli_platform.test_launcher import (
    _arg_sequence,
    _log,
    _run_launcher,
    _write_compact_installation,
)
from tests.unit.cli_platform.test_launcher import (
    fake_repo as fake_repo,
)

# pragma: allowlist nextline secret
SECRET_VALUE = "Operator-Fixture-Value-32"
SECRET_RELATIVE = ".flow-steward/secret-input/operator-password"


def _secret_file(repo: Path, *, mode: int = 0o600, name: str = "operator-password") -> Path:
    secret_dir = repo / ".flow-steward" / "secret-input"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_dir.chmod(0o700)
    path = secret_dir / name
    path.write_text(f"{SECRET_VALUE}\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _users_create(*password_arguments: str) -> tuple[str, ...]:
    return (
        "users",
        "create",
        "--email",
        "operator@example.com",
        *password_arguments,
        "--confirm",
        "--json",
    )


def _assert_handed_over_on_stdin(
    log: str, service: str, module: str, *args: str, file_option: str = "--password-file"
) -> None:
    assert _arg_sequence(service, "python", "-m", module, *args) in log
    assert f"STDIN={SECRET_VALUE}" in log
    assert "ARG=-T" in log
    assert f"ARG={file_option}" not in log
    assert not [
        line for line in log.splitlines() if line.startswith("ARG=") and "secret-input" in line
    ]


@pytest.mark.parametrize(
    ("cwd_part", "password_arguments"),
    (
        ("", ("--password-file", SECRET_RELATIVE)),
        ("docs", ("--password-file", f"../{SECRET_RELATIVE}")),
        ("", ("--password-file=ABSOLUTE",)),
    ),
    ids=("relative-from-install-dir", "relative-from-subdirectory", "absolute-joined"),
)
def test_scalable_product_command_receives_the_documented_secret_file_on_stdin(
    fake_repo: tuple[Path, Path],
    cwd_part: str,
    password_arguments: tuple[str, ...],
) -> None:
    repo, _ = fake_repo
    secret = _secret_file(repo)
    cwd = repo / cwd_part if cwd_part else repo
    cwd.mkdir(exist_ok=True)
    arguments = tuple(argument.replace("ABSOLUTE", str(secret)) for argument in password_arguments)

    result = _run_launcher(
        fake_repo,
        *_users_create(*arguments),
        cwd=cwd,
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_CAPTURE_STDIN": "1"},
    )

    assert result.returncode == 0, result.stderr
    _assert_handed_over_on_stdin(
        _log(fake_repo),
        "fs-app",
        "core.entrypoints.cli.click_app",
        *_users_create("--password-stdin"),
    )


def test_compact_product_command_receives_the_documented_secret_file_on_stdin(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _ = fake_repo
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)
    _secret_file(repo)

    result = _run_launcher(
        fake_repo,
        *_users_create("--password-file", SECRET_RELATIVE),
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_CAPTURE_STDIN": "1",
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert result.returncode == 0, result.stderr
    _assert_handed_over_on_stdin(
        _log(fake_repo),
        "compact-runtime",
        "core.entrypoints.cli.click_app",
        *_users_create("--password-stdin"),
    )


def test_warm_cli_command_receives_the_documented_secret_file_on_stdin(
    fake_repo: tuple[Path, Path],
) -> None:
    """A lifecycle command runs in fs-cli, whose working directory is the image's /app."""
    repo, _ = fake_repo
    _secret_file(repo, name="dsn")
    arguments = ("database", "restore-parity-capture", "--dsn-file")

    result = _run_launcher(
        fake_repo,
        *arguments,
        ".flow-steward/secret-input/dsn",
        "--json",
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_CAPTURE_STDIN": "1"},
    )

    assert result.returncode == 0, result.stderr
    _assert_handed_over_on_stdin(
        _log(fake_repo),
        "fs-cli",
        "core.entrypoints.cli.lifecycle_click_app",
        "database",
        "restore-parity-capture",
        "--dsn-stdin",
        "--json",
        file_option="--dsn-file",
    )


def _unsafe_world_readable(repo: Path) -> str:
    _secret_file(repo, mode=0o644)
    return SECRET_RELATIVE


def _unsafe_group_readable(repo: Path) -> str:
    _secret_file(repo, mode=0o640)
    return SECRET_RELATIVE


def _unsafe_symlink(repo: Path) -> str:
    outside = repo / "outside-password"
    outside.write_text(f"{SECRET_VALUE}\n", encoding="utf-8")
    outside.chmod(0o600)
    _secret_file(repo, name="placeholder")
    (repo / SECRET_RELATIVE).symlink_to(outside)
    return SECRET_RELATIVE


def _unsafe_symlinked_directory(repo: Path) -> str:
    outside_dir = repo / "outside-dir"
    outside_dir.mkdir()
    target = outside_dir / "operator-password"
    target.write_text(f"{SECRET_VALUE}\n", encoding="utf-8")
    target.chmod(0o600)
    _secret_file(repo, name="placeholder")
    (repo / ".flow-steward/secret-input/linked").symlink_to(outside_dir)
    return ".flow-steward/secret-input/linked/operator-password"


def _unsafe_outside_secret_input(repo: Path) -> str:
    outside = repo / "operator-password"
    outside.write_text(f"{SECRET_VALUE}\n", encoding="utf-8")
    outside.chmod(0o600)
    return "operator-password"


def _unsafe_directory(repo: Path) -> str:
    _secret_file(repo, name="placeholder")
    (repo / SECRET_RELATIVE).mkdir()
    return SECRET_RELATIVE


@pytest.mark.parametrize(
    "prepare",
    (
        _unsafe_world_readable,
        _unsafe_group_readable,
        _unsafe_symlink,
        _unsafe_symlinked_directory,
        _unsafe_outside_secret_input,
        _unsafe_directory,
    ),
    ids=(
        "world-readable",
        "group-readable",
        "symlink",
        "symlinked-directory",
        "outside-secret-input",
        "directory",
    ),
)
def test_a_file_failing_the_cli_rules_is_passed_on_unchanged(
    fake_repo: tuple[Path, Path],
    prepare,
) -> None:
    repo, _ = fake_repo
    path = prepare(repo)

    result = _run_launcher(
        fake_repo,
        *_users_create("--password-file", path),
        env={"FAKE_IMAGE_REVISION": "test-revision"},
        input_text="",
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert (
        _arg_sequence(
            "fs-app",
            "python",
            "-m",
            "core.entrypoints.cli.click_app",
            *_users_create("--password-file", path),
        )
        in log
    )
    assert "ARG=--password-stdin" not in log
    assert SECRET_VALUE not in log


def test_stdin_taken_by_input_json_keeps_the_file_option(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _ = fake_repo
    _secret_file(repo)
    arguments = (
        "users",
        "create",
        "--input-json",
        "-",
        "--password-file",
        SECRET_RELATIVE,
        "--confirm",
        "--json",
    )

    result = _run_launcher(
        fake_repo,
        *arguments,
        env={"FAKE_IMAGE_REVISION": "test-revision"},
        input_text='{"email":"operator@example.com"}',
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert (
        _arg_sequence("fs-app", "python", "-m", "core.entrypoints.cli.click_app", *arguments) in log
    )
    assert SECRET_VALUE not in log


def test_secret_input_directory_follows_the_selected_state_dir(
    fake_repo: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """FS_INSTALLER_STATE_DIR moves the state directory, and secret-input with it."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    password = _secret_file(state_dir.parent, name="operator-password")
    moved = state_dir / "secret-input"
    moved.mkdir(mode=0o700)
    password = password.rename(moved / "operator-password")

    result = _run_launcher(
        fake_repo,
        *_users_create("--password-file", str(password)),
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CAPTURE_STDIN": "1",
            "FS_INSTALLER_STATE_DIR": str(state_dir),
        },
    )

    assert result.returncode == 0, result.stderr
    _assert_handed_over_on_stdin(
        _log(fake_repo),
        "fs-app",
        "core.entrypoints.cli.click_app",
        *_users_create("--password-stdin"),
    )
