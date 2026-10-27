"""The packaged Compact management command works without a host checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.infrastructure.runtime.compact.runtime_secrets import (
    establish_runtime_secrets,
    self_minted_secret_names,
)

ROOT = Path(__file__).resolve().parents[2]


def _run_cli(
    tmp_path: Path,
    *arguments: str,
    profile: str = "compact",
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in self_minted_secret_names():
        environment.pop(name, None)
    environment["FS_COMPACT_DATA_ROOT"] = str(tmp_path)
    environment["FS_DEPLOYMENT_PROFILE"] = profile
    return subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli.compact_container_cli", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _establish_state(tmp_path: Path) -> Path:
    state_path = tmp_path / "state" / "runtime-secrets.json"
    establish_runtime_secrets(state_path=state_path, environment={})
    return state_path


def test_restore_runtime_secrets_preserves_operator_values(tmp_path: Path) -> None:
    from core.entrypoints.cli.compact_container_cli import restore_runtime_secrets

    state_path = _establish_state(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    first = self_minted_secret_names()[0]
    environment = {first: "operator-value"}

    restore_runtime_secrets(state_path, environment)

    assert environment[first] == "operator-value"
    for name in self_minted_secret_names()[1:]:
        assert environment[name] == state["minted"][name]


def test_packaged_compact_command_shows_product_help_with_runtime_state(tmp_path: Path) -> None:
    _establish_state(tmp_path)

    result = _run_cli(tmp_path, "--help")

    assert result.returncode == 0, result.stderr or result.stdout
    assert "users" in result.stdout
    assert "projects" in result.stdout
    assert "config" not in result.stdout
    assert "release" not in result.stdout
    assert "stack" not in result.stdout


def test_compact_command_refuses_invalid_secret_state_without_printing_it(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "runtime-secrets.json"
    state_path.parent.mkdir(parents=True)
    marker = "private-value"
    state_path.write_text(
        json.dumps({"version": 99, "minted": {"FS_SECRET_KEY": marker}}),
        encoding="utf-8",
    )

    result = _run_cli(tmp_path, "--help")

    assert result.returncode != 0
    assert "runtime secret state" in result.stderr.lower()
    assert marker not in result.stderr


def test_compact_command_refuses_non_compact_runtime(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "--help", profile="scalable")

    assert result.returncode == 2
    assert "requires the Compact runtime container" in result.stderr


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("config", "show"), "config"),
        (("environment", "list"), "environment"),
        (("quality", "lint"), "quality"),
        (("stack", "status"), "stack"),
        (("release", "check"), "release"),
        (("testing", "smoke-test"), "testing"),
    ],
)
def test_compact_command_explains_unsupported_host_operation(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected: str,
) -> None:
    result = _run_cli(tmp_path, *arguments)

    assert result.returncode == 2
    assert expected in result.stderr
    assert "not available in Compact" in result.stderr
    assert "Docker" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ("extensions", "install"),
        ("extensions", "uninstall"),
        ("users", "issue-bootstrap-token"),
    ],
)
def test_compact_command_refuses_mutations_owned_by_installation_procedure(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    result = _run_cli(tmp_path, *arguments)

    assert result.returncode == 2
    assert "not available in Compact" in result.stderr
    assert "installation procedure" in result.stderr


def test_compact_command_runs_product_query_with_restored_secret(tmp_path: Path) -> None:
    _establish_state(tmp_path)

    result = _run_cli(tmp_path, "--json", "projects", "list")

    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout)["ok"] is True
