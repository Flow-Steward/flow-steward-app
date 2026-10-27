from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from click.testing import CliRunner

from core.application.lifecycle.backup_service import (
    BackupCreateRequest,
    BackupListRequest,
    BackupListResult,
    BackupResult,
    BackupService,
    BackupVerificationStatus,
    BackupVerifyRequest,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.management.command_specs.contracts import (
    ExitCode,
    FieldType,
    InteractivePolicy,
    OperationProgressEvent,
    OperationStepStatus,
    OperationType,
)
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_registry import ServiceRegistry
from core.entrypoints.cli.runtime import CLIRuntime

_NOW = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)
_BACKUP_PATH = "backups/flow-steward-20260725T060000Z.dump.enc"


def _backup_result(*, verified: bool = True) -> BackupResult:
    return BackupResult(
        path=_BACKUP_PATH,
        size_bytes=4096,
        created_at=_NOW,
        verification_status=(
            BackupVerificationStatus.VERIFIED if verified else BackupVerificationStatus.UNVERIFIED
        ),
        verified_at=_NOW if verified else None,
        log_path="logs/runtime/backup.log",
    )


@dataclass
class _BackupService:
    calls: list[tuple[str, object, dict[str, object]]] = field(default_factory=list)
    fail: bool = False

    def create(self, request: BackupCreateRequest, **kwargs: object) -> BackupResult:
        self.calls.append(("create", request, kwargs))
        self._emit_progress(kwargs, step_id="publish", label="Publish protected backup")
        self._raise_if_requested()
        return _backup_result()

    def list(self, request: BackupListRequest) -> BackupListResult:
        self.calls.append(("list", request, {}))
        self._raise_if_requested()
        return BackupListResult(backups=(_backup_result(),))

    def verify(self, request: BackupVerifyRequest, **kwargs: object) -> BackupResult:
        self.calls.append(("verify", request, kwargs))
        self._emit_progress(kwargs, step_id="inspect", label="Inspect protected backup")
        self._raise_if_requested()
        return _backup_result()

    @staticmethod
    def _emit_progress(
        kwargs: dict[str, object],
        *,
        step_id: str,
        label: str,
    ) -> None:
        progress_sink = kwargs.get("progress_sink")
        if callable(progress_sink):
            progress_sink(
                OperationProgressEvent(
                    step_id=step_id,
                    status=OperationStepStatus.SUCCEEDED,
                    label=label,
                )
            )

    def _raise_if_requested(self) -> None:
        if self.fail:
            raise LifecycleError(
                code="backup_dependency_unavailable",
                summary="The protected PostgreSQL backup operation failed.",
                cause="The backup dependency is unavailable.",
                remediation="Check the backup dependency and retry.",
                retryable=True,
                step="backup",
                exit_code=ExitCode.DEPENDENCY,
            )


@dataclass
class _Runtime:
    backup_command_service: _BackupService = field(default_factory=_BackupService)


def _app() -> tuple[Any, _Runtime]:
    runtime = _Runtime()
    return build_click_app(ServiceRegistry(cast(Any, runtime)), SimpleNamespace()), runtime


def test_backup_specs_are_exact_closed_and_immediate() -> None:
    specs = {spec.command_id: spec for spec in PUBLIC_COMMAND_SPECS if spec.path[0] == "backup"}

    assert tuple(specs) == ("backup.create", "backup.list", "backup.verify")
    create = specs["backup.create"]
    assert create.operation_type is OperationType.CHANGE
    assert create.interactive_policy is InteractivePolicy.IMMEDIATE
    assert create.fields == ()
    assert create.input_model is BackupCreateRequest
    assert create.result_model is BackupResult
    assert create.plan_builder_ref is None
    assert create.lifecycle_interrupts is True

    list_spec = specs["backup.list"]
    assert list_spec.operation_type is OperationType.READ
    assert list_spec.interactive_policy is InteractivePolicy.IMMEDIATE
    assert list_spec.fields == ()
    assert list_spec.input_model is BackupListRequest
    assert list_spec.result_model is BackupListResult

    verify = specs["backup.verify"]
    assert verify.operation_type is OperationType.READ
    assert verify.interactive_policy is InteractivePolicy.IMMEDIATE
    assert tuple(field.key for field in verify.fields) == ("backup",)
    backup_field = verify.fields[0]
    assert backup_field.type is FieldType.PATH
    assert backup_field.required is True
    assert backup_field.positional is True
    assert backup_field.option_name is None
    assert verify.input_model is BackupVerifyRequest
    assert verify.result_model is BackupResult
    assert verify.plan_builder_ref is None


