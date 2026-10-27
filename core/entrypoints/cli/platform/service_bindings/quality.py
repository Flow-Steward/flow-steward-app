"""Quality-domain CLI bindings to typed application tooling services."""

from __future__ import annotations

from collections.abc import Callable

from core.application.management.command_specs.contracts import OperationProgressEvent
from core.application.tooling.quality.models import (
    QualityCheckCiExcludesRequest,
    QualityCheckNamingRequest,
    QualityLintRequest,
    QualityReleaseGateRequest,
    QualitySonarEnsureRulesRequest,
    QualitySonarExportIssuesRequest,
    QualitySonarGenerateBacklogRequest,
    QualityVerifyRequest,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    call_with_optional_progress,
)


async def quality_lint(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.quality_tooling_service.lint,
            QualityLintRequest.model_validate(value),
            progress_sink,
        )
    )


async def quality_check_ci_excludes(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.quality_tooling_service.check_ci_excludes,
            QualityCheckCiExcludesRequest.model_validate(value),
            progress_sink,
        )
    )


async def quality_check_naming(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.quality_tooling_service.check_naming,
            QualityCheckNamingRequest.model_validate(value),
            progress_sink,
        )
    )


async def quality_release_gate(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.quality_tooling_service.release_gate,
            QualityReleaseGateRequest.model_validate(value),
            progress_sink,
        )
    )


async def quality_verify(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.quality_tooling_service.verify,
            QualityVerifyRequest.model_validate(value),
            progress_sink,
        )
    )


async def quality_sonar_export_issues(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.quality_tooling_service.sonar_export_issues,
            QualitySonarExportIssuesRequest.model_validate(value),
            progress_sink,
        )
    )


async def quality_sonar_ensure_rules(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.quality_tooling_service.sonar_ensure_rules,
            QualitySonarEnsureRulesRequest.model_validate(value),
            progress_sink,
        )
    )


async def quality_sonar_generate_backlog(
    runtime: RuntimeServices,
    value: object,
    *,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    **_kwargs: object,
) -> object:
    return await await_maybe(
        call_with_optional_progress(
            runtime.quality_tooling_service.sonar_generate_backlog,
            QualitySonarGenerateBacklogRequest.model_validate(value),
            progress_sink,
        )
    )


QUALITY_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "quality.lint": quality_lint,
    "quality.check-ci-excludes": quality_check_ci_excludes,
    "quality.check-naming": quality_check_naming,
    "quality.release-gate": quality_release_gate,
    "quality.verify": quality_verify,
    "quality.sonar-export-issues": quality_sonar_export_issues,
    "quality.sonar-ensure-rules": quality_sonar_ensure_rules,
    "quality.sonar-generate-backlog": quality_sonar_generate_backlog,
}
