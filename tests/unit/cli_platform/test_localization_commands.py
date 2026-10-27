"""Contract tests for schema-driven localization tooling commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@dataclass
class _LocalizationTooling:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _record(self, name: str, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append((name, payload))
        return {
            "operation": f"localization.{name.replace('_', '-')}",
            "status": "completed",
            "exit_code": 0,
            "command": [],
            "stdout": "",
            "stderr": "",
            "artifacts": [],
            "details": payload,
        }

    def bootstrap_catalog(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("bootstrap_catalog", command)

    def rebuild_pot(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("rebuild_pot", command)

    def init_locale(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("init_locale", command)

    def update(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("update", command)

    def compile(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("compile", command)

    def validate_keys(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("validate_keys", command)


@dataclass
class _Runtime:
    localization_tooling_service: _LocalizationTooling = field(default_factory=_LocalizationTooling)
    user_query_service: object = object()
    user_command_service: object = object()
    project_query_service: object = object()
    project_command_service: object = object()
    job_query_service: object = object()
    job_command_service: object = object()
    approval_command_service: object = object()
    reporting_service: object = object()
    ops_health_snapshot_service: object = object()
    workflow_operator_service: object = object()
    extension_query_service: object = object()
    extension_binding_service: object = object()
    extension_scaffold_service: object = object()
    database_tooling_service: object = object()
    testing_tooling_service: object = object()
    quality_tooling_service: object = object()


def _app() -> tuple[Any, _Runtime]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    runtime = _Runtime()
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def test_localization_commands_delegate_to_typed_tooling_service(monkeypatch: Any) -> None:
    _interactive_terminal(monkeypatch)
    app, runtime = _app()
    runner = CliRunner()

    cases = [
        (
            [
                "localization",
                "bootstrap-catalog",
                "--source",
                "core/application/settings/ui_i18n.py",
                "--plain",
            ],
            "bootstrap_catalog",
            {"source": "core/application/settings/ui_i18n.py"},
            "y\n",
        ),
        (["localization", "rebuild-pot", "--plain"], "rebuild_pot", {}, "y\n"),
        (
            [
                "localization",
                "init-locale",
                "de",
                "--label",
                "German",
                "--native-label",
                "Deutsch",
                "--plain",
            ],
            "init_locale",
            {"code": "de", "label": "German", "native_label": "Deutsch"},
            "y\n",
        ),
        (["localization", "update", "--plain"], "update", {}, "y\n"),
        (["localization", "compile", "--force", "--plain"], "compile", {"force": True}, "y\n"),
        (["localization", "validate-keys", "--plain"], "validate_keys", {}, ""),
    ]

    for args, method, expected_subset, stdin in cases:
        result = runner.invoke(app, args, input=stdin)
        assert result.exit_code == 0, (args, result.output)
        name, payload = runtime.localization_tooling_service.calls[-1]
        assert name == method
        for key, value in expected_subset.items():
            assert payload[key] == value


def test_localization_mutation_is_not_invoked_when_review_is_cancelled(monkeypatch: Any) -> None:
    _interactive_terminal(monkeypatch)
    app, runtime = _app()

    result = CliRunner().invoke(app, ["localization", "compile", "--plain"], input="n\n")

    assert result.exit_code == 10, result.output
    assert runtime.localization_tooling_service.calls == []


def _interactive_terminal(monkeypatch: Any) -> None:
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.platform.modes import TerminalFacts

    monkeypatch.setattr(
        click_adapter,
        "_terminal_facts",
        lambda: TerminalFacts(
            stdin_isatty=True,
            stdout_isatty=True,
            columns=100,
            rows=30,
            term="xterm-256color",
            no_color_env=False,
        ),
    )
