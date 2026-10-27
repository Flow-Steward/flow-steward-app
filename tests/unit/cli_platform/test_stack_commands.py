from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_service import (
    StackLogsRequest,
    StackLogsResult,
    StackMutationResult,
    StackRestartRequest,
    StackStatusResult,
)
from core.application.management.command_specs.contracts import (
    OperationProgressEvent,
    OperationStepStatus,
)
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_bindings.common import PreparedCommand
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@dataclass
class _StackService:
    calls: list[tuple[str, object]] = field(default_factory=list)
    fail_action: str | None = None

    def status(self, request: object) -> StackStatusResult:
        self.calls.append(("status", request))
        return StackStatusResult(action="status", project_name="installed-project")

    def start(self, request: object, **_kwargs: object) -> StackMutationResult:
        return self._mutation("start", request, _kwargs)

    def stop(self, request: object, **_kwargs: object) -> StackMutationResult:
        return self._mutation("stop", request, _kwargs)

    def restart(self, request: object, **_kwargs: object) -> StackMutationResult:
        return self._mutation("restart", request, _kwargs)

    def rebuild(self, request: object, **_kwargs: object) -> StackMutationResult:
        return self._mutation("rebuild", request, _kwargs)

    def logs(self, request: object, **kwargs: object) -> StackLogsResult:
        self.calls.append(("logs", request))
        assert isinstance(request, StackLogsRequest)
        technical_sink = kwargs.get("technical_sink")
        if callable(technical_sink):
            technical_sink("authorization=must-not-appear")
        return StackLogsResult(
            service=request.service,
            tail=request.tail,
            follow=request.follow,
            lines_streamed=1,
            lines=("fs-app | application ready",),
            stdout_full="fs-app | application ready",
        )

    def log_service_choices(self) -> tuple[str, ...]:
        return ("fs-app", "fs-worker")

    def plan(self, action: str, request: object) -> dict[str, object]:
        self.calls.append((f"plan-{action}", request))
        return {
            "action": action,
            "target_services": ["fs-app", "fs-worker"],
            "preservation_gates": ["durable volumes", "database", "artifact storage"],
            "rollback_policy": "exact prior image IDs and running/stopped topology",
        }

    def _mutation(
        self,
        action: str,
        request: object,
        kwargs: dict[str, object],
    ) -> StackMutationResult:
        self.calls.append((action, request))
        progress_sink = kwargs.get("progress_sink")
        if callable(progress_sink):
            progress_sink(
                OperationProgressEvent(
                    step_id="verify_runtime",
                    status=OperationStepStatus.SUCCEEDED,
                    label="Verify runtime readiness",
                )
            )
        if self.fail_action == action:
            raise LifecycleError.stable_failure(
                code=f"stack_{action}_failed",
                cause="The selected runtime failed readiness.",
                remediation="Inspect the lifecycle journal.",
                step="verify_runtime",
            )
        return StackMutationResult(
            action=action,
            status="completed",
            project_name="installed-project",
            target_services=("fs-app",),
            operation_id=f"operation-{action}",
            journal_path=f"/tmp/{action}.json",
            log_path=f"/tmp/{action}.log",
            preservation_verified=True,
        )


@dataclass
class _Runtime:
    stack_service: _StackService = field(default_factory=_StackService)


