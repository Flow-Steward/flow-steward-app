"""Localization-domain CLI bindings to typed application tooling services."""

from __future__ import annotations

from collections.abc import Callable

from core.application.management.command_specs.contracts import OperationProgressEvent
from core.application.tooling.i18n.models import (
    LocalizationBootstrapCatalogRequest,
    LocalizationCompileRequest,
    LocalizationInitLocaleRequest,
    LocalizationRebuildPotRequest,
    LocalizationUpdateRequest,
    LocalizationValidateKeysRequest,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    call_with_optional_progress,
)


async def localization_bootstrap_catalog(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.localization_tooling_service.bootstrap_catalog,
            LocalizationBootstrapCatalogRequest.model_validate(value),
            progress_sink,
        )
    )


async def localization_rebuild_pot(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.localization_tooling_service.rebuild_pot,
            LocalizationRebuildPotRequest.model_validate(value),
            progress_sink,
        )
    )


async def localization_init_locale(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.localization_tooling_service.init_locale,
            LocalizationInitLocaleRequest.model_validate(value),
            progress_sink,
        )
    )


async def localization_update(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.localization_tooling_service.update,
            LocalizationUpdateRequest.model_validate(value),
            progress_sink,
        )
    )


async def localization_compile(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.localization_tooling_service.compile,
            LocalizationCompileRequest.model_validate(value),
            progress_sink,
        )
    )


async def localization_validate_keys(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.localization_tooling_service.validate_keys,
            LocalizationValidateKeysRequest.model_validate(value),
            progress_sink,
        )
    )


LOCALIZATION_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "localization.bootstrap-catalog": localization_bootstrap_catalog,
    "localization.rebuild-pot": localization_rebuild_pot,
    "localization.init-locale": localization_init_locale,
    "localization.update": localization_update,
    "localization.compile": localization_compile,
    "localization.validate-keys": localization_validate_keys,
}
