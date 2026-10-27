"""Thin CLI bindings for lifecycle-owned environment operations."""

from __future__ import annotations

from typing import Any, TypeVar, cast

from pydantic import BaseModel

from core.application.lifecycle.environment_service import (
    EnvironmentShowRequest,
    EnvironmentSwitchRequest,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    PreparedCommand,
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    current_cancellation_token,
)

_RequestModel = TypeVar("_RequestModel", bound=BaseModel)


async def environment_show(
    runtime: RuntimeServices,
    value: object,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        runtime.environment_service.show(_request(value, EnvironmentShowRequest))
    )


async def environment_switch(
    runtime: RuntimeServices,
    value: object,
    **kwargs: object,
) -> object:
    return await await_maybe(
        runtime.environment_service.switch(
            _request(value, EnvironmentSwitchRequest),
            progress_sink=cast(Any, kwargs.get("progress_sink")),
            technical_sink=cast(Any, kwargs.get("technical_sink")),
            cancellation=current_cancellation_token(),
        )
    )


async def environment_switch_preview(
    runtime: RuntimeServices,
    value: object,
) -> PreparedCommand:
    request = _request(value, EnvironmentSwitchRequest)
    review = await await_maybe(runtime.environment_service.plan_switch(request))
    deferred = (
        ("artifact_decision",)
        if review.get("artifact_decision_required") and request.artifact_decision is None
        else ()
    )
    return PreparedCommand(
        execution_input=request,
        review=review,
        deferred_fields=deferred,
    )


def _request(value: object, model: type[_RequestModel]) -> _RequestModel:
    return value if isinstance(value, model) else model.model_validate(value)


ENVIRONMENT_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "environment.show": environment_show,
    "environment.switch": environment_switch,
}

ENVIRONMENT_PLAN_BUILDERS = {
    "environment.switch.preview": environment_switch_preview,
}
