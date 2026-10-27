"""Database-domain CLI bindings to typed application tooling operations."""

from __future__ import annotations

from core.application.tooling.db.models import (
    DatabaseBootstrapSeedRequest,
    DatabaseGenerateMigrationRequest,
    DatabaseQueryDiagnosticsRequest,
    DatabaseRestoreParityCaptureRequest,
    DatabaseRestoreParityVerifyRequest,
    DatabaseStatusRequest,
    DatabaseTuneEnvRequest,
    DatabaseUpgradeRequest,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
)


async def database_status(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, DatabaseStatusRequest)
        else DatabaseStatusRequest.model_validate(value)
    )
    return await await_maybe(runtime.database_tooling_service.status(command))


async def database_upgrade(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, DatabaseUpgradeRequest)
        else DatabaseUpgradeRequest.model_validate(value)
    )
    return await await_maybe(runtime.database_tooling_service.upgrade(command))


async def database_bootstrap_seed(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, DatabaseBootstrapSeedRequest)
        else DatabaseBootstrapSeedRequest.model_validate(value)
    )
    return await await_maybe(runtime.database_tooling_service.bootstrap_seed(command))


async def database_generate_migration(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, DatabaseGenerateMigrationRequest)
        else DatabaseGenerateMigrationRequest.model_validate(value)
    )
    return await await_maybe(runtime.database_tooling_service.generate_migration(command))


async def database_query_diagnostics(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, DatabaseQueryDiagnosticsRequest)
        else DatabaseQueryDiagnosticsRequest.model_validate(value)
    )
    return await await_maybe(runtime.database_tooling_service.query_diagnostics(command))


async def database_tune_env(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, DatabaseTuneEnvRequest)
        else DatabaseTuneEnvRequest.model_validate(value)
    )
    return await await_maybe(runtime.database_tooling_service.tune_env(command))


async def database_restore_parity_capture(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, DatabaseRestoreParityCaptureRequest)
        else DatabaseRestoreParityCaptureRequest.model_validate(value)
    )
    return await await_maybe(runtime.database_tooling_service.restore_parity_capture(command))


async def database_restore_parity_verify(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, DatabaseRestoreParityVerifyRequest)
        else DatabaseRestoreParityVerifyRequest.model_validate(value)
    )
    return await await_maybe(runtime.database_tooling_service.restore_parity_verify(command))


DATABASE_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "database.status": database_status,
    "database.upgrade": database_upgrade,
    "database.bootstrap-seed": database_bootstrap_seed,
    "database.generate-migration": database_generate_migration,
    "database.query-diagnostics": database_query_diagnostics,
    "database.tune-env": database_tune_env,
    "database.restore-parity-capture": database_restore_parity_capture,
    "database.restore-parity-verify": database_restore_parity_verify,
}
