"""Regression tests for typed tooling command service boundaries."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[3]


class _InlineRunner:
    def run_callable(self, **kwargs: Any) -> Any:
        from core.application.tooling.contracts import ToolRunResult

        target = kwargs["target"]
        assert callable(target)
        exit_code = int(target() or 0)
        return ToolRunResult(
            operation=str(kwargs["operation"]),
            status="completed" if exit_code == 0 else "failed",
            exit_code=exit_code,
            command=[str(kwargs["operation"])],
        )

    def run_process(self, **_kwargs: Any) -> Any:
        raise AssertionError("application callable test must not launch an OS process")


def test_migrated_tooling_services_do_not_launch_legacy_cli_modules() -> None:
    service_paths = [
        ROOT / "core/application/tooling/testing/service.py",
        ROOT / "core/application/tooling/quality/service.py",
        ROOT / "core/application/tooling/i18n/service.py",
    ]

    for path in service_paths:
        source = path.read_text(encoding="utf-8")
        assert "run_python_module" not in source
        assert "python -m core.entrypoints.cli" not in source


def test_tooling_modules_do_not_keep_public_argparse_entrypoints() -> None:
    source = (ROOT / "core/application/tooling/testing/nightfire_regression.py").read_text(
        encoding="utf-8"
    )

    assert "argparse.ArgumentParser" not in source
    assert "--auth-password" not in source


def test_migrated_extension_tooling_has_only_typed_operation_surfaces() -> None:
    paths = [
        ROOT / "core/application/tooling/extensions/bundle_extension.py",
        ROOT / "core/application/tooling/extensions/validate_extensions.py",
        ROOT / "core/application/tooling/extensions/test_extension.py",
        ROOT / "core/application/tooling/extensions/deps.py",
    ]

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "import argparse" not in source, path
        assert "argparse.Namespace" not in source, path
        assert "def main(" not in source, path
        assert 'if __name__ == "__main__"' not in source, path


def test_task_ingress_extension_scaffold_has_no_legacy_argparse_entrypoint() -> None:
    path = ROOT / "core/application/tooling/extensions/create_task_ingress_extension.py"
    source = path.read_text(encoding="utf-8")

    assert "import argparse" not in source
    assert "argparse.ArgumentParser" not in source
    tree = ast.parse(source)
    assert not any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body)
    assert not any(
        isinstance(node, ast.If) and "__main__" in ast.unparse(node.test) for node in tree.body
    )


def test_removed_uninstall_plan_contract_is_not_left_in_source() -> None:
    source = (ROOT / "core/application/management/command_specs/extensions.py").read_text(
        encoding="utf-8"
    )

    assert "EXTENSIONS_UNINSTALL_PLAN_SPEC" not in source
    assert "ExtensionUninstallPlanCommandInput" not in source
    assert "ExtensionUninstallPlanView" not in source


def test_testing_service_uses_typed_callables_for_smoke() -> None:
    source = (ROOT / "core/application/tooling/testing/service.py").read_text(encoding="utf-8")

    assert "run_process" not in source
    assert '"-m"' not in source
    assert "core.application.tooling.testing.smoke_test" not in source


def test_testing_modules_do_not_resolve_dotenv_or_run_at_import_time() -> None:
    source = (ROOT / "core/application/tooling/testing/smoke_test.py").read_text(encoding="utf-8")
    assert "include_dotenv=True" not in source
    assert 'if __name__ != "__main__"' not in source
    assert "\nS, H = setup_session()\n" not in source


def test_migrated_tooling_bindings_forward_progress_when_supported() -> None:
    binding_paths = [
        ROOT / "core/entrypoints/cli/platform/service_bindings/testing.py",
        ROOT / "core/entrypoints/cli/platform/service_bindings/quality.py",
        ROOT / "core/entrypoints/cli/platform/service_bindings/localization.py",
    ]

    for path in binding_paths:
        source = path.read_text(encoding="utf-8")
        assert "call_with_optional_progress" in source
        assert "progress_sink" in source


def test_cutover_preflight_uses_typed_database_dsn_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.tooling.testing import service as testing_service_module
    from core.application.tooling.testing.models import TestingCutoverPreflightRequest

    captured: dict[str, object] = {}

    def fake_run_preflight(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        "core.application.tooling.testing.cutover_preflight.run_preflight",
        fake_run_preflight,
    )

    service = testing_service_module.TestingToolingService(
        runner=_InlineRunner(),
        database_dsn_provider=lambda: "postgres://typed-dsn",
    )
    result = service.cutover_preflight(
        TestingCutoverPreflightRequest(project_id="project-1", timeout_seconds=10)
    )

    assert result.exit_code == 0
    assert captured["database_dsn"] == "postgres://typed-dsn"
    assert captured["project_id"] == "project-1"
