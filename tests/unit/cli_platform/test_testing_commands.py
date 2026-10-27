"""Contract tests for schema-driven testing tooling commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@dataclass
class _TestingTooling:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _record(self, name: str, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append((name, payload))
        return _result(f"testing.{name.replace('_', '-')}", payload)

    def smoke_test(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("smoke_test", command)

    def nightfire_regression(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("nightfire_regression", command)

    def fuzz_api(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("fuzz_api", command)

    def fuzz_mcp(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("fuzz_mcp", command)

    def fuzz_ui(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("fuzz_ui", command)

    def cutover_preflight(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("cutover_preflight", command)

    def cutover_smoke(self, command: object, *, progress_sink: Any = None) -> dict[str, Any]:
        return self._record("cutover_smoke", command)


@dataclass
class _Runtime:
    testing_tooling_service: _TestingTooling = field(default_factory=_TestingTooling)
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
    quality_tooling_service: object = object()
    localization_tooling_service: object = object()


def _app() -> tuple[Any, _Runtime]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    runtime = _Runtime()
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def test_testing_commands_delegate_to_typed_tooling_service(monkeypatch: Any) -> None:
    _interactive_terminal(monkeypatch)
    app, runtime = _app()
    runner = CliRunner()

    cases = [
        (
            [
                "testing",
                "smoke-test",
                "--project-id",
                "proj_1",
                "--workflow-id",
                "weekly_report",
                "--lane",
                "ui",
                "--plain",
            ],
            "smoke_test",
            {"project_id": "proj_1", "workflow_id": "weekly_report", "lane": "ui"},
            "y\n",
        ),
        (
            ["testing", "nightfire-regression", "--lane", "backend", "--fail-fast", "--plain"],
            "nightfire_regression",
            {"lane": "backend", "fail_fast": True},
            "y\n",
        ),
        (
            ["testing", "fuzz-api", "--skip-auth", "--strict", "--plain"],
            "fuzz_api",
            {"skip_auth": True, "strict": True},
            "y\n",
        ),
        (
            ["testing", "fuzz-mcp", "--burst-size", "3", "--skip-auth", "--plain"],
            "fuzz_mcp",
            {"burst_size": 3, "skip_auth": True},
            "y\n",
        ),
        (
            ["testing", "fuzz-ui", "--project-id", "proj_1", "--skip-lighthouse", "--plain"],
            "fuzz_ui",
            {"project_id": "proj_1", "skip_lighthouse": True},
            "y\n",
        ),
        (
            ["testing", "cutover-preflight", "--project-id", "proj_1", "--plain"],
            "cutover_preflight",
            {"project_id": "proj_1"},
            "",
        ),
        (
            [
                "testing",
                "cutover-smoke",
                "--project-id",
                "proj_1",
                "--bearer-token-stdin",
                "--confirm",
                "--plain",
            ],
            "cutover_smoke",
            {"project_id": "proj_1", "bearer_token": "service-token"},
            "service-token\n",
        ),
    ]

    for args, method, expected_subset, stdin in cases:
        result = runner.invoke(app, args, input=stdin)
        assert result.exit_code == 0, (args, result.output)
        name, payload = runtime.testing_tooling_service.calls[-1]
        assert name == method
        for key, value in expected_subset.items():
            assert payload[key] == value


def test_software_delivery_testing_commands_are_not_registered(monkeypatch: Any) -> None:
    _interactive_terminal(monkeypatch)
    app, runtime = _app()
    runner = CliRunner()

    for command in ("e2e-live-test", "preflight-check", "workflow-ui-deep-validation"):
        result = runner.invoke(app, ["testing", command, "--plain"], input="y\n")
        assert result.exit_code != 0, (command, result.output)
    assert runtime.testing_tooling_service.calls == []


def test_testing_mutation_is_not_invoked_when_review_is_cancelled(monkeypatch: Any) -> None:
    _interactive_terminal(monkeypatch)
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        ["testing", "fuzz-api", "--skip-auth", "--plain"],
        input="n\n",
    )

    assert result.exit_code == 10, result.output
    assert runtime.testing_tooling_service.calls == []


def _result(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": operation,
        "status": "completed",
        "exit_code": 0,
        "command": [],
        "stdout": "",
        "stderr": "",
        "artifacts": [],
        "details": payload,
    }


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
