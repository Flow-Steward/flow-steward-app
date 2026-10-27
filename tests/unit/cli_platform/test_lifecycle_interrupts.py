from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.application.lifecycle.contracts import (
    CancellationToken,
    LifecycleOperationJournal,
    LifecycleOperationKind,
    LifecycleOperationStatus,
)
from core.application.lifecycle.errors import ForcedLifecycleInterrupt, LifecycleError
from core.application.management.command_specs.contracts import ExitCode, OperationType
from core.application.management.command_specs.users import USERS_LIST_SPEC
from core.entrypoints.cli.platform.execution import execute_command
from core.entrypoints.cli.platform.interrupts import lifecycle_interrupt_scope
from core.entrypoints.cli.platform.renderers import JSONRenderer
from core.entrypoints.cli.platform.service_bindings.common import current_cancellation_token
from core.entrypoints.cli.platform.service_registry import SERVICE_FACTORIES, ServiceRegistry
from core.infrastructure.lifecycle.state_store import LifecycleStateStore


class _LifecycleRegistry:
    def __init__(self) -> None:
        self.cancellation: CancellationToken | None = None

    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
        cancellation: CancellationToken | None = None,
    ) -> object:
        del value, progress_sink, technical_sink
        assert key == "users.list"
        self.cancellation = cancellation
        return {"users": [], "total": 0}


class _FailingLifecycleRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
        cancellation: CancellationToken | None = None,
    ) -> object:
        del key, value, progress_sink, technical_sink, cancellation
        failure = LifecycleError.dependency(
            code="docker_unavailable",
            cause="Docker is unavailable.",
            remediation="Start Docker and retry.",
            step="preflight",
        )
        failure.operation_id = "operation-1"
        failure.log_path = "/tmp/operation-1.log"
        raise failure


class _EarlyForcedInterruptRegistry:
    def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
        cancellation: CancellationToken | None = None,
    ) -> object:
        del key, value, progress_sink, technical_sink
        assert cancellation is not None
        cancellation.set_recovery_command("flow-steward stack restart")
        handler = _installed_handler()
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)
        raise AssertionError("forced interrupt must leave the invocation")


def _clock(values: Iterator[float]):
    return lambda: next(values)


def _installed_handler():
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    return handler


def test_first_interrupt_requests_safe_boundary_cancellation_and_restores_handler() -> None:
    token = CancellationToken()
    token.set_recovery_command("flow-steward stack restart")
    messages: list[str] = []
    previous = signal.getsignal(signal.SIGINT)

    with lifecycle_interrupt_scope(
        token,
        reporter=messages.append,
        clock=_clock(iter((100.0,))),
    ):
        _installed_handler()(signal.SIGINT, None)
        assert token.requested is True
        assert token.forced is False

    assert signal.getsignal(signal.SIGINT) is previous
    assert "safe boundary" in messages[0].lower()
    assert "again within 5 seconds" in messages[0].lower()


def test_second_interrupt_within_window_forces_exit_and_marks_recovery(
    tmp_path: Path,
) -> None:
    store = LifecycleStateStore(tmp_path)
    store.save_journal(
        LifecycleOperationJournal(
            operation_id="operation-1",
            kind=LifecycleOperationKind.STACK_RESTART,
            status=LifecycleOperationStatus.RUNNING,
            recovery_command="flow-steward stack restart",
        )
    )
    token = CancellationToken()
    token.set_recovery_command("flow-steward stack restart")
    token.register_force_callback(
        lambda: store.mark_recovery_required(
            "operation-1", step="restart", error_code="lifecycle_forced_interrupt"
        )
    )
    messages: list[str] = []

    def _raises_operation_137():
        handler = _installed_handler()
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)

    with (
        pytest.raises(ForcedLifecycleInterrupt),
        lifecycle_interrupt_scope(
            token,
            reporter=messages.append,
            clock=_clock(iter((100.0, 104.0))),
        ),
    ):
        _raises_operation_137()

    journal = store.load_journal("operation-1")
    assert token.forced is True
    assert journal.status is LifecycleOperationStatus.RECOVERY_REQUIRED
    assert journal.last_error_code == "lifecycle_forced_interrupt"
    assert "flow-steward stack restart" in messages[-1]


def test_second_interrupt_after_window_repeats_cooperative_request() -> None:
    token = CancellationToken()
    messages: list[str] = []

    with lifecycle_interrupt_scope(
        token,
        reporter=messages.append,
        clock=_clock(iter((100.0, 106.0))),
    ):
        handler = _installed_handler()
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)

    assert token.requested is True
    assert token.forced is False
    assert len(messages) == 2


def test_disabled_interrupt_scope_preserves_normal_click_signal_behavior() -> None:
    token = CancellationToken()
    previous = signal.getsignal(signal.SIGINT)

    with lifecycle_interrupt_scope(token, enabled=False):
        assert signal.getsignal(signal.SIGINT) is previous

    assert token.requested is False
    assert signal.getsignal(signal.SIGINT) is previous


