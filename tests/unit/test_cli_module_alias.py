"""The public CLI has a single root launcher: ``./flow-steward``."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_root_cli_py_launcher_is_retired() -> None:
    assert not (ROOT / "cli.py").exists()
