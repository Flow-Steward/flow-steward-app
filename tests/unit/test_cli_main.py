"""Compatibility tests for the package-level public CLI module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_package_module_help_is_schema_driven_click() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage: flow-steward" in result.stdout
    assert "Flow Steward public management commands." in result.stdout
    assert "Management CLI" not in result.stdout


def test_package_module_rejects_removed_tooling_parser() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli", "tooling", "db", "upgrade"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "No such command" in result.stderr
