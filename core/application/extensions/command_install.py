from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.management.command_models import (
    ExtensionInstallInput,
    ExtensionInstallResultView,
    ExtensionReleaseInstallInput,
    ExtensionRuntimeHealthResultView,
    ExtensionRuntimeTokenInput,
    ExtensionRuntimeTokenResultView,
)
from core.application.management.errors import ServiceValidationError
from core.application.management.scope import ScopeContext

from .browser_authority_approval import PreparedBrowserAuthorityApproval
from .command_context import ExtensionCommandContext
from .command_core import ExtensionCommandCore
from .command_install_dependencies import ExtensionInstallDependencyPolicy
from .command_install_projection import ExtensionInstallProjectionService
from .command_install_release import (
    ExtensionInstallLifecyclePort,
    ExtensionReleaseInstallWorkflow,
)
from .command_install_runtime import ExtensionInstallRuntimeService
from .service_support import (
    _http_probe,
    _list_extension_runtime_connections,
    _safe_remote_http_url,
)


class ExtensionCommandInstall:
    """Explicitly composed facade for extension install capabilities."""

    def __init__(
        self,
        *,
        context: ExtensionCommandContext,
        core: ExtensionCommandCore,
        lifecycle: Callable[[], ExtensionInstallLifecyclePort],
    ) -> None:
        self._context = context
        self._core = core
        self._lifecycle = lifecycle
        self._dependencies = ExtensionInstallDependencyPolicy()
        self._projections = ExtensionInstallProjectionService(context=context)

    def _runtime(self) -> ExtensionInstallRuntimeService:
        return ExtensionInstallRuntimeService(
            context=self._context,
            http_probe=_http_probe,
            list_runtime_connections=_list_extension_runtime_connections,
        )

    def _release_workflow(self) -> ExtensionReleaseInstallWorkflow:
        return ExtensionReleaseInstallWorkflow(
            context=self._context,
            lifecycle=self._lifecycle,
            validate_dependencies=self._validate_release_dependencies_for_install,
            run_handshake=self.run_install_handshake,
            project_release=self.project_release_for_install,
            materialize_bridges=self.materialize_dataset_execution_bridges,
            validate_remote_url=_safe_remote_http_url,
        )

    def _logger(self) -> Any:
        return self._context.logger()

    def _install_store(self) -> Any:
        return self._context.install_store()

    def _runtime_store(self) -> Any:
        return self._context.runtime_store()

    def _extension_persistence_connection(self) -> Any:
        return self._context.persistence_connection()

    def install_extension(
        self,
        inp: ExtensionInstallInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionInstallResultView:
        del inp, scope
        raise ServiceValidationError(
            "Direct extension install is retired; install a validated release instead"
        )

    def install_release(
        self,
        inp: ExtensionReleaseInstallInput,
        *,
        scope: ScopeContext | None = None,
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionInstallResultView:
        """Install a release and all host-owned projections atomically."""
        connection = self._context.persistence_connection()
        if connection is None:
            return self._install_release(
                inp,
                scope=scope,
                allow_same_version_enrichment=allow_same_version_enrichment,
                _browser_authority_handoff=_browser_authority_handoff,
            )
        from core.infrastructure.db.db_pool import (
            active_transaction_connection,
            managed_transaction_connection,
            transaction_connection,
        )

        if (
            active_transaction_connection(connection) is not None
            and managed_transaction_connection(connection) is None
        ):
            return self._install_release(
                inp,
                scope=scope,
                allow_same_version_enrichment=allow_same_version_enrichment,
                _browser_authority_handoff=_browser_authority_handoff,
            )
        with transaction_connection(connection):
            return self._install_release(
                inp,
                scope=scope,
                allow_same_version_enrichment=allow_same_version_enrichment,
                _browser_authority_handoff=_browser_authority_handoff,
            )

    def _install_release(
        self,
        inp: ExtensionReleaseInstallInput,
        *,
        scope: ScopeContext | None = None,
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionInstallResultView:
        return self._release_workflow().install(
            inp,
            scope=scope,
            allow_same_version_enrichment=allow_same_version_enrichment,
            _browser_authority_handoff=_browser_authority_handoff,
        )

    def validate_release_dependencies_for_install(
        self,
        *,
        release: Any,
        account_id: str,
        runtime_store: Any,
        install_store: Any,
    ) -> list[dict[str, Any]]:
        return self._validate_release_dependencies_for_install(
            release=release,
            account_id=account_id,
            runtime_store=runtime_store,
            install_store=install_store,
        )

    def _validate_release_dependencies_for_install(
        self,
        *,
        release: Any,
        account_id: str,
        runtime_store: Any,
        install_store: Any,
    ) -> list[dict[str, Any]]:
        return self._dependencies.validate(
            release=release,
            account_id=account_id,
            runtime_store=runtime_store,
            install_store=install_store,
        )

    def extension_dependency_specs(self, descriptor: dict[str, Any]) -> list[dict[str, Any]]:
        return self._dependencies.extension_dependency_specs(descriptor)

    def _dependency_version_error(
        self,
        *,
        dependency: dict[str, Any],
        installed_version: str,
    ) -> str:
        return self._dependencies.dependency_version_error(
            dependency=dependency,
            installed_version=installed_version,
        )

    def _reject_required_dependency_cycles(
        self,
        *,
        root_extension_id: str,
        root_descriptor: dict[str, Any],
        runtime_store: Any,
    ) -> None:
        self._dependencies.reject_required_cycles(
            root_extension_id=root_extension_id,
            root_descriptor=root_descriptor,
            runtime_store=runtime_store,
        )

    def project_release_for_install(
        self,
        *,
        install_record: Any,
        release: Any,
        account_id: str,
    ) -> None:
        self._projections.project_release(
            install_record=install_record,
            release=release,
            account_id=account_id,
        )

    def materialize_dataset_execution_bridges(
        self,
        *,
        install_record: Any,
        release: Any,
        account_id: str,
    ) -> None:
        self._projections.materialize_dataset_bridges(
            install_record=install_record,
            release=release,
            account_id=account_id,
        )

    def remove_dataset_execution_bridges(self, *, install_id: str, account_id: str = "") -> None:
        self._projections.remove_dataset_bridges(install_id=install_id, account_id=account_id)

    def deactivate_projected_skills(self, *, install_id: str, account_id: str) -> None:
        self._projections.deactivate_projected_skills(install_id=install_id, account_id=account_id)

    def run_install_handshake(
        self,
        *,
        install_id: str,
        scope: ScopeContext | None = None,
    ) -> ExtensionRuntimeHealthResultView:
        return self._runtime().run_handshake(install_id=install_id, scope=scope)

    def issue_runtime_token(
        self,
        inp: ExtensionRuntimeTokenInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionRuntimeTokenResultView:
        return self._runtime().issue_token(inp, scope=scope)

    def _token_denial_reason(
        self,
        *,
        install: Any,
        account_id: str,
        connection_id: str,
        project_id: str | None,
    ) -> str:
        return self._runtime().token_denial_reason(
            install=install,
            account_id=account_id,
            connection_id=connection_id,
            project_id=project_id,
        )


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