def _app() -> tuple[Any, _Runtime]:
    runtime = _Runtime()
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def test_stack_specs_are_closed_and_keep_status_distinct_from_top_level_status() -> None:
    specs = {spec.command_id: spec for spec in PUBLIC_COMMAND_SPECS if spec.path[0] == "stack"}

    assert tuple(specs) == (
        "stack.status",
        "stack.start",
        "stack.stop",
        "stack.restart",
        "stack.rebuild",
        "stack.logs",
    )
    assert specs["stack.status"].service_ref == "stack.status"
    assert specs["stack.status"].result_renderer_ref == "stack.status"
    assert specs["stack.logs"].fields[0].choices == ()
    assert specs["stack.logs"].fields[0].choice_provider == "stack.log-services"
    assert specs["stack.logs"].fields[1].default == 200
    assert specs["stack.logs"].fields[2].default is False
    assert all(
        spec.lifecycle_interrupts
        for command_id, spec in specs.items()
        if command_id not in {"stack.status", "stack.logs"}
    )
    mutation_specs = tuple(
        spec
        for command_id, spec in specs.items()
        if command_id not in {"stack.status", "stack.logs"}
    )
    assert all(spec.plan_builder_ref == "stack.mutation.preview" for spec in mutation_specs)
    # Only `start` takes an input, and only the declared image-pin redeploy intent.
    assert all(
        spec.fields == ()
        for command_id, spec in specs.items()
        if command_id not in {"stack.status", "stack.logs", "stack.start"}
    )
    assert tuple(field.key for field in specs["stack.start"].fields) == ("adopt_image_pins",)
    assert specs["stack.start"].fields[0].option_name == "--adopt-image-pins"
    assert specs["stack.start"].fields[0].default is False


def test_stack_help_and_dynamic_choices_come_from_the_same_spec_registry() -> None:
    app, runtime = _app()
    runner = CliRunner()

    group_help = runner.invoke(app, ["stack", "--help"])
    restart_help = runner.invoke(app, ["stack", "restart", "--help"])
    logs_help = runner.invoke(app, ["stack", "logs", "--help"])
    choices = runner.invoke(app, ["stack", "logs", "--plain"], input="1\n")

    assert group_help.exit_code == 0, group_help.output
    assert all(
        leaf in group_help.output
        for leaf in ("status", "start", "stop", "restart", "rebuild", "logs")
    )
    assert logs_help.exit_code == 0, logs_help.output
    assert restart_help.exit_code == 0, restart_help.output
    assert "--operation-id" not in restart_help.output
    assert "--service" in logs_help.output
    assert "--tail" in logs_help.output
    assert "--follow" in logs_help.output
    assert "Choices: fs-app" not in logs_help.output
    assert choices.exit_code == 0, choices.output
    assert runtime.stack_service.calls[-1][0] == "logs"


def test_stack_status_supports_plain_json_and_is_not_root_readiness_status() -> None:
    app, runtime = _app()
    runner = CliRunner()

    plain = runner.invoke(app, ["stack", "status", "--plain"])
    machine = runner.invoke(app, ["stack", "status", "--json"])

    assert plain.exit_code == 0, plain.output
    assert "installed-project" in plain.output
    payload = json.loads(machine.output)
    assert payload["command"] == "stack.status"
    assert payload["result"]["action"] == "status"
    assert [call[0] for call in runtime.stack_service.calls] == ["status", "status"]


def test_stack_status_exits_precondition_when_required_runtime_is_degraded() -> None:
    app, runtime = _app()
    runtime.stack_service.status = lambda _request: StackStatusResult(
        action="status",
        status="degraded",
        project_name="installed-project",
        process_exit_code=4,
    )

    result = CliRunner().invoke(app, ["stack", "status", "--json"])

    assert result.exit_code == 4, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "precondition_failed"
    assert "degraded" in payload["error"]["message"]


def test_stack_mutations_require_review_and_delegate_in_every_presentation_mode() -> None:
    app, runtime = _app()
    runner = CliRunner()

    refused = runner.invoke(app, ["stack", "restart", "--non-interactive", "--plain"])
    plain = runner.invoke(
        app,
        ["stack", "restart", "--non-interactive", "--confirm", "--plain"],
    )
    machine = runner.invoke(
        app,
        ["stack", "rebuild", "--non-interactive", "--confirm", "--json"],
    )

    assert refused.exit_code == 10
    assert [call[0] for call in runtime.stack_service.calls] == [
        "plan-restart",
        "plan-restart",
        "restart",
        "plan-rebuild",
        "rebuild",
    ]
    assert plain.exit_code == 0, plain.output
    assert "Impact plan for stack.restart" in plain.output
    assert "preservation_gates" in plain.output
    assert "rollback_policy" in plain.output
    assert "fs-app" in plain.output
    assert "completed" in plain.output
    payload = json.loads(machine.output)
    assert machine.exit_code == 0
    assert payload["command"] == "stack.rebuild"
    assert payload["operation_id"] == "operation-rebuild"
    assert payload["log_path"] == "/tmp/rebuild.log"
    assert payload["result"]["preservation_verified"] is True


