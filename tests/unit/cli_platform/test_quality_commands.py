"""Contract tests for schema-driven quality tooling commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.entrypoints.cli.platform.service_registry import ServiceRegistry


def test_sonar_failures_receive_stable_diagnostic_codes() -> None:
    from core.application.tooling.contracts import ToolRunResult
    from core.application.tooling.quality.service import _classify_sonar_failure

    cases = (
        ("[sonar-export] Failed to reach SonarQube: refused", "sonar_unreachable"),
        ("[sonar-rules] HTTP 401 from SonarQube", "sonar_authentication_failed"),
        ("[sonar-backlog] No sonar_issues_flat_*.csv files found", "sonar_input_missing"),
    )
    for stderr, expected_code in cases:
        result = _classify_sonar_failure(
            ToolRunResult(
                operation="quality.sonar",
                status="failed",
                exit_code=1,
                stderr=stderr,
                stderr_full=stderr,
            )
        )
        assert result.details["error_code"] == expected_code
        assert result.details["remediation"]


@dataclass
class _QualityTooling:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _record(self, name: str, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append((name, payload))
        return _result(f"quality.{name.replace('_', '-')}", payload)

    def lint(self, command: object, *, progress_sink: object | None = None) -> dict[str, Any]:
        del progress_sink
        return self._record("lint", command)

    def verify(self, command: object, *, progress_sink: object | None = None) -> dict[str, Any]:
        del progress_sink
        return self._record("verify", command)

    def check_ci_excludes(
        self, command: object, *, progress_sink: object | None = None
    ) -> dict[str, Any]:
        del progress_sink
        return self._record("check_ci_excludes", command)

    def check_naming(
        self, command: object, *, progress_sink: object | None = None
    ) -> dict[str, Any]:
        del progress_sink
        return self._record("check_naming", command)

    def release_gate(
        self, command: object, *, progress_sink: object | None = None
    ) -> dict[str, Any]:
        del progress_sink
        return self._record("release_gate", command)

    def sonar_export_issues(
        self, command: object, *, progress_sink: object | None = None
    ) -> dict[str, Any]:
        del progress_sink
        return self._record("sonar_export_issues", command)

    def sonar_ensure_rules(
        self, command: object, *, progress_sink: object | None = None
    ) -> dict[str, Any]:
        del progress_sink
        return self._record("sonar_ensure_rules", command)

    def sonar_generate_backlog(
        self, command: object, *, progress_sink: object | None = None
    ) -> dict[str, Any]:
        del progress_sink
        return self._record("sonar_generate_backlog", command)


@dataclass
class _Runtime:
    quality_tooling_service: _QualityTooling = field(default_factory=_QualityTooling)
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
    localization_tooling_service: object = object()


def _app() -> tuple[Any, _Runtime]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    runtime = _Runtime()
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def test_quality_commands_delegate_to_typed_tooling_service(monkeypatch: Any) -> None:
    _interactive_terminal(monkeypatch)
    app, runtime = _app()
    runner = CliRunner()

    cases = [
        (
            [
                "quality",
                "verify",
                "--tier",
                "3",
                "--evidence-mode",
                "report",
                "--plain",
            ],
            "verify",
            {"tier": 3, "evidence_mode": "report"},
            "y\n",
        ),
        (
            ["quality", "lint", "--mode", "fast", "--checks", "ruff", "--plain"],
            "lint",
            {"mode": "fast", "checks": "ruff"},
            "y\n",
        ),
        (["quality", "check-ci-excludes", "--plain"], "check_ci_excludes", {}, ""),
        (
            ["quality", "check-naming", "--base", "origin/main", "--plain"],
            "check_naming",
            {"base": "origin/main"},
            "",
        ),
        (["quality", "release-gate", "--plain"], "release_gate", {}, "y\n"),
        (
            ["quality", "sonar-export-issues", "--project-key", "fs", "--plain"],
            "sonar_export_issues",
            {"project_key": "fs"},
            "y\n",
        ),
        (
            ["quality", "sonar-ensure-rules", "--rules", "py:S1", "--plain"],
            "sonar_ensure_rules",
            {"rules": "py:S1"},
            "y\n",
        ),
        (
            ["quality", "sonar-generate-backlog", "--output", "tmp/backlog.md", "--plain"],
            "sonar_generate_backlog",
            {"output": "tmp/backlog.md"},
            "y\n",
        ),
    ]

    for args, method, expected_subset, stdin in cases:
        result = runner.invoke(app, args, input=stdin)
        assert result.exit_code == 0, (args, result.output)
        name, payload = runtime.quality_tooling_service.calls[-1]
        assert name == method
        for key, value in expected_subset.items():
            assert payload[key] == value


def test_check_ci_excludes_help_names_the_metadata_file_option() -> None:
    app, runtime = _app()
    runner = CliRunner()

    help_text = runner.invoke(app, ["quality", "check-ci-excludes", "--help"]).output
    renamed = runner.invoke(
        app,
        [
            "quality",
            "check-ci-excludes",
            "--exclude-file",
            "tests/CI_EXCLUDES.txt",
            "--plain",
        ],
    )
    obsolete = runner.invoke(
        app,
        ["quality", "check-ci-excludes", "--path", "core/application", "--plain"],
    )

    assert "--exclude-file" in help_text
    assert "CI_EXCLUDES metadata file" in help_text
    optional_inputs = help_text.split("Advanced inputs:", 1)[0]
    assert "tests/CI_EXCLUDES.txt." in optional_inputs
    assert "Examples: core/application" not in help_text
    assert "--path" not in help_text
    assert renamed.exit_code == 0, renamed.output
    assert runtime.quality_tooling_service.calls[-1][1]["path"] == "tests/CI_EXCLUDES.txt"
    assert obsolete.exit_code == 2


def test_sonar_token_uses_secret_source_and_is_not_plain_argument(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    _interactive_terminal(monkeypatch)
    app, runtime = _app()
    secret_root = tmp_path / ".flow-steward" / "secret-input"
    secret_root.mkdir(parents=True)
    token = secret_root / "sonar"
    token.write_text("super-secret-token\n", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setattr(click_adapter, "DEFAULT_SECRET_FILE_ROOT", secret_root)

    result = CliRunner().invoke(
        app,
        [
            "quality",
            "sonar-export-issues",
            "--sonar-token-file",
            str(token),
            "--confirm",
            "--json",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert runtime.quality_tooling_service.calls[-1][1]["sonar_token"] == "super-secret-token"
    assert "super-secret-token" not in result.output
    help_text = CliRunner().invoke(app, ["quality", "sonar-export-issues", "--help"]).output
    assert "--sonar-token " not in help_text
    assert "--sonar-token-file" in help_text


def test_failed_mutating_quality_command_exits_recovery_required(monkeypatch: Any) -> None:
    _interactive_terminal(monkeypatch)
    app, runtime = _app()
    runtime.quality_tooling_service.lint = lambda _command, *, progress_sink=None: {
        **_result("quality.lint", {}),
        "status": "failed",
        "exit_code": 17,
        "stderr": "partial effects possible",
    }

    result = CliRunner().invoke(
        app,
        ["quality", "lint", "--json", "--confirm"],
    )

    assert result.exit_code == 9


def _result(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": operation,
        "status": "completed",
        "exit_code": 0,
        "command": [],
        "stdout": "",
        "stderr": "",
        "artifacts": [],
        "details": {key: value for key, value in payload.items() if "token" not in key},
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
