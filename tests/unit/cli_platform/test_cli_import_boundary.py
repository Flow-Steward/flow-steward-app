"""Import boundary for the standalone lifecycle CLI distribution."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[3]

_BLOCKER = """
import importlib.abc
import sys

blocked = {
    "anthropic", "fastapi", "langchain", "langgraph", "mcp", "numpy",
    "openai", "openpyxl", "pika", "polars", "scipy", "sknetwork",
    "tree_sitter", "tree_sitter_languages", "uvicorn",
}

class BlockProductRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise ModuleNotFoundError(f"forbidden standalone CLI import: {fullname}")
        return None

sys.meta_path.insert(0, BlockProductRuntime())
"""


def test_public_help_does_not_import_product_runtime_packages() -> None:
    """Adding an eager product import must break standalone CLI startup."""
    program = _BLOCKER + textwrap.dedent(
        """
        from core.entrypoints.cli.lifecycle_click_app import click_app
        click_app.main(args=["--help"], prog_name="flow-steward", standalone_mode=False)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Flow Steward public management commands" in result.stdout


def test_lifecycle_runtime_composition_does_not_import_product_packages() -> None:
    """Adding an eager product dependency to local lifecycle composition must fail."""
    program = _BLOCKER + textwrap.dedent(
        """
        from core.entrypoints.cli.lifecycle_runtime import LifecycleCLIRuntime
        LifecycleCLIRuntime()
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_lifecycle_service_binding_does_not_import_product_packages() -> None:
    """Resolving a local binding must not import product service implementations."""
    program = _BLOCKER + textwrap.dedent(
        """
        from core.entrypoints.cli.platform.service_registry import SERVICE_FACTORIES
        assert callable(SERVICE_FACTORIES["readiness.status"])
        prefix = "core.entrypoints.cli.platform.service_bindings."
        loaded_bindings = {
            name.removeprefix(prefix).split(".", 1)[0]
            for name in sys.modules
            if name.startswith(prefix)
        }
        assert loaded_bindings <= {"common", "contracts", "readiness"}, loaded_bindings
        forbidden_service_modules = {
            "core.application.reports.reporting_service",
            "core.application.tooling.db.service",
            "core.application.tooling.i18n.service",
            "core.application.tooling.quality.service",
            "core.application.tooling.testing.service",
        }
        assert forbidden_service_modules.isdisjoint(sys.modules), (
            forbidden_service_modules & sys.modules.keys()
        )
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
