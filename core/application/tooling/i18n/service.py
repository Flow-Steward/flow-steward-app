"""Typed localization tooling operations for schema-driven public CLI."""

from __future__ import annotations

from collections.abc import Callable

from core.application.management.command_specs.contracts import OperationProgressEvent
from core.application.tooling.contracts import ToolRunner, ToolRunResult
from core.application.tooling.i18n.models import (
    LocalizationBootstrapCatalogRequest,
    LocalizationCompileRequest,
    LocalizationInitLocaleRequest,
    LocalizationRebuildPotRequest,
    LocalizationUpdateRequest,
    LocalizationValidateKeysRequest,
)


class LocalizationToolingService:
    """Run localization tooling leaves through explicit typed request models."""

    def __init__(self, *, runner: ToolRunner) -> None:
        self._runner = runner

    def bootstrap_catalog(
        self,
        request: LocalizationBootstrapCatalogRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.i18n import ui_i18n_catalog

        return self._run_i18n(
            "localization.bootstrap-catalog",
            lambda: ui_i18n_catalog.bootstrap_catalog(source=request.source),
            request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def rebuild_pot(
        self,
        request: LocalizationRebuildPotRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.i18n import ui_i18n_catalog

        return self._run_i18n(
            "localization.rebuild-pot",
            ui_i18n_catalog.rebuild_pot,
            request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def init_locale(
        self,
        request: LocalizationInitLocaleRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.i18n import ui_i18n_catalog

        return self._run_i18n(
            "localization.init-locale",
            lambda: ui_i18n_catalog.init_locale(
                code=request.code,
                label=request.label,
                native_label=request.native_label,
            ),
            request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def update(
        self,
        request: LocalizationUpdateRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.i18n import ui_i18n_catalog

        return self._run_i18n(
            "localization.update",
            ui_i18n_catalog.update_catalogs,
            request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def compile(
        self,
        request: LocalizationCompileRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.i18n import ui_i18n_catalog

        return self._run_i18n(
            "localization.compile",
            lambda: ui_i18n_catalog.compile_catalogs(force=request.force),
            request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def validate_keys(
        self,
        request: LocalizationValidateKeysRequest,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        from core.application.tooling.i18n import ui_i18n_catalog

        return self._run_i18n(
            "localization.validate-keys",
            ui_i18n_catalog.validate_keys,
            request.timeout_seconds,
            progress_sink=progress_sink,
        )

    def _run_i18n(
        self,
        operation: str,
        target: Callable[[], int],
        timeout_seconds: int,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    ) -> ToolRunResult:
        return self._runner.run_callable(
            operation=operation,
            target=lambda: int(target() or 0),
            timeout_seconds=timeout_seconds,
            progress_sink=progress_sink,
        )
