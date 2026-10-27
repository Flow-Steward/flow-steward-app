"""Thin CLI bindings for protected PostgreSQL backup operations."""

from __future__ import annotations

from typing import cast

from core.application.lifecycle.backup_service import (
    BackupCreateRequest,
    BackupListRequest,
    BackupVerifyRequest,
)
from core.application.lifecycle.ports import ProgressSink, TechnicalSink
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    current_cancellation_token,
)


async def backup_create(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    request = (
        value
        if isinstance(value, BackupCreateRequest)
        else BackupCreateRequest.model_validate(value)
    )
    return await await_maybe(
        runtime.backup_command_service.create(
            request,
            progress_sink=cast(ProgressSink | None, kwargs.get("progress_sink")),
            technical_sink=cast(TechnicalSink | None, kwargs.get("technical_sink")),
            cancellation=current_cancellation_token(),
        )
    )


async def backup_list(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    request = (
        value if isinstance(value, BackupListRequest) else BackupListRequest.model_validate(value)
    )
    return await await_maybe(runtime.backup_command_service.list(request))


async def backup_verify(runtime: RuntimeServices, value: object, **kwargs: object) -> object:
    request = (
        value
        if isinstance(value, BackupVerifyRequest)
        else BackupVerifyRequest.model_validate(value)
    )
    return await await_maybe(
        runtime.backup_command_service.verify(
            request,
            progress_sink=cast(ProgressSink | None, kwargs.get("progress_sink")),
            technical_sink=cast(TechnicalSink | None, kwargs.get("technical_sink")),
        )
    )


BACKUP_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "backup.create": backup_create,
    "backup.list": backup_list,
    "backup.verify": backup_verify,
}

__all__ = ["BACKUP_SERVICE_FACTORIES"]
