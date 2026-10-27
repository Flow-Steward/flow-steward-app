"""Boundary tests for tooling process execution ownership and composition."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).parents[3]


def test_application_tooling_exposes_contracts_without_process_implementation() -> None:
    contracts_path = ROOT / "core/application/tooling/contracts.py"
    old_runner_path = ROOT / "core/application/tooling/process_runner.py"

    assert contracts_path.is_file()
    assert not old_runner_path.exists()

    tree = ast.parse(contracts_path.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        str(node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert imported_roots.isdisjoint(
        {
            "contextlib",
            "io",
            "multiprocessing",
            "os",
            "queue",
            "signal",
            "subprocess",
            "tempfile",
            "threading",
            "time",
        }
    )


class _FakeRunner:
    def __init__(self) -> None:
        self.callable_calls: list[dict[str, Any]] = []
        self.process_calls: list[dict[str, Any]] = []

    def run_callable(self, **kwargs: Any) -> Any:
        from core.application.tooling.contracts import ToolRunResult

        self.callable_calls.append(kwargs)
        return ToolRunResult(
            operation=str(kwargs["operation"]),
            status="completed",
            exit_code=0,
            command=[str(kwargs["operation"])],
        )

    def run_process(self, **kwargs: Any) -> Any:
        from core.application.tooling.contracts import ToolRunResult

        self.process_calls.append(kwargs)
        return ToolRunResult(
            operation=str(kwargs["operation"]),
            status="completed",
            exit_code=0,
            command=list(kwargs["command"]),
        )


def test_tooling_services_dispatch_only_through_injected_runner() -> None:
    from core.application.tooling.i18n.models import LocalizationRebuildPotRequest
    from core.application.tooling.i18n.service import LocalizationToolingService
    from core.application.tooling.quality.models import QualityLintRequest
    from core.application.tooling.quality.runtime_routing import (
        AUDIT_SERVICE,
        delegation_command,
    )
    from core.application.tooling.quality.service import QualityToolingService
    from core.application.tooling.testing.models import TestingSmokeTestRequest
    from core.application.tooling.testing.service import TestingToolingService

    runner = _FakeRunner()
    lint_path = ROOT / "core/entrypoints/cli/scripts/lint.sh"

    TestingToolingService(
        runner=runner,
        database_dsn_provider=lambda: "postgres://typed-dsn",
    ).smoke_test(TestingSmokeTestRequest(project_id="proj_1"))
    QualityToolingService(runner=runner, lint_script_path=lint_path).lint(
        QualityLintRequest(mode="fast", checks="ruff")
    )
    LocalizationToolingService(runner=runner).rebuild_pot(LocalizationRebuildPotRequest())

    assert [call["operation"] for call in runner.callable_calls] == [
        "testing.smoke-test",
        "localization.rebuild-pot",
    ]
    assert runner.process_calls == [
        {
            "operation": "quality.lint",
            "command": delegation_command(
                AUDIT_SERVICE,
                [
                    "bash",
                    str(lint_path),
                    "--inside",
                    "--permission-preflight",
                    "--fast",
                    "--checks",
                    "ruff",
                ],
            ),
            "timeout_seconds": 3600,
            "progress_sink": None,
        }
    ]


def test_cli_runtime_injects_one_os_runner_into_all_tooling_services() -> None:
    from core.entrypoints.cli.runtime import CLIRuntime
    from core.infrastructure.tooling.process_runner import OSProcessRunner

    runtime = CLIRuntime()
    runner = runtime.tool_runner

    assert isinstance(runner, OSProcessRunner)
    assert runtime.testing_tooling_service._runner is runner
    assert runtime.quality_tooling_service._runner is runner
    assert runtime.localization_tooling_service._runner is runner
    assert runner.working_directory == ROOT
    assert runtime.quality_tooling_service.lint_script_path == (
        ROOT / "core/entrypoints/cli/scripts/lint.sh"
    )


def test_quality_dependency_heavy_commands_delegate_to_audit_runtime() -> None:
    from core.application.tooling.quality.models import (
        QualityLintRequest,
        QualityVerifyRequest,
    )
    from core.application.tooling.quality.service import QualityToolingService

    runner = _FakeRunner()
    lint_path = ROOT / "core/entrypoints/cli/scripts/lint.sh"

    def delegated(_service: str, argv: list[str]) -> list[str]:
        return ["delegated-to-audit", *argv]

    with (
        patch(
            "core.application.tooling.quality.service.delegation_command",
            side_effect=delegated,
        ),
        patch(
            "core.application.tooling.quality.service.already_delegated",
            return_value=False,
        ),
    ):
        service = QualityToolingService(runner=runner, lint_script_path=lint_path)
        service.lint(QualityLintRequest(mode="fast", checks="ruff"))
        service.verify(QualityVerifyRequest(tier=2, evidence_mode="report"))

    assert runner.callable_calls == []
    assert runner.process_calls[0]["command"] == [
        "delegated-to-audit",
        "bash",
        str(lint_path),
        "--inside",
        "--permission-preflight",
        "--fast",
        "--checks",
        "ruff",
    ]
    assert runner.process_calls[1]["command"] == [
        "delegated-to-audit",
        "python",
        "-m",
        "core.application.tooling.quality.verify",
        "--tier",
        "2",
        "--evidence-mode",
        "report",
        "--fingerprint-source",
        "worktree",
        "--trust",
        "local",
        "--repository-root",
        ".",
        "--evidence-root",
        "logs/quality/evidence",
        "--timeout-seconds",
        "7200",
    ]


def test_quality_lint_does_not_redelegate_from_audit_runtime() -> None:
    from core.application.tooling.quality.models import QualityLintRequest
    from core.application.tooling.quality.service import QualityToolingService

    runner = _FakeRunner()
    lint_path = ROOT / "core/entrypoints/cli/scripts/lint.sh"

    with patch(
        "core.application.tooling.quality.service.already_delegated",
        return_value=True,
    ):
        QualityToolingService(runner=runner, lint_script_path=lint_path).lint(
            QualityLintRequest(mode="fast")
        )

    assert runner.process_calls[0]["command"] == [
        "bash",
        str(lint_path),
        "--inside",
        "--permission-preflight",
        "--fast",
    ]
