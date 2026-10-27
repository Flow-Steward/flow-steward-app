"""Thin CLI bindings for lifecycle-owned configuration operations."""

from __future__ import annotations

from typing import TypeVar, cast

from pydantic import BaseModel

from core.application.lifecycle.configuration_service import (
    ConfigApplyRequest,
    ConfigProxyFragmentRequest,
    ConfigSetRequest,
    ConfigShowRequest,
    ConfigValidateRequest,
)
from core.application.lifecycle.ports import ProgressSink, TechnicalSink
from core.entrypoints.cli.platform.service_bindings.common import (
    PreparedCommand,
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    current_cancellation_token,
)

_RequestModel = TypeVar("_RequestModel", bound=BaseModel)


async def config_show(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    return await await_maybe(runtime.configuration_service.show(_request(value, ConfigShowRequest)))


async def config_proxy_fragment(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    return await await_maybe(
        runtime.configuration_service.proxy_fragment(_request(value, ConfigProxyFragmentRequest))
    )


async def config_validate(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    return await await_maybe(
        runtime.configuration_service.validate(_request(value, ConfigValidateRequest))
    )


async def config_set(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    return await await_maybe(runtime.configuration_service.set(_request(value, ConfigSetRequest)))


async def config_apply(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    return await await_maybe(
        runtime.configuration_service.apply(
            _request(value, ConfigApplyRequest),
            progress_sink=cast(ProgressSink | None, kwargs.get("progress_sink")),
            technical_sink=cast(TechnicalSink | None, kwargs.get("technical_sink")),
            cancellation=current_cancellation_token(),
        )
    )


async def config_apply_preview(runtime: RuntimeServices, value: object) -> PreparedCommand:
    request = _request(value, ConfigApplyRequest)
    review = await await_maybe(runtime.configuration_service.plan_apply(request))
    return PreparedCommand(execution_input=request, review=review)


def _request(value: object, model: type[_RequestModel]) -> _RequestModel:
    return value if isinstance(value, model) else model.model_validate(value)


CONFIG_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "config.show": config_show,
    "config.proxy-fragment": config_proxy_fragment,
    "config.validate": config_validate,
    "config.set": config_set,
    "config.apply": config_apply,
}

CONFIG_PLAN_BUILDERS = {
    "config.apply.preview": config_apply_preview,
}
