from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.application.installer.spec import INSTALLER_SPEC
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.lifecycle.environment_service import (
    EnvironmentResult,
    EnvironmentShowRequest,
    EnvironmentSwitchRequest,
)
from core.application.management.command_specs.contracts import (
    InteractivePolicy,
    OperationProgressEvent,
    OperationStepStatus,
    OperationType,
)
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.modes import TerminalFacts
from core.entrypoints.cli.platform.service_bindings.common import PreparedCommand
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@dataclass
class _EnvironmentService:
    calls: list[tuple[str, object]] = field(default_factory=list)

    def show(self, request: EnvironmentShowRequest) -> EnvironmentResult:
        self.calls.append(("show", request))
        return EnvironmentResult(
            status="active",
            active=DeploymentConfig(environment="development"),
            active_environment="development",
            source_release="4.8.2",
            target_release="4.8.2",
        )

    def switch(self, request: EnvironmentSwitchRequest, **kwargs: object) -> EnvironmentResult:
        self.calls.append(("switch", request))
        progress = kwargs.get("progress_sink")
        if callable(progress):
            progress(
                OperationProgressEvent(
                    step_id="verify_health",
                    status=OperationStepStatus.SUCCEEDED,
                    label="Verify target runtime health",
                )
            )
        active = DeploymentConfig(
            environment=request.target,
            development_tools=request.development_tools if request.target == "development" else (),
        )
        return EnvironmentResult(
            status="active",
            active=active,
            active_environment=request.target,
            source_environment="development",
            operation_id="environment-operation",
            journal_path="/tmp/environment-operation.json",
            log_path="/tmp/environment-operation.log",
            source_release="4.8.2",
            target_release="4.8.2",
            preservation_verified=True,
        )

    def plan_switch(self, request: EnvironmentSwitchRequest) -> dict[str, object]:
        self.calls.append(("plan", request))
        changing_storage = request.file_storage != "keep"
        return {
            "transition": f"development -> {request.target}",
            "release": "4.8.2 (unchanged)",
            "development_tools": list(request.development_tools),
            "storage_decision": request.file_storage,
            "backup_gate": "required before config/runtime mutation",
            "rollback": "exact previous configuration and runtime",
            "source_storage_inventory": (
                {
                    "approximate_count": 4,
                    "approximate_size_bytes": 128,
                    "source_identity": "volume:installed-project_seaweedfs-data",
                    "completeness": "complete",
                }
                if changing_storage
                else None
            ),
            "artifact_decision_required": changing_storage,
        }


@dataclass
class _Runtime:
    environment_service: _EnvironmentService = field(default_factory=_EnvironmentService)


def _app() -> tuple[Any, _Runtime]:
    runtime = _Runtime()
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def test_environment_specs_are_exact_closed_and_dependency_aware() -> None:
    specs = {
        spec.command_id: spec for spec in PUBLIC_COMMAND_SPECS if spec.path[0] == "environment"
    }

    assert tuple(specs) == ("environment.show", "environment.switch")
    assert specs["environment.show"].operation_type is OperationType.READ
    switch = specs["environment.switch"]
    assert switch.operation_type is OperationType.CHANGE
    assert switch.interactive_policy is InteractivePolicy.ALWAYS_REVIEW
    assert switch.lifecycle_interrupts is True
    assert switch.plan_builder_ref == "environment.switch.preview"
    assert tuple(field.key for field in switch.fields) == (
        "target",
        "file_storage",
        "s3_endpoint",
        "s3_bucket",
        "s3_region",
        "s3_credentials_file",
        "allow_insecure_s3_http",
        "artifact_decision",
    )
    fields = {field.key: field for field in switch.fields}
    assert fields["target"].positional is True
    assert fields["s3_credentials_file"].allow_secret_file is True
    assert fields["s3_credentials_file"].visible_if == {"file_storage": "external_s3"}
    assert fields["artifact_decision"].visible_if == {}
    assert fields["artifact_decision"].deferred_until_plan is True
    assert not any(field.secret for field in switch.fields)


def test_environment_help_show_plain_json_and_no_secret_arguments() -> None:
    app, runtime = _app()
    runner = CliRunner()

    group = runner.invoke(app, ["environment", "--help"])
    switch_help = runner.invoke(app, ["environment", "switch", "--help"])
    plain = runner.invoke(app, ["environment", "show", "--plain"])
    machine = runner.invoke(app, ["environment", "show", "--json"])

    assert group.exit_code == 0, group.output
    assert "show" in group.output
    assert "switch" in group.output
    assert switch_help.exit_code == 0, switch_help.output
    assert "development|production" in switch_help.output
    assert "--development-tools" not in switch_help.output
    assert "--file-storage" in switch_help.output
    assert "--s3-credentials-file" in switch_help.output
    assert "--s3-access-key" not in switch_help.output
    assert "--s3-secret-key" not in switch_help.output
    assert plain.exit_code == 0, plain.output
    assert "development" in plain.output
    assert json.loads(machine.output)["result"]["active_environment"] == "development"
    assert [call[0] for call in runtime.environment_service.calls] == ["show", "show"]


