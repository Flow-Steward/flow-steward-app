"""Tests for renderer-neutral execution and deterministic CLI output."""

from __future__ import annotations

import json

import pytest

from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CLIErrorDetail,
    ExecutionResult,
    ExitCode,
    FieldSpec,
    FieldType,
    OperationProgressEvent,
    OperationStepStatus,
    OperationType,
)
from core.application.management.command_specs.database import DATABASE_COMMAND_SPECS
from core.application.management.command_specs.quality import QUALITY_COMMAND_SPECS
from core.application.management.command_specs.users import USERS_CREATE_SPEC, USERS_LIST_SPEC
from core.application.tooling.contracts import ToolRunResult


class _Registry:
    async def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> object:
        del value, progress_sink, technical_sink
        assert key == "users.list"
        return {"users": [], "total": 0}


class _FailingSecretRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> object:
        del key, value, progress_sink, technical_sink
        raise RuntimeError("remote rejected password=open-sesame")


class _ProgressRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> object:
        del key, value, technical_sink
        assert callable(progress_sink)
        progress_sink(
            OperationProgressEvent(
                step_id="index",
                status=OperationStepStatus.RUNNING,
                label="Indexing project",
                completed=2,
                total=4,
            )
        )
        return {"users": [], "total": 0}


class _DiagnosticSuccessRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> object:
        del key, value, progress_sink
        assert callable(technical_sink)
        technical_sink("diagnostic only")
        return {"users": [], "total": 0}


class _FailedToolRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> object:
        del value, progress_sink, technical_sink
        assert key == "quality.lint"
        return ToolRunResult(
            operation="quality.lint",
            status="failed",
            exit_code=17,
            stdout="token=ghp_abcdefghijklmnopqrstuvwxyz",
            stderr="lint failed",
        )


class _SecretStdoutRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> object:
        del value, progress_sink, technical_sink
        assert key == "quality.sonar-export-issues"
        return ToolRunResult(
            operation="quality.sonar-export-issues",
            status="completed",
            exit_code=0,
            stdout="uploaded with token super-secret-token",
            stderr="",
        )


class _CompletedToolRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> object:
        del value, progress_sink, technical_sink
        assert key == "quality.lint"
        return ToolRunResult(
            operation="quality.lint",
            status="completed",
            exit_code=0,
            stdout="[red]literal stdout[/red]",
            stderr="literal stderr",
        )


class _CompletedToolWithFullLogRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> object:
        del value, progress_sink, technical_sink
        assert key == "quality.lint"
        return ToolRunResult(
            operation="quality.lint",
            status="completed",
            exit_code=0,
            stdout="BEGIN\n[output truncated; omitted 240 characters]\nEND",
            stderr="short stderr",
            stdout_full="BEGIN\n" + ("x" * 240) + "\nEND",
            stderr_full="short stderr",
        )


class _FailedDatabaseRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> object:
        del value, progress_sink, technical_sink
        assert key == "database.restore-parity-verify"
        return {
            "operation": "database.restore-parity-verify",
            "status": "failed",
            "changed": True,
            "message": "restore parity mismatch",
            "details": {"passed": False},
            "artifacts": [],
        }


class _StrictCallbackRegistry:
    def __init__(self) -> None:
        self.sinks: tuple[object | None, object | None] | None = None

    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None,
        technical_sink: object | None,
    ) -> object:
        assert key == "users.list"
        del value
        self.sinks = (progress_sink, technical_sink)
        return {"users": [], "total": 0}


def test_executor_validates_service_result_and_returns_structured_success() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    result = execute_command(
        spec=USERS_LIST_SPEC,
        command_input=USERS_LIST_SPEC.input_model(),
        services=_Registry(),
    )

    assert result.error is None
    assert result.result is not None


def test_executor_always_passes_exact_callback_keywords() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    registry = _StrictCallbackRegistry()
    result = execute_command(
        spec=USERS_LIST_SPEC,
        command_input=USERS_LIST_SPEC.input_model(),
        services=registry,
    )

    assert result.error is None
    assert registry.sinks == (None, None)


