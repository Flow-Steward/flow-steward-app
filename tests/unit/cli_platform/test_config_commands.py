from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.application.lifecycle.configuration_service import (
    ConfigApplyCommandResult,
    ConfigApplyRequest,
    ConfigSecretState,
    ConfigSetRequest,
    ConfigSetResult,
    ConfigShowRequest,
    ConfigShowResult,
    ConfigValidateRequest,
)
from core.application.lifecycle.contracts import (
    ConfigurationPatch,
    ConfigurationValidationResult,
    DeploymentConfig,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.management.command_specs.contracts import (
    InteractivePolicy,
    OperationProgressEvent,
    OperationStepStatus,
    OperationType,
)
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@dataclass
class _ConfigurationService:
    calls: list[tuple[str, object]] = field(default_factory=list)
    fail_apply: bool = False
    nothing_to_apply: bool = False
    validation_result: ConfigurationValidationResult = field(
        default_factory=lambda: ConfigurationValidationResult(valid=True)
    )

    def show(self, request: ConfigShowRequest) -> ConfigShowResult:
        self.calls.append(("show", request))
        return ConfigShowResult(
            active=DeploymentConfig(public_host="flow.example.test"),
            pending=ConfigurationPatch(public_port=443),
            pending_diff={"public_port": {"active": 8099, "pending": 443}},
            protected_setting_states=(
                ConfigSecretState(name="FS_SECRET_KEY", state="configured"),
                ConfigSecretState(name="FS_BACKUP_ENCRYPTION_KEY", state="missing"),
            ),
        )

    def validate(self, request: ConfigValidateRequest) -> ConfigurationValidationResult:
        self.calls.append(("validate", request))
        return self.validation_result

    def set(self, request: ConfigSetRequest) -> ConfigSetResult:
        self.calls.append(("set", request))
        return ConfigSetResult(pending=request)

    def apply(self, request: ConfigApplyRequest, **kwargs: object) -> ConfigApplyCommandResult:
        self.calls.append(("apply", request))
        progress = kwargs.get("progress_sink")
        if callable(progress):
            progress(
                OperationProgressEvent(
                    step_id="verify_health",
                    status=OperationStepStatus.SUCCEEDED,
                    label="Verify runtime health",
                )
            )
        if self.fail_apply:
            raise LifecycleError.stable_failure(
                code="configuration_apply_failed",
                cause="The target runtime failed; the previous runtime was restored.",
                remediation="Correct the pending configuration and retry.",
                step="recovery",
            )
        return ConfigApplyCommandResult(
            active=DeploymentConfig(public_host="flow.example.test", public_port=443),
            operation_id="config-operation",
            journal_path="/tmp/config-operation.json",
            log_path="/tmp/config-operation.log",
            preservation_verified=True,
        )

    def plan_apply(self, request: ConfigApplyRequest) -> dict[str, object]:
        self.calls.append(("plan-apply", request))
        if self.nothing_to_apply:
            raise LifecycleError.stable_failure(
                code="nothing_to_apply",
                cause="There is no pending configuration patch to apply.",
                remediation="Run flow-steward config set first.",
                step="configuration",
            )
        return {
            "pending_fields": ["public_port"],
            "runtime_change": True,
            "preservation": "required",
            "rollback": "exact previous configuration and runtime",
        }


@dataclass
class _Runtime:
    configuration_service: _ConfigurationService = field(default_factory=_ConfigurationService)


def _app() -> tuple[Any, _Runtime]:
    runtime = _Runtime()
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def test_config_specs_register_exact_four_leaves_and_closed_fields() -> None:
    specs = {spec.command_id: spec for spec in PUBLIC_COMMAND_SPECS if spec.path[0] == "config"}

    assert tuple(specs) == (
        "config.show",
        "config.validate",
        "config.set",
        "config.apply",
    )
    assert specs["config.show"].operation_type is OperationType.READ
    assert specs["config.validate"].operation_type is OperationType.READ
    assert specs["config.set"].operation_type is OperationType.CHANGE
    assert specs["config.set"].interactive_policy is InteractivePolicy.WIZARD_WHEN_INCOMPLETE
    assert tuple(field.key for field in specs["config.set"].fields) == (
        "deployment_profile",
        "runtime_http_port",
        "worker_processes",
        # Review P2: Scalable's worker containers, beside Compact's processes.
        "worker_replicas",
        "file_storage",
        "ingress_mode",
        "public_host",
        "public_port",
        "tls_mode",
        "reverse_proxy_backend_host",
        "reverse_proxy_backend_port",
        "reverse_proxy_mcp_port",
        "proxy_placement",
        "proxy_kind",
        "proxy_trusted_ips",
        "traefik_network",
        "traefik_https_entrypoint",
        "traefik_http_entrypoint",
        "traefik_cert_resolver",
        "proxy_network",
        "acme_contact",
        "acme_directory_url",
        "extension_installer_origin",
        "certificate_path",
        "private_key_path",
    )
    assert specs["config.apply"].fields == ()
    assert specs["config.apply"].lifecycle_interrupts is True
    assert not any(field.secret for spec in specs.values() for field in spec.fields)


def test_config_help_is_generated_and_has_no_generic_or_secret_setter() -> None:
    app, _runtime = _app()
    runner = CliRunner()

    group = runner.invoke(app, ["config", "--help"])
    set_help = runner.invoke(app, ["config", "set", "--help"])

    assert group.exit_code == 0, group.output
    assert all(leaf in group.output for leaf in ("show", "validate", "set", "apply"))
    assert set_help.exit_code == 0, set_help.output
    assert "--public-host" in set_help.output
    assert "--tls-mode" in set_help.output
    assert "--key" not in set_help.output
    assert "--value" not in set_help.output
    assert "secret" not in set_help.output.lower()


def test_config_set_rejects_arbitrary_environment_and_secret_inputs() -> None:
    app, runtime = _app()
    runner = CliRunner()

    results = (
        runner.invoke(app, ["config", "set", "--environment", "production"]),
        runner.invoke(app, ["config", "set", "--fs-secret-key", "secret-value"]),
        runner.invoke(app, ["config", "set", "--key", "public_host", "--value", "unsafe"]),
    )

    assert all(result.exit_code == 2 for result in results)
    assert runtime.configuration_service.calls == []
    assert all("secret-value" not in result.output for result in results)


def test_show_plain_and_json_include_pending_diff_and_only_secret_states() -> None:
    app, runtime = _app()
    runner = CliRunner()

    plain = runner.invoke(app, ["config", "show", "--plain"])
    machine = runner.invoke(app, ["config", "show", "--json"])

    assert plain.exit_code == 0, plain.output
    assert "flow.example.test" in plain.output
    assert "configured" in plain.output
    assert "missing" in plain.output
    payload = json.loads(machine.output)
    assert payload["result"]["active"]["public_host"] == "flow.example.test"
    assert payload["result"]["pending_diff"]["public_port"] == {
        "active": 8099,
        "pending": 443,
    }
    assert payload["result"]["protected_setting_states"] == [
        {"name": "FS_SECRET_KEY", "state": "configured"},
        {"name": "FS_BACKUP_ENCRYPTION_KEY", "state": "missing"},
    ]
    output = plain.output + machine.output
    assert "secret-value" not in output
    assert [call[0] for call in runtime.configuration_service.calls] == ["show", "show"]


def test_validate_is_immediate_and_set_uses_only_typed_declarative_fields() -> None:
    app, runtime = _app()
    runner = CliRunner()

    validated = runner.invoke(app, ["config", "validate", "--non-interactive", "--json"])
    staged = runner.invoke(
        app,
        [
            "config",
            "set",
            "--deployment-profile",
            "compact",
            "--file-storage",
            "external_s3",
            "--public-host",
            "new.example.test",
            "--public-port",
            "443",
            "--plain",
        ],
    )

    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.output)["result"]["valid"] is True
    assert staged.exit_code == 0, staged.output
    request = runtime.configuration_service.calls[-1][1]
    assert isinstance(request, ConfigSetRequest)
    assert request.model_dump(exclude_none=True) == {
        "deployment_profile": "compact",
        "file_storage": "external_s3",
        "public_host": "new.example.test",
        "public_port": 443,
    }


