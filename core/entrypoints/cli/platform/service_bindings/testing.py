"""Testing-domain CLI bindings to typed application tooling services."""

from __future__ import annotations

from collections.abc import Callable

from core.application.management.command_specs.contracts import OperationProgressEvent
from core.application.tooling.testing.models import (
    TestingCutoverPreflightRequest,
    TestingCutoverSmokeRequest,
    TestingFuzzApiRequest,
    TestingFuzzMcpRequest,
    TestingFuzzUiRequest,
    TestingNightfireRegressionRequest,
    TestingSmokeTestRequest,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    call_with_optional_progress,
)


async def testing_smoke_test(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.testing_tooling_service.smoke_test,
            TestingSmokeTestRequest.model_validate(value),
            progress_sink,
        )
    )


async def testing_nightfire_regression(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.testing_tooling_service.nightfire_regression,
            TestingNightfireRegressionRequest.model_validate(value),
            progress_sink,
        )
    )


async def testing_fuzz_api(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.testing_tooling_service.fuzz_api,
            TestingFuzzApiRequest.model_validate(value),
            progress_sink,
        )
    )


async def testing_fuzz_mcp(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.testing_tooling_service.fuzz_mcp,
            TestingFuzzMcpRequest.model_validate(value),
            progress_sink,
        )
    )


async def testing_fuzz_ui(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.testing_tooling_service.fuzz_ui,
            TestingFuzzUiRequest.model_validate(value),
            progress_sink,
        )
    )


async def testing_cutover_preflight(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.testing_tooling_service.cutover_preflight,
            TestingCutoverPreflightRequest.model_validate(value),
            progress_sink,
        )
    )


async def testing_cutover_smoke(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.testing_tooling_service.cutover_smoke,
            TestingCutoverSmokeRequest.model_validate(value),
            progress_sink,
        )
    )


TESTING_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "testing.smoke-test": testing_smoke_test,
    "testing.nightfire-regression": testing_nightfire_regression,
    "testing.fuzz-api": testing_fuzz_api,
    "testing.fuzz-mcp": testing_fuzz_mcp,
    "testing.fuzz-ui": testing_fuzz_ui,
    "testing.cutover-preflight": testing_cutover_preflight,
    "testing.cutover-smoke": testing_cutover_smoke,
}