def test_backup_help_exposes_only_the_closed_public_inputs() -> None:
    app, _runtime = _app()
    runner = CliRunner()

    group = runner.invoke(app, ["backup", "--help"])
    create = runner.invoke(app, ["backup", "create", "--help"])
    list_result = runner.invoke(app, ["backup", "list", "--help"])
    verify = runner.invoke(app, ["backup", "verify", "--help"])
    restore = runner.invoke(app, ["backup", "restore", "--help"])

    assert group.exit_code == 0, group.output
    assert all(leaf in group.output for leaf in ("create", "list", "verify"))
    assert "restore" not in group.output
    assert create.exit_code == 0, create.output
    assert list_result.exit_code == 0, list_result.output
    assert verify.exit_code == 0, verify.output
    assert "BACKUP" in verify.output
    assert "backups/" in verify.output
    assert "--operation-id" not in create.output
    assert "--output" not in create.output
    assert "--backup" not in verify.output
    assert restore.exit_code == 2


def test_backup_create_is_immediate_noninteractive_and_reports_progress() -> None:
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        ["backup", "create", "--non-interactive", "--plain"],
    )

    assert result.exit_code == 0, result.output
    assert "Publish protected backup" in result.output
    assert _BACKUP_PATH in result.output
    action, request, kwargs = runtime.backup_command_service.calls[-1]
    assert action == "create"
    assert isinstance(request, BackupCreateRequest)
    assert request.operation_id is None
    assert kwargs.get("cancellation") is not None


def test_backup_list_supports_human_and_json_output_without_progress() -> None:
    app, runtime = _app()
    runner = CliRunner()

    human = runner.invoke(app, ["backup", "list"])
    machine = runner.invoke(app, ["backup", "list", "--json"])

    assert human.exit_code == 0, human.output
    assert _BACKUP_PATH in human.output
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.output)
    assert payload["command"] == "backup.list"
    assert payload["result"]["backups"][0]["path"] == _BACKUP_PATH
    assert [call[0] for call in runtime.backup_command_service.calls] == ["list", "list"]


def test_backup_verify_requires_a_path_and_supports_plain_progress() -> None:
    app, runtime = _app()
    runner = CliRunner()

    missing = runner.invoke(
        app,
        ["backup", "verify", "--non-interactive", "--plain"],
    )
    result = runner.invoke(
        app,
        ["backup", "verify", _BACKUP_PATH, "--non-interactive", "--plain"],
    )

    assert missing.exit_code == 3, missing.output
    assert "backup" in missing.output.lower()
    assert result.exit_code == 0, result.output
    assert "Inspect protected backup" in result.output
    action, request, kwargs = runtime.backup_command_service.calls[-1]
    assert action == "verify"
    assert isinstance(request, BackupVerifyRequest)
    assert request.backup == _BACKUP_PATH
    assert callable(kwargs.get("progress_sink"))


def test_backup_lifecycle_errors_keep_stable_json_exit_contract() -> None:
    app, runtime = _app()
    runtime.backup_command_service.fail = True

    result = CliRunner().invoke(app, ["backup", "list", "--json"])

    assert result.exit_code == ExitCode.DEPENDENCY
    payload = json.loads(result.output)
    assert payload["command"] == "backup.list"
    assert payload["error"]["code"] == "backup_dependency_unavailable"
    assert "dependency is unavailable" in payload["error"]["message"]


def test_cli_runtime_splits_public_command_service_from_internal_backup_port() -> None:
    runtime = CLIRuntime()
    service = runtime.backup_command_service

    assert callable(service.create)
    assert callable(service.list)
    assert callable(service.verify)
    assert service is not runtime.backup_service
    assert BackupService.__module__ == "core.application.lifecycle.backup_service"