def test_stack_plan_builder_returns_same_typed_execution_request() -> None:
    runtime = _Runtime()
    registry = ServiceRegistry(runtime)
    request = StackRestartRequest()

    import asyncio

    prepared = asyncio.run(registry.build_plan("stack.mutation.preview", request))

    assert isinstance(prepared, PreparedCommand)
    assert prepared.execution_input is request
    assert prepared.execution_input.model_dump() == {}
    assert prepared.review["target_services"] == ["fs-app", "fs-worker"]
    assert "artifact storage" in prepared.review["preservation_gates"]


def test_stack_start_stop_progress_and_stable_error_exit_contracts() -> None:
    app, runtime = _app()
    runner = CliRunner()

    started = runner.invoke(
        app,
        ["stack", "start", "--non-interactive", "--confirm", "--plain"],
    )
    stopped = runner.invoke(
        app,
        ["stack", "stop", "--non-interactive", "--confirm", "--plain"],
    )
    runtime.stack_service.fail_action = "restart"
    failed = runner.invoke(
        app,
        ["stack", "restart", "--non-interactive", "--confirm", "--json"],
    )

    assert started.exit_code == 0, started.output
    assert stopped.exit_code == 0, stopped.output
    assert "Verify runtime readiness" in started.output
    assert "Verify runtime readiness" in stopped.output
    assert failed.exit_code == 8
    payload = json.loads(failed.output)
    assert payload["error"]["code"] == "stack_restart_failed"


def test_stack_logs_defaults_and_redacts_technical_output_in_operation_log(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.platform.operation_log import OperationLog

    monkeypatch.setattr(
        click_adapter,
        "OperationLog",
        lambda command_id: OperationLog(command_id, root=tmp_path),
    )
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        ["stack", "logs", "--service", "fs-app", "--plain", "--verbose"],
    )

    assert result.exit_code == 0, result.output
    request = runtime.stack_service.calls[-1][1]
    assert request.service == "fs-app"
    assert request.tail == 200
    assert request.follow is False
    assert "must-not-appear" not in result.output
    assert "[REDACTED]" in result.output
    logs = list(tmp_path.glob("*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "must-not-appear" not in text
    assert "[REDACTED]" in text


def test_stack_logs_renders_actual_lines_in_plain_and_json_modes() -> None:
    app, _runtime = _app()
    runner = CliRunner()

    plain = runner.invoke(
        app,
        ["stack", "logs", "--service", "fs-app", "--tail", "5", "--plain"],
    )
    machine = runner.invoke(
        app,
        ["stack", "logs", "--service", "fs-app", "--tail", "5", "--json"],
    )

    assert plain.exit_code == 0, plain.output
    assert "fs-app | application ready" in plain.output
    payload = json.loads(machine.output)
    assert payload["result"]["lines"] == ["fs-app | application ready"]
    assert payload["result"]["lines_streamed"] == 1


def test_stack_logs_reports_zero_only_when_no_lines_were_rendered() -> None:
    app, runtime = _app()
    runtime.stack_service.logs = lambda request, **_kwargs: StackLogsResult(
        service=request.service,
        tail=request.tail,
        follow=request.follow,
        lines_streamed=0,
        lines=(),
    )
    runner = CliRunner()

    plain = runner.invoke(app, ["stack", "logs", "--tail", "5", "--plain"])
    machine = runner.invoke(app, ["stack", "logs", "--tail", "5", "--json"])

    assert plain.exit_code == 0, plain.output
    assert '"lines_streamed": 0' in plain.output
    payload = json.loads(machine.output)
    assert payload["result"]["lines"] == []
    assert payload["result"]["lines_streamed"] == 0