def test_executor_redacts_known_command_secret_before_error_and_technical_sinks() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    technical: list[str] = []
    result = execute_command(
        spec=USERS_CREATE_SPEC,
        command_input=USERS_CREATE_SPEC.input_model(
            email="operator@example.com", password="open-sesame", roles=["viewer"]
        ),
        services=_FailingSecretRegistry(),
        technical_sink=technical.append,
    )

    assert result.error is not None
    assert "open-sesame" not in result.error.message
    assert result.error.message == "An unexpected internal error occurred."
    assert result.exit_code is ExitCode.RECOVERY_REQUIRED
    assert all("open-sesame" not in message for message in technical)
    assert "[REDACTED]" in "\n".join(technical)


@pytest.mark.parametrize(
    ("operation_type", "expected_exit_code", "expected_retryable"),
    (
        (OperationType.READ, ExitCode.STABLE_FAILURE, True),
        (OperationType.CHANGE, ExitCode.RECOVERY_REQUIRED, False),
        (OperationType.DESTRUCTIVE, ExitCode.RECOVERY_REQUIRED, False),
    ),
)
def test_executor_maps_generic_service_exception_by_operation_type(
    operation_type: OperationType,
    expected_exit_code: ExitCode,
    expected_retryable: bool,
) -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    spec = USERS_LIST_SPEC.model_copy(update={"operation_type": operation_type})

    result = execute_command(
        spec=spec,
        command_input=spec.input_model(),
        services=_FailingSecretRegistry(),
    )

    assert result.error is not None
    assert result.error.code == "internal_error"
    assert result.error.retryable is expected_retryable
    assert result.exit_code is expected_exit_code


def test_executor_diagnostic_sink_failure_cannot_mask_mutation_failure() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    spec = USERS_LIST_SPEC.model_copy(update={"operation_type": OperationType.CHANGE})

    def failing_sink(_message: str) -> None:
        raise OSError("operation log is unavailable")

    result = execute_command(
        spec=spec,
        command_input=spec.input_model(),
        services=_FailingSecretRegistry(),
        technical_sink=failing_sink,
    )

    assert result.error is not None
    assert result.error.code == "internal_error"
    assert result.exit_code is ExitCode.RECOVERY_REQUIRED


def test_executor_diagnostic_sink_failure_cannot_turn_service_success_into_failure() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    def failing_sink(_message: str) -> None:
        raise OSError("operation log is unavailable")

    result = execute_command(
        spec=USERS_LIST_SPEC,
        command_input=USERS_LIST_SPEC.input_model(),
        services=_DiagnosticSuccessRegistry(),
        technical_sink=failing_sink,
    )

    assert result.error is None
    assert result.exit_code is ExitCode.SUCCESS


def test_executor_forwards_structured_progress_to_supported_service_registry() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    received: list[OperationProgressEvent] = []
    result = execute_command(
        spec=USERS_LIST_SPEC,
        command_input=USERS_LIST_SPEC.input_model(),
        services=_ProgressRegistry(),
        progress_sink=received.append,
    )

    assert result.error is None
    assert [(event.step_id, event.completed, event.total) for event in received] == [
        ("index", 2, 4)
    ]


