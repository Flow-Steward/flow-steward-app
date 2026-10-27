"""Legacy public argparse surfaces are removed from the Phase 2 CLI."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

ROOT = Path(__file__).parents[3]
REMOVED_MODULES = (
    "core.entrypoints.cli.main",
    "core.entrypoints.cli.users",
    "core.entrypoints.cli.projects",
    "core.entrypoints.cli.workflows",
    "core.entrypoints.cli.tasks",
    "core.entrypoints.cli.jobs",
    "core.entrypoints.cli.reports",
    "core.entrypoints.cli.tooling",
)


class _Registry:
    async def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> dict[str, object]:
        del progress_sink, technical_sink
        raise AssertionError(f"Unexpected service invocation: {key} {value!r}")

    async def choices(self, _key: str) -> tuple[Any, ...]:
        return ()


def _app() -> Any:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(_Registry(), SimpleNamespace())


def test_removed_migrated_parser_modules_are_not_importable() -> None:
    for module in REMOVED_MODULES:
        assert importlib.util.find_spec(module) is None


def test_public_python_module_uses_schema_driven_click_tree() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "Usage: flow-steward" in completed.stdout
    assert "Flow Steward public management commands." in completed.stdout
    assert "Management CLI" not in completed.stdout
    assert "tooling" not in completed.stdout


def test_public_wrappers_do_not_import_or_dispatch_to_old_parser() -> None:
    assert not (ROOT / "cli.py").exists()
    source = "\n".join(
        [
            (ROOT / "core/entrypoints/cli/__main__.py").read_text(encoding="utf-8"),
            (ROOT / "core/entrypoints/cli/click_app.py").read_text(encoding="utf-8"),
        ]
    )

    for forbidden in (
        "core.entrypoints.cli.main",
        "register_tooling_commands",
        "register_users_commands",
        "register_projects_commands",
        "register_jobs_commands",
        "argparse.REMAINDER",
        "runpy",
        "parse_known_args",
    ):
        assert forbidden not in source


def test_old_public_spellings_are_rejected_before_service_invocation() -> None:
    app = _app()
    runner = CliRunner()
    legacy_spellings = (
        ["tooling"],
        ["tool"],
        ["db"],
        ["ext"],
        ["i18n"],
        ["l10n"],
        ["operations"],
        ["backup", "restore", "backups/phase4.tar"],
        ["storage", "migrate", "external_s3"],
        ["tooling", "quality", "check-naming"],
        ["tooling", "db", "upgrade"],
        ["tooling", "testing", "smoke-test"],
        ["tooling", "extensions", "validate"],
        ["users", "force-password-reset", "--email", "operator@example.com"],
        ["quality", "lint", "--", "--fast"],
    )

    for arguments in legacy_spellings:
        result = runner.invoke(app, arguments)
        assert result.exit_code == 2, (arguments, result.output)
        assert (
            "No such command" in result.output
            or "No such option" in result.output
            or "unexpected extra argument" in result.output
        )


def test_public_python_module_rejects_legacy_tooling_path() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli", "tooling", "quality", "check-naming"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert "No such command" in completed.stderr
    assert "check-naming" not in completed.stdout


def test_active_runbooks_do_not_reference_removed_public_cli_commands() -> None:
    runbooks = (
        ROOT / "docs/operations/scheduled-maintenance.md",
        ROOT / "docs/operations/compact-scalable-deployment.md",
        ROOT / "docs/operator-postgresql-restore-runbook.md",
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in runbooks)

    for removed in (
        "core.entrypoints.cli tooling",
        "flow-steward tooling",
        "`tooling db upgrade`",
    ):
        assert removed not in text