def test_validate_invalid_configuration_returns_failure_envelope_and_exit_code() -> None:
    app, runtime = _app()
    runtime.configuration_service.validation_result = ConfigurationValidationResult(
        valid=False,
        issues=("active: public_host must be a valid DNS name",),
    )
    runner = CliRunner()

    result = runner.invoke(app, ["config", "validate", "--non-interactive", "--json"])

    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "configuration_invalid"
    assert payload["error"]["details"]["issues"] == ["active: public_host must be a valid DNS name"]


def test_apply_requires_review_emits_progress_and_preserves_lifecycle_metadata() -> None:
    app, runtime = _app()
    runner = CliRunner()

    refused = runner.invoke(app, ["config", "apply", "--non-interactive", "--plain"])
    applied = runner.invoke(
        app,
        ["config", "apply", "--non-interactive", "--confirm", "--plain"],
    )

    assert refused.exit_code == 10
    assert applied.exit_code == 0, applied.output
    assert "Impact plan for config.apply" in applied.output
    assert "exact previous configuration and runtime" in applied.output
    assert "Verify runtime health" in applied.output
    assert [call[0] for call in runtime.configuration_service.calls] == [
        "plan-apply",
        "plan-apply",
        "apply",
    ]

    runtime.configuration_service.fail_apply = True
    failed = runner.invoke(
        app,
        ["config", "apply", "--non-interactive", "--confirm", "--json"],
    )
    assert failed.exit_code == 8
    assert json.loads(failed.output)["error"]["code"] == "configuration_apply_failed"


def test_apply_empty_state_preserves_nothing_to_apply_from_review_plan() -> None:
    app, runtime = _app()
    runtime.configuration_service.nothing_to_apply = True
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            app,
            ["config", "apply", "--non-interactive", "--confirm", "--json"],
        )
        payload = json.loads(result.output)
        log_text = Path(payload["log_path"]).read_text(encoding="utf-8")

    assert result.exit_code == 8, result.output
    assert payload["error"]["code"] == "nothing_to_apply"
    assert payload["error"]["message"] == "There is no pending configuration patch to apply."
    assert payload["error"]["hint"] == "Run flow-steward config set first."
    assert payload["operation_id"]
    assert "PLAN_FAILED nothing_to_apply" in log_text
    assert [call[0] for call in runtime.configuration_service.calls] == ["plan-apply"]