def test_executor_maps_failed_tool_result_to_nonzero_cli_error() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    spec = next(item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.lint")
    result = execute_command(
        spec=spec,
        command_input=spec.input_model(),
        services=_FailedToolRegistry(),
    )

    assert result.result is None
    assert result.error is not None
    assert result.error.code == "tool_failed"
    assert result.exit_code is ExitCode.RECOVERY_REQUIRED
    assert "ghp_" not in result.error.message


def test_executor_maps_failed_database_operation_result_to_cli_error() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    spec = next(
        item
        for item in DATABASE_COMMAND_SPECS
        if item.command_id == "database.restore-parity-verify"
    )
    result = execute_command(
        spec=spec,
        command_input=spec.input_model(baseline="restore.json"),
        services=_FailedDatabaseRegistry(),
    )

    assert result.result is None
    assert result.error is not None
    assert result.error.code == "operation_failed"
    assert result.exit_code is ExitCode.RECOVERY_REQUIRED


def test_executor_keeps_nonzero_read_tool_result_as_stable_failure() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    spec = next(
        item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.check-ci-excludes"
    )

    class _FailedReadRegistry:
        def invoke(
            self,
            key: str,
            value: object,
            *,
            progress_sink: object | None = None,
            technical_sink: object | None = None,
        ) -> object:
            del value, progress_sink, technical_sink
            assert key == "quality.check-ci-excludes"
            return ToolRunResult(
                operation=key,
                status="failed",
                exit_code=17,
                stderr="check failed",
            )

    result = execute_command(
        spec=spec,
        command_input=spec.input_model(),
        services=_FailedReadRegistry(),
    )

    assert result.error is not None
    assert result.error.code == "tool_failed"
    assert result.exit_code is ExitCode.STABLE_FAILURE


def test_executor_redacts_success_payload_before_renderer_boundary() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    spec = next(
        item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.sonar-export-issues"
    )
    result = execute_command(
        spec=spec,
        command_input=spec.input_model(sonar_token="super-secret-token"),
        services=_SecretStdoutRegistry(),
    )

    assert result.error is None
    assert result.result is not None
    typed_payload = result.result.model_dump(mode="json")
    assert "super-secret-token" in typed_payload["stdout"]
    payload = result.public_result or {}
    assert "super-secret-token" not in str(payload)
    assert "[REDACTED]" in payload["stdout"]


def test_executor_preserves_typed_tool_failure_code_and_remediation() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    class _Registry:
        def invoke(self, key, value, **_kwargs):
            del key, value
            return ToolRunResult(
                operation="quality.sonar-export-issues",
                status="failed",
                exit_code=1,
                stderr="[sonar-export] Failed to reach SonarQube: refused",
                details={
                    "error_code": "sonar_unreachable",
                    "remediation": "Verify the Sonar URL and network reachability, then retry.",
                },
            )

    spec = next(
        item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.sonar-export-issues"
    )
    result = execute_command(
        spec=spec,
        command_input=spec.input_model(sonar_token="protected-token"),
        services=_Registry(),
    )

    assert result.error is not None
    assert result.error.code == "sonar_unreachable"
    assert result.error.hint == "Verify the Sonar URL and network reachability, then retry."


def test_renderer_registry_preserves_domain_specific_renderers() -> None:
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
    from core.entrypoints.cli.platform.renderers import RENDERERS, PlainRenderer, RichRenderer

    assert set(RENDERERS) == {spec.result_renderer_ref for spec in PUBLIC_COMMAND_SPECS}
    assert isinstance(RENDERERS["users.list"], RichRenderer)
    assert isinstance(RENDERERS["extensions.create"], RichRenderer)
    assert isinstance(RENDERERS["reports.generate"], RichRenderer)
    assert isinstance(RENDERERS["projects.dispatch-notification"], RichRenderer)
    assert "installer.run" not in RENDERERS
    assert isinstance(
        PlainRenderer(), PlainRenderer
    )  # keep the stable --plain renderer importable.
    for command_id in RENDERERS:
        if command_id.startswith(
            (
                "users.",
                "extensions.",
                "projects.",
                "workflows.",
                "tasks.",
                "jobs.",
                "reports.",
                "database.",
                "testing.",
                "quality.",
                "localization.",
            )
        ):
            assert isinstance(RENDERERS[command_id], RichRenderer), command_id


def test_renderer_for_falls_back_to_plain_when_rich_is_not_installed(monkeypatch) -> None:
    from core.application.management.command_specs.readiness import STATUS_SPEC
    from core.entrypoints.cli.platform import renderers

    monkeypatch.setattr(renderers, "find_spec", lambda package: None if package == "rich" else True)

    resolved = renderers.renderer_for(
        STATUS_SPEC,
        json_requested=False,
        plain_requested=False,
    )

    assert isinstance(resolved, renderers.PlainRenderer)
    assert not isinstance(resolved, renderers.RichRenderer)


def test_executor_logs_tool_stdout_and_stderr_to_technical_sink() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    spec = next(item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.lint")
    technical: list[str] = []

    result = execute_command(
        spec=spec,
        command_input=spec.input_model(),
        services=_CompletedToolRegistry(),
        technical_sink=technical.append,
    )

    assert result.error is None
    assert "stdout:" in technical
    assert "[red]literal stdout[/red]" in technical
    assert "stderr:" in technical
    assert "literal stderr" in technical


def test_executor_logs_full_tool_streams_to_operation_log_sink() -> None:
    from core.entrypoints.cli.platform.execution import execute_command

    spec = next(item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.lint")
    technical: list[str] = []

    result = execute_command(
        spec=spec,
        command_input=spec.input_model(),
        services=_CompletedToolWithFullLogRegistry(),
        technical_sink=technical.append,
    )

    assert result.error is None
    assert "[output truncated" not in "\n".join(technical)
    assert "BEGIN\n" + ("x" * 240) + "\nEND" in technical


def test_execution_result_carries_central_exit_code() -> None:
    result = ExecutionResult(
        error=CLIErrorDetail(code="permission_denied", message="Denied."),
        exit_code=ExitCode.PERMISSION,
    )

    assert result.exit_code is ExitCode.PERMISSION


def test_rich_progress_sink_emits_structured_stable_redirected_lines_and_logs(capsys) -> None:
    from core.entrypoints.cli.platform.renderers import RichRenderer

    logs: list[str] = []
    with RichRenderer().operation_progress(technical_sink=logs.append) as progress_sink:
        progress_sink(
            OperationProgressEvent(
                step_id="index",
                status=OperationStepStatus.RUNNING,
                label="Indexing project",
                detail="scanning",
                completed=2,
                total=4,
            )
        )

    assert "running: Indexing project (2/4): scanning" in capsys.readouterr().out
    assert logs == ["running: Indexing project (2/4): scanning"]


def test_rich_no_color_mode_disables_console_color_and_terminal_animation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import rich.console
    import rich.progress

    from core.entrypoints.cli.platform.renderers import RichRenderer

    captured: dict[str, object] = {}

    class FakeConsole:
        is_terminal = True

        def __init__(self, **kwargs: object) -> None:
            captured["console_kwargs"] = kwargs

    class FailingProgress:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("no-color mode must not start animated Rich progress")

    monkeypatch.setattr(rich.console, "Console", FakeConsole)
    monkeypatch.setattr(rich.progress, "Progress", FailingProgress)

    logs: list[str] = []
    with RichRenderer(color_enabled=False, animation_enabled=False).operation_progress(
        technical_sink=logs.append
    ) as progress_sink:
        progress_sink(
            OperationProgressEvent(
                step_id="index",
                status=OperationStepStatus.RUNNING,
                label="Indexing project",
                detail="scanning",
            )
        )

    assert captured["console_kwargs"] == {
        "color_system": None,
        "force_interactive": False,
    }
    assert "running: Indexing project: scanning" in capsys.readouterr().out
    assert logs == ["running: Indexing project: scanning"]


def test_animated_rich_progress_waits_for_real_event_before_task_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rich.console
    import rich.progress

    from core.entrypoints.cli.platform.renderers import RichRenderer

    added_tasks: list[str] = []

    class FakeConsole:
        is_terminal = True

        def __init__(self, **_kwargs: object) -> None:
            pass

    class FakeProgress:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def add_task(self, description: str, *, total: int | None = None) -> int:
            del total
            added_tasks.append(description)
            return len(added_tasks)

        def update(self, *_args: object, **_kwargs: object) -> None:
            pass

        def stop_task(self, *_args: object, **_kwargs: object) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(rich.console, "Console", FakeConsole)
    monkeypatch.setattr(rich.progress, "Progress", FakeProgress)

    with RichRenderer(color_enabled=True, animation_enabled=True).operation_progress() as sink:
        assert added_tasks == []
        sink(
            OperationProgressEvent(
                step_id="lint",
                status=OperationStepStatus.RUNNING,
                label="Linting",
            )
        )

    assert added_tasks == ["running: Linting"]


def test_json_renderer_emits_one_normalized_envelope_without_secret(capsys) -> None:
    from core.entrypoints.cli.platform.renderers import JSONRenderer

    renderer = JSONRenderer()
    renderer.render_result(
        USERS_LIST_SPEC,
        ExecutionResult(
            result=USERS_LIST_SPEC.result_model.model_validate({"users": [], "total": 0})
        ),
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "schema": "flow-steward.cli.v1",
        "ok": True,
        "command": "users.list",
        "result": {"users": [], "total": 0, "limit": 50, "offset": 0, "has_more": False},
        "warnings": [],
        "operation_id": None,
        "log_path": None,
    }


def test_rich_renderer_uses_semantic_color_for_success_and_error(capsys) -> None:
    from core.entrypoints.cli.platform.renderers import RichRenderer

    renderer = RichRenderer(color_enabled=True, animation_enabled=False)
    renderer.render_result(
        next(item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.lint"),
        ExecutionResult(
            result=ToolRunResult(
                operation="quality.lint",
                status="completed",
                exit_code=0,
                stdout="ok",
            ),
            public_result={
                "operation": "quality.lint",
                "status": "completed",
                "exit_code": 0,
                "stdout": "ok",
                "stderr": "",
                "artifacts": [],
                "details": {},
                "command": ["quality.lint"],
            },
        ),
    )
    renderer.render_result(
        next(item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.lint"),
        ExecutionResult(
            error=CLIErrorDetail(
                code="validation_failed",
                message="Bad input",
                hint="Fix the value.",
                retryable=True,
            ),
            exit_code=ExitCode.VALIDATION,
        ),
    )
    output = capsys.readouterr().out

    assert "\x1b[" in output
    assert "quality.lint" in output
    assert "stdout" in output
    assert "Hint" in output


def test_rich_renderer_uses_semantic_color_for_destructive_impact_plan(capsys) -> None:
    from core.application.management.command_specs.extensions import EXTENSIONS_UNINSTALL_SPEC
    from core.entrypoints.cli.platform.renderers import RichRenderer

    RichRenderer(color_enabled=True, animation_enabled=False).render_plan(
        EXTENSIONS_UNINSTALL_SPEC,
        {
            "extension_id": "acme.crm-sync",
            "revision": "rev-1",
            "remove": [{"kind": "bundle", "id": "acme.crm-sync"}],
            "affected": [{"kind": "workflow", "id": "wf-1"}],
            "retained": [],
        },
    )
    output = capsys.readouterr().out

    assert "\x1b[" in output
    assert "Impact plan" in output
    assert "Remove" in output
    assert '"remove"' not in output
    assert '"kind"' not in output
    assert '"id"' not in output
    assert "{" not in output
    assert "}" not in output
    assert "Kind" in output
    assert "bundle" in output
    assert "acme.crm-sync" in output


def test_rich_renderer_skips_duplicate_operation_status_fields(capsys) -> None:
    from core.entrypoints.cli.platform.renderers import RichRenderer

    renderer = RichRenderer(color_enabled=False, animation_enabled=False)
    renderer.render_result(
        next(item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.lint"),
        ExecutionResult(
            result=ToolRunResult(
                operation="quality.lint",
                status="completed",
                exit_code=0,
                stdout="[red]literal[/red]",
            ),
            public_result={
                "operation": "quality.lint",
                "status": "completed",
                "exit_code": 0,
                "stdout": "[red]literal[/red]",
                "stderr": "",
                "artifacts": [],
                "details": {},
                "command": ["quality.lint"],
            },
        ),
    )
    output = capsys.readouterr().out

    assert "quality.lint completed" in output
    assert "operation:" not in output
    assert "status:" not in output
    assert "[red]literal[/red]" in output
    assert "\x1b[" not in output


def test_rich_renderer_escapes_nested_payload_markup(capsys) -> None:
    from core.entrypoints.cli.platform.renderers import RichRenderer

    renderer = RichRenderer(color_enabled=False, animation_enabled=False)
    renderer.render_result(
        next(item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.lint"),
        ExecutionResult(
            result=ToolRunResult(
                operation="quality.lint",
                status="completed",
                exit_code=0,
                details={"literal": "[red]do not style me[/red]"},
            ),
            public_result={
                "operation": "quality.lint",
                "status": "completed",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "artifacts": [],
                "details": {"literal": "[red]do not style me[/red]"},
                "command": ["quality.lint"],
            },
        ),
    )

    output = capsys.readouterr().out

    assert "[red]do not style me[/red]" in output
    assert "\x1b[" not in output


def test_rich_renderer_has_bounded_users_list_table(capsys) -> None:
    from core.entrypoints.cli.platform.renderers import RichRenderer

    renderer = RichRenderer(color_enabled=False, animation_enabled=False)
    renderer.render_result(
        USERS_LIST_SPEC,
        ExecutionResult(
            result=USERS_LIST_SPEC.result_model.model_validate(
                {
                    "users": [
                        {
                            "id": "u_1",
                            "username": "operator@example.com",
                            "email": "operator@example.com",
                            "display_name": "Ops",
                            "roles": ["admin", "viewer"],
                            "is_active": True,
                            "must_change_password": False,
                        }
                    ],
                    "total": 20,
                    "limit": 1,
                    "offset": 0,
                    "has_more": True,
                }
            ),
            public_result={
                "users": [
                    {
                        "id": "u_1",
                        "email": "operator@example.com",
                        "display_name": "Ops",
                        "roles": ["admin", "viewer"],
                        "is_active": True,
                        "must_change_password": False,
                    }
                ],
                "total": 20,
                "limit": 1,
                "offset": 0,
                "has_more": True,
            },
        ),
    )

    output = capsys.readouterr().out

    assert "Users" in output
    assert "operator@example.com" in output
    assert "admin, viewer" in output
    assert "Showing 1 of 20" in output
    assert "--offset 1" in output


def test_default_rich_renderer_preserves_domain_payload_fields(capsys) -> None:
    from core.application.management.command_specs.projects import PROJECT_COMMAND_SPECS
    from core.entrypoints.cli.platform.renderers import renderer_for

    spec = next(
        item
        for item in PROJECT_COMMAND_SPECS
        if item.command_id == "projects.dispatch-notification"
    )
    renderer = renderer_for(
        spec,
        json_requested=False,
        plain_requested=False,
        color_enabled=False,
        animation_enabled=False,
    )
    renderer.render_result(
        spec,
        ExecutionResult(
            result=spec.result_model(
                project_id="proj_123",
                notification_deliveries=[{"id": "delivery_1", "status": "sent"}],
            )
        ),
    )
    output = capsys.readouterr().out

    assert "projects.dispatch-notification" in output
    assert "project_id: proj_123" in output
    assert "notification_deliveries" in output
    assert '"id": "delivery_1"' in output
    assert '"status": "sent"' in output
    assert "\x1b[" not in output


def test_rich_renderer_respects_no_color_for_result_output(capsys) -> None:
    from core.entrypoints.cli.platform.renderers import RichRenderer

    RichRenderer(color_enabled=False, animation_enabled=False).render_result(
        next(item for item in QUALITY_COMMAND_SPECS if item.command_id == "quality.lint"),
        ExecutionResult(
            error=CLIErrorDetail(code="operation_failed", message="failed"),
            exit_code=ExitCode.STABLE_FAILURE,
        ),
    )
    output = capsys.readouterr().out

    assert "\x1b[" not in output
    assert "Error: failed" in output


def test_plain_renderer_renders_error_remediation_and_log_path(capsys) -> None:
    from core.entrypoints.cli.platform.renderers import PlainRenderer

    PlainRenderer().render_result(
        USERS_LIST_SPEC,
        ExecutionResult(
            error=CLIErrorDetail(code="operation_failed", message="failed", hint="Retry safely."),
            exit_code=ExitCode.STABLE_FAILURE,
            operation_id="op-1",
            log_path=".flow-steward/logs/users.list-op-1.log",
        ),
    )
    output = capsys.readouterr().out

    assert "Error: failed" in output
    assert "Code: operation_failed" in output
    assert "Retry safely." in output
    assert "users.list-op-1.log" in output


def test_plain_renderer_skips_fields_hidden_by_current_answers(monkeypatch) -> None:
    from core.entrypoints.cli.platform.renderers import PlainRenderer

    fields = (
        FieldSpec(
            key="storage",
            label="Storage",
            type=FieldType.SINGLE_SELECT,
            choices=(ChoiceItem(value="bundled", label="Bundled"),),
            default="bundled",
        ),
        FieldSpec(
            key="s3_endpoint",
            label="S3 endpoint",
            type=FieldType.TEXT,
            required=True,
            visible_if={"storage": "external_s3"},
        ),
    )
    prompts: list[str] = []

    def prompt(label, **_kwargs):
        prompts.append(label)
        return "1"

    monkeypatch.setattr("click.prompt", prompt)

    values = PlainRenderer().collect_missing(fields, {})

    assert values == {"storage": "bundled"}
    assert prompts == ["Storage"]


def test_plain_renderer_omits_prepopulated_hidden_fields_from_review(monkeypatch, capsys) -> None:
    from core.application.installer.spec import INSTALLER_SPEC
    from core.entrypoints.cli.platform.renderers import PlainRenderer

    monkeypatch.setattr("click.confirm", lambda *_args, **_kwargs: False)

    PlainRenderer().confirm_review(
        INSTALLER_SPEC,
        {
            "file_storage": "bundled",
            "s3_endpoint": "https://user:password@objects.example.test",
            "s3_credentials_path": "/run/secrets/s3.json",
        },
    )

    output = capsys.readouterr().out
    assert "user:password" not in output
    assert "s3.json" not in output


def test_plain_renderer_does_not_ask_for_an_account_when_there_is_only_one(monkeypatch) -> None:
    from core.entrypoints.cli.platform.renderers import PlainRenderer

    only_account = (ChoiceItem(value="acct_main", label="Main account"),)
    fields = tuple(
        field.model_copy(update={"choices": only_account})
        for field in USERS_CREATE_SPEC.fields
        if field.key in {"account_id", "account_ids"}
    )
    prompts: list[str] = []
    monkeypatch.setattr("click.prompt", lambda label, **_kwargs: prompts.append(label) or "1")

    values = PlainRenderer().collect_missing(fields, {})

    assert prompts == []
    assert values == {"account_id": "acct_main"}


def test_plain_renderer_still_asks_for_an_account_when_there_are_several(monkeypatch) -> None:
    from core.entrypoints.cli.platform.renderers import PlainRenderer

    accounts = (
        ChoiceItem(value="acct_main", label="Main account"),
        ChoiceItem(value="acct_eu", label="Europe"),
    )
    fields = tuple(
        field.model_copy(update={"choices": accounts})
        for field in USERS_CREATE_SPEC.fields
        if field.key == "account_id"
    )
    prompts: list[str] = []
    monkeypatch.setattr("click.prompt", lambda label, **_kwargs: prompts.append(label) or "2")

    values = PlainRenderer().collect_missing(fields, {})

    assert prompts == ["Primary account"]
    assert values == {"account_id": "acct_eu"}