def test_switch_requires_review_and_supports_plain_json_noninteractive_progress() -> None:
    app, runtime = _app()
    runner = CliRunner()

    refused = runner.invoke(
        app,
        ["environment", "switch", "production", "--non-interactive", "--plain"],
    )
    plain = runner.invoke(
        app,
        [
            "environment",
            "switch",
            "production",
            "--file-storage",
            "keep",
            "--non-interactive",
            "--confirm",
            "--plain",
        ],
    )
    machine = runner.invoke(
        app,
        [
            "environment",
            "switch",
            "development",
            "--non-interactive",
            "--confirm",
            "--json",
        ],
    )

    assert refused.exit_code == 10
    assert plain.exit_code == 0, plain.output
    assert "Impact plan for environment.switch" in plain.output
    assert "4.8.2 (unchanged)" in plain.output
    assert "Verify target runtime health" in plain.output
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.output)
    assert payload["command"] == "environment.switch"
    assert payload["operation_id"] == "environment-operation"
    assert payload["log_path"] == "/tmp/environment-operation.log"
    request = runtime.environment_service.calls[-1][1]
    assert request.development_tools == ()


def test_environment_and_installer_expose_no_development_tool_selector() -> None:
    public_fields = {
        field.key
        for spec in PUBLIC_COMMAND_SPECS
        if spec.command_id.startswith("environment.")
        for field in spec.fields
    }
    assert "development_tools" not in public_fields
    assert all(field.key != "development_tools" for field in INSTALLER_SPEC.fields)

    app, _runtime = _app()
    result = CliRunner().invoke(
        app,
        [
            "environment",
            "switch",
            "development",
            "--development-tools",
            "test_runner",
            "--non-interactive",
            "--confirm",
        ],
    )
    assert result.exit_code == 2


def test_switch_preview_preserves_typed_execution_input() -> None:
    import asyncio

    runtime = _Runtime()
    registry = ServiceRegistry(runtime)
    request = EnvironmentSwitchRequest(target="production")

    prepared = asyncio.run(registry.build_plan("environment.switch.preview", request))

    assert isinstance(prepared, PreparedCommand)
    assert prepared.execution_input is request
    assert prepared.review["release"] == "4.8.2 (unchanged)"


def test_switch_preview_defers_required_decision_until_inventory_is_visible() -> None:
    import asyncio

    runtime = _Runtime()
    registry = ServiceRegistry(runtime)
    request = EnvironmentSwitchRequest(
        target="production",
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="artifacts",
        s3_region="eu-test-1",
        s3_credentials_file="/safe/candidate.json",
    )

    prepared = asyncio.run(registry.build_plan("environment.switch.preview", request))

    assert prepared.review["source_storage_inventory"]["approximate_count"] == 4
    assert prepared.deferred_fields == ("artifact_decision",)


def test_interactive_decision_prompt_follows_rendered_source_inventory(
    monkeypatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    monkeypatch.setattr(
        click_adapter,
        "_terminal_facts",
        lambda: TerminalFacts(
            stdin_isatty=True,
            stdout_isatty=True,
            columns=80,
            rows=24,
            term="xterm-256color",
            no_color_env=False,
        ),
    )
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        [
            "environment",
            "switch",
            "production",
            "--file-storage",
            "external_s3",
            "--s3-endpoint",
            "https://objects.example.test",
            "--s3-bucket",
            "artifacts",
            "--s3-region",
            "eu-test-1",
            "--s3-credentials-file",
            "/safe/candidate.json",
            "--plain",
        ],
        input="2\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert result.output.index('"source_storage_inventory"') < result.output.index(
        "Existing artifact decision"
    )
    request = runtime.environment_service.calls[-1][1]
    assert request.artifact_decision == "accept_unavailable"


def test_noninteractive_external_storage_aggregates_missing_conditional_inputs() -> None:
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        [
            "environment",
            "switch",
            "production",
            "--file-storage",
            "external_s3",
            "--non-interactive",
            "--confirm",
            "--json",
        ],
    )

    assert result.exit_code == 3
    payload = json.loads(result.output)
    fields = {issue["field"] for issue in payload["error"]["field_issues"]}
    assert {"s3_endpoint", "s3_bucket", "s3_region", "s3_credentials_file"} <= fields
    assert runtime.environment_service.calls == []


def test_noninteractive_missing_artifact_decision_returns_reviewed_inventory() -> None:
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        [
            "environment",
            "switch",
            "production",
            "--file-storage",
            "external_s3",
            "--s3-endpoint",
            "https://objects.example.test",
            "--s3-bucket",
            "artifacts",
            "--s3-region",
            "eu-test-1",
            "--s3-credentials-file",
            "/safe/candidate.json",
            "--non-interactive",
            "--confirm",
            "--json",
        ],
    )

    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["error"]["details"]["source_storage_inventory"] == {
        "approximate_count": 4,
        "approximate_size_bytes": 128,
        "source_identity": "volume:installed-project_seaweedfs-data",
        "completeness": "complete",
    }
    assert [call[0] for call in runtime.environment_service.calls] == ["plan"]
