"""Thin CLI bindings for lifecycle-owned installed-stack operations."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from core.application.lifecycle.stack_service import (
    StackLogsRequest,
    StackRebuildRequest,
    StackRestartRequest,
    StackStartRequest,
    StackStatusRequest,
    StackStopRequest,
)
from core.application.management.command_specs.contracts import ChoiceItem
from core.entrypoints.cli.platform.service_bindings.common import (
    PreparedCommand,
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    current_cancellation_token,
)

_RequestModel = TypeVar("_RequestModel", bound=BaseModel)


async def stack_status(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    return await await_maybe(runtime.stack_service.status(_request(value, StackStatusRequest)))


async def stack_start(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    return await _mutation(runtime, "start", _request(value, StackStartRequest), kwargs)


async def stack_stop(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    return await _mutation(runtime, "stop", _request(value, StackStopRequest), kwargs)


async def stack_restart(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    return await _mutation(runtime, "restart", _request(value, StackRestartRequest), kwargs)


async def stack_rebuild(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    return await _mutation(runtime, "rebuild", _request(value, StackRebuildRequest), kwargs)


async def stack_logs(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    request = _request(value, StackLogsRequest)
    return await await_maybe(
        runtime.stack_service.logs(
            request,
            progress_sink=kwargs.get("progress_sink"),
            technical_sink=kwargs.get("technical_sink"),
        )
    )


async def stack_log_services(runtime: RuntimeServices) -> tuple[ChoiceItem, ...]:
    values = await await_maybe(runtime.stack_service.log_service_choices())
    return tuple(ChoiceItem(value=str(value), label=str(value)) for value in values)


async def stack_mutation_preview(runtime: RuntimeServices, value: object) -> PreparedCommand:
    if isinstance(value, StackStartRequest):
        review = await await_maybe(runtime.stack_service.plan("start", value))
    elif isinstance(value, StackStopRequest):
        review = await await_maybe(runtime.stack_service.plan("stop", value))
    elif isinstance(value, StackRestartRequest):
        review = await await_maybe(runtime.stack_service.plan("restart", value))
    elif isinstance(value, StackRebuildRequest):
        review = await await_maybe(runtime.stack_service.plan("rebuild", value))
    else:
        raise ValueError("Stack mutation preview requires a typed stack request")
    return PreparedCommand(execution_input=value, review=review)


async def _mutation(
    runtime: RuntimeServices,
    action: str,
    request: object,
    kwargs: dict[str, object],
) -> object:
    method = getattr(runtime.stack_service, action)
    return await await_maybe(
        method(
            request,
            progress_sink=kwargs.get("progress_sink"),
            technical_sink=kwargs.get("technical_sink"),
            cancellation=current_cancellation_token(),
        )
    )


def _request(value: object, model: type[_RequestModel]) -> _RequestModel:
    return value if isinstance(value, model) else model.model_validate(value)


STACK_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "stack.status": stack_status,
    "stack.start": stack_start,
    "stack.stop": stack_stop,
    "stack.restart": stack_restart,
    "stack.rebuild": stack_rebuild,
    "stack.logs": stack_logs,
}

STACK_PLAN_BUILDERS = {"stack.mutation.preview": stack_mutation_preview}