def test_common_execution_path_passes_exact_optional_cancellation_token() -> None:
    registry = _LifecycleRegistry()
    token = CancellationToken()
    spec = USERS_LIST_SPEC.model_copy(
        update={
            "operation_type": OperationType.CHANGE,
            "lifecycle_interrupts": True,
        }
    )

    result = execute_command(
        spec=spec,
        command_input=spec.input_model(),
        services=registry,
        cancellation=token,
    )

    assert result.error is None
    assert registry.cancellation is token


def test_service_registry_exposes_token_without_changing_existing_factory_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[CancellationToken | None] = []

    async def existing_style_factory(
        runtime,
        value,
        *,
        progress_sink=None,
        technical_sink=None,
    ):
        del runtime, value, progress_sink, technical_sink
        observed.append(current_cancellation_token())
        return {"users": [], "total": 0}

    monkeypatch.setitem(SERVICE_FACTORIES, "test.lifecycle", existing_style_factory)
    token = CancellationToken()
    registry = ServiceRegistry(object())

    result = asyncio.run(
        registry.invoke(
            "test.lifecycle",
            object(),
            cancellation=token,
        )
    )

    assert result == {"users": [], "total": 0}
    assert observed == [token]
    assert current_cancellation_token() is None


def test_executor_maps_typed_lifecycle_error_and_operation_metadata() -> None:
    spec = USERS_LIST_SPEC.model_copy(
        update={
            "operation_type": OperationType.CHANGE,
            "lifecycle_interrupts": True,
        }
    )

    result = execute_command(
        spec=spec,
        command_input=spec.input_model(),
        services=_FailingLifecycleRegistry(),
    )

    assert result.error is not None
    assert result.error.code == "docker_unavailable"
    assert result.error.message == "Docker is unavailable."
    assert result.error.hint == "Start Docker and retry."
    assert result.exit_code is ExitCode.DEPENDENCY
    assert result.operation_id == "operation-1"
    assert result.log_path == "/tmp/operation-1.log"


def test_executor_maps_early_forced_interrupt_to_exit_10_with_recovery_command() -> None:
    spec = USERS_LIST_SPEC.model_copy(
        update={
            "operation_type": OperationType.CHANGE,
            "lifecycle_interrupts": True,
        }
    )
    technical: list[str] = []

    result = execute_command(
        spec=spec,
        command_input=spec.input_model(),
        services=_EarlyForcedInterruptRegistry(),
        technical_sink=technical.append,
    )

    assert result.error is not None
    assert result.error.code == "lifecycle_forced_interrupt"
    assert result.exit_code is ExitCode.INTERRUPTED
    assert "flow-steward stack restart" in result.error.hint
    assert any("flow-steward stack restart" in message for message in technical)


@pytest.mark.parametrize(
    "failure",
    (
        pytest.param(SystemExit(0), id="service-exit-zero"),
        pytest.param(SystemExit(13), id="service-exit-nonzero"),
        pytest.param(asyncio.CancelledError("private-diagnostic"), id="cancelled-awaitable"),
        pytest.param(GeneratorExit("private-diagnostic"), id="generator-exit"),
    ),
)
def test_executor_contains_service_control_exits_in_redacted_failure_envelope(
    failure: BaseException, capsys: pytest.CaptureFixture[str]
) -> None:
    class ExitingRegistry:
        async def invoke(
            self,
            _key: str,
            _value: object,
            *,
            progress_sink: object | None = None,
            technical_sink: object | None = None,
        ) -> object:
            del progress_sink, technical_sink
            print("service output: private-diagnostic")
            raise failure

    technical: list[str] = []
    result = execute_command(
        spec=USERS_LIST_SPEC,
        command_input=USERS_LIST_SPEC.input_model(),
        services=ExitingRegistry(),
        capture_stdout=True,
        technical_sink=technical.append,
        known_secrets=("private-diagnostic",),
    )

    assert result.exit_code is ExitCode.STABLE_FAILURE
    assert result.result is None
    assert result.public_result is None
    assert result.error is not None
    assert result.error.code == "internal_error"
    assert result.error.message == "An unexpected internal error occurred."
    assert result.error.hint
    assert capsys.readouterr().out == ""
    assert "private-diagnostic" not in "\n".join(technical)
    assert "service output: [REDACTED]" in technical

    JSONRenderer().render_result(USERS_LIST_SPEC, result)
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 1
    envelope = json.loads(output.out)
    assert envelope["schema"] == "flow-steward.cli.v1"
    assert envelope["ok"] is False
    assert envelope["command"] == "users.list"
    assert envelope["error"]["code"] == "internal_error"
    assert "result" not in envelope
    assert "private-diagnostic" not in output.out + output.err
