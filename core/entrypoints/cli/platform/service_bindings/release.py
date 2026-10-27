"""Thin CLI bindings for lifecycle-owned release operations."""

from __future__ import annotations

from typing import TypeVar, cast

from pydantic import BaseModel

from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.ports import ProgressSink, TechnicalSink
from core.application.release.service import (
    PreparedReleaseUpgrade,
    ReleaseCheckRequest,
    ReleaseInfoRequest,
    ReleaseResult,
    ReleaseUpgradeRequest,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    PreparedCommand,
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    current_cancellation_token,
)

_RequestModel = TypeVar("_RequestModel", bound=BaseModel)


async def release_info(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    return await await_maybe(runtime.release_service.info(_request(value, ReleaseInfoRequest)))


async def release_check(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    return await await_maybe(runtime.release_service.check(_request(value, ReleaseCheckRequest)))


async def release_upgrade(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    prepared = (
        value
        if isinstance(value, PreparedReleaseUpgrade)
        else _request(value, ReleaseUpgradeRequest)
    )
    result = await await_maybe(
        runtime.release_service.upgrade(
            prepared,
            progress_sink=cast(ProgressSink | None, kwargs.get("progress_sink")),
            technical_sink=cast(TechnicalSink | None, kwargs.get("technical_sink")),
            cancellation=current_cancellation_token(),
        )
    )
    if isinstance(result, ReleaseResult) and result.status == "migration_required":
        raise LifecycleError.stable_failure(
            code="release_migration_required",
            cause="The target release requires an explicit database migration before activation.",
            remediation=result.remediation,
            step="release_upgrade",
            details=result.model_dump(mode="json"),
        )
    return result


async def release_upgrade_preview(
    runtime: RuntimeServices,
    value: object,
) -> PreparedCommand:
    request = _request(value, ReleaseUpgradeRequest)
    prepared = await await_maybe(runtime.release_service.prepare_upgrade(request))
    review = await await_maybe(runtime.release_service.review_upgrade(prepared))
    return PreparedCommand(execution_input=prepared, review=review)


def _request(value: object, model: type[_RequestModel]) -> _RequestModel:
    return value if isinstance(value, model) else model.model_validate(value)


RELEASE_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "release.info": release_info,
    "release.check": release_check,
    "release.upgrade": release_upgrade,
}

RELEASE_PLAN_BUILDERS = {"release.upgrade.preview": release_upgrade_preview}
