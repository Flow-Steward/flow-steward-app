"""Product CLI composition used only inside the existing fs-app runtime."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any

from core.entrypoints.cli.lifecycle_runtime import (
    _LINT_SCRIPT_PATH,
    _REPO_ROOT,
    LifecycleCLIRuntime,
    _installed_runtime_environment,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

if TYPE_CHECKING:
    from core.application.auth.bootstrap_tokens import BootstrapTokenService
    from core.application.auth.user_admin import UserAdminCommandService, UserAdminQueryService
    from core.application.extensions.binding_service import ExtensionBindingService
    from core.application.extensions.query_service import ExtensionQueryService
    from core.application.extensions.scaffold import ExtensionScaffoldService
    from core.application.management.command_services import (
        ApprovalCommandService,
        JobCommandService,
        ProjectCommandService,
    )
    from core.application.management.query_services import ProjectQueryService
    from core.application.management.workflow_operator_service import WorkflowOperatorService
    from core.application.ports.platform_store import (
        JobStorePort,
        PlatformStorePort,
        ProjectStorePort,
    )
    from core.application.reports.reporting_service import ReportingService
    from core.application.tooling.db.service import DatabaseToolingService
    from core.application.tooling.i18n.service import LocalizationToolingService
    from core.application.tooling.quality.service import QualityToolingService
    from core.application.tooling.testing.service import TestingToolingService
    from core.entrypoints.cli.platform.service_bindings.contracts import JobQueryServiceProtocol
    from core.infrastructure.tooling.process_runner import OSProcessRunner


class CLIRuntime(LifecycleCLIRuntime):
    """Compose product services in fs-app while reusing lifecycle ownership."""

    def __init__(self) -> None:
        super().__init__()

    @cached_property
    def bootstrap_token_service(self) -> BootstrapTokenService:
        from core.application.management.errors import ServicePreconditionError

        raise ServicePreconditionError(
            "Bootstrap recovery requires the installed host command: "
            "./flow-steward users issue-bootstrap-token --confirm."
        )

    @cached_property
    def _platform_dsn(self) -> str:
        from core.infrastructure.runtime.entrypoint_db_adapters import enforce_runtime_schema

        dsn = self._configured_platform_dsn()
        enforce_runtime_schema(dsn, component="cli")
        return dsn

    @cached_property
    def identity_store(self) -> PlatformStorePort:
        from core.infrastructure.runtime.entrypoint_db_adapters import create_identity_store

        return create_identity_store(self._platform_dsn)

    @cached_property
    def project_store(self) -> ProjectStorePort:
        from core.infrastructure.runtime.entrypoint_db_adapters import create_project_store

        return create_project_store(self._platform_dsn)

    @cached_property
    def integration_store(self) -> PlatformStorePort:
        from core.infrastructure.runtime.entrypoint_db_adapters import create_integration_store

        return create_integration_store(self._platform_dsn)

    @cached_property
    def job_store(self) -> JobStorePort:
        from core.infrastructure.runtime.entrypoint_db_adapters import create_job_store

        return create_job_store(self._platform_dsn)

    @cached_property
    def worker_store(self) -> PlatformStorePort:
        from core.infrastructure.runtime.entrypoint_db_adapters import create_worker_store

        return create_worker_store(self._platform_dsn)

    def get_identity_store(self) -> PlatformStorePort:
        return self.identity_store

    def get_project_store(self) -> ProjectStorePort:
        return self.project_store

    def get_integration_store(self) -> PlatformStorePort:
        return self.integration_store

    def get_job_store(self) -> PlatformStorePort:
        return self.job_store

    @cached_property
    def reporting_store(self) -> PlatformStorePort:
        from core.infrastructure.runtime.entrypoint_db_adapters import create_reporting_store

        return create_reporting_store(self._platform_dsn)

    def get_reporting_store(self) -> PlatformStorePort:
        return self.reporting_store

    def get_worker_store(self) -> PlatformStorePort:
        return self.worker_store

    @cached_property
    def user_query_service(self) -> UserAdminQueryService:
        from core.application.auth.user_admin import UserAdminQueryService

        return UserAdminQueryService(get_logger=self.get_identity_store)

    @cached_property
    def user_command_service(self) -> UserAdminCommandService:
        from core.application.auth.user_admin import UserAdminCommandService

        def _notify_signup(**kwargs: Any) -> Any:
            from core.application.extensions.system_email import send_signup_notification_email

            return send_signup_notification_email(**kwargs)

        return UserAdminCommandService(
            get_logger=self.get_identity_store,
            signup_notifier=_notify_signup,
        )

    @cached_property
    def project_query_service(self) -> ProjectQueryService:
        from core.application.management.management_runtime import create_project_query_service

        return create_project_query_service(
            get_logger=self.get_project_store,
            get_job_store=self.get_job_store,
        )

    @cached_property
    def project_command_service(self) -> ProjectCommandService:
        from core.application.management.management_runtime import create_project_command_service

        return create_project_command_service(
            get_logger=self.get_project_store,
            get_notification_logger=self.get_integration_store,
        )

    @cached_property
    def job_query_service(self) -> JobQueryServiceProtocol:
        from core.application.management.management_runtime import create_job_query_service

        return create_job_query_service(get_logger=self.get_job_store)

    @cached_property
    def job_command_service(self) -> JobCommandService:
        from core.application.management.management_runtime import create_job_command_service

        return create_job_command_service(
            get_logger=self.get_job_store,
            get_project_run_config=lambda project_id: (
                self.get_project_store().get_project_run_config(project_id)
            ),
        )

    @cached_property
    def approval_command_service(self) -> ApprovalCommandService:
        from core.entrypoints.cli.approval_command_runtime import (
            create_runtime_approval_command_service,
        )

        return create_runtime_approval_command_service(get_logger=self.get_job_store)

    @cached_property
    def reporting_service(self) -> ReportingService:
        from core.application.reports.reporting_service import ReportingService

        return ReportingService(
            get_logger_only=self.get_reporting_store,
            job_query_service=lambda: self.job_query_service,
        )

    @cached_property
    def ops_health_snapshot_service(self) -> _OpsHealthSnapshotService:
        return _OpsHealthSnapshotService()

    @cached_property
    def database_tooling_service(self) -> DatabaseToolingService:
        from core.application.tooling.db.service import DatabaseToolingService

        return DatabaseToolingService(
            project_root=_REPO_ROOT,
            dsn_provider=self._configured_platform_dsn,
        )

    @cached_property
    def tool_runner(self) -> OSProcessRunner:
        from core.infrastructure.tooling.process_runner import OSProcessRunner

        return OSProcessRunner(working_directory=_REPO_ROOT)

    @cached_property
    def testing_tooling_service(self) -> TestingToolingService:
        from core.application.tooling.testing.service import TestingToolingService
        from core.infrastructure.config.connection_strings import resolve_rabbitmq_url

        return TestingToolingService(
            runner=self.tool_runner,
            database_dsn_provider=self._configured_platform_dsn,
            rabbitmq_url_provider=lambda: resolve_rabbitmq_url(_installed_runtime_environment()),
        )

    @cached_property
    def quality_tooling_service(self) -> QualityToolingService:
        from core.application.tooling.quality.service import QualityToolingService

        return QualityToolingService(
            runner=self.tool_runner,
            lint_script_path=_LINT_SCRIPT_PATH,
        )

    @cached_property
    def localization_tooling_service(self) -> LocalizationToolingService:
        from core.application.tooling.i18n.service import LocalizationToolingService

        return LocalizationToolingService(runner=self.tool_runner)

    @cached_property
    def workflow_operator_service(self) -> WorkflowOperatorService:
        from core.application.management.management_runtime import create_workflow_operator_service

        return create_workflow_operator_service(
            get_logger=self.get_worker_store,
            project_query_service=lambda: self.project_query_service,
            get_project_store=self.get_project_store,
            get_integration_store=self.get_integration_store,
            get_install_store=self._get_extension_install_store,
            get_job_store=self.get_job_store,
        )

    @cached_property
    def extension_scaffold_service(self) -> ExtensionScaffoldService:
        from core.application.extensions.scaffold import ExtensionScaffoldService

        return ExtensionScaffoldService()

    @cached_property
    def extension_query_service(self) -> ExtensionQueryService:
        from core.application.extensions.query_service import create_extension_query_service
        from core.infrastructure.extension_runtime import extension_runtime_helpers

        return create_extension_query_service(
            get_logger=self.get_identity_store,
            get_install_store=self._get_extension_install_store,
            get_runtime_store=self._get_extension_runtime_store,
            list_extension_projects=self._list_extension_projects,
            resolve_extension_runtime_record=self._resolve_extension_runtime_record,
            list_extension_runtime_records=(
                extension_runtime_helpers.list_extension_runtime_records
            ),
            build_extension_compiled_manifest=(
                extension_runtime_helpers.build_extension_compiled_manifest
            ),
            extension_validation_summary=(extension_runtime_helpers.extension_validation_summary),
            compile_extension_preview=lambda extension: __import__(
                "core.infrastructure.extension_runtime.extension_runtime",
                fromlist=["compile_extension_preview"],
            ).compile_extension_preview(extension),
            run_extension_health=lambda extension: __import__(
                "core.infrastructure.extension_runtime.extension_runtime",
                fromlist=["run_extension_health"],
            ).run_extension_health(extension),
        )

    @cached_property
    def artifact_plane(self) -> Any:
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_storage import create_storage_adapter
        from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage

        return build_artifact_storage(
            environ=dict(_runtime_env),
            create_storage_adapter=create_storage_adapter,
            artifact_plane_factory=ArtifactDatasetPlane,
        )

    @cached_property
    def extension_binding_service(self) -> ExtensionBindingService:
        from core.application.extensions.query_service import create_extension_query_service
        from core.application.management.command_service_runtime import (
            create_extension_binding_service,
        )
        from core.infrastructure.extension_runtime import extension_runtime_helpers

        return create_extension_binding_service(
            # Lifecycle keeps the identity store; operations get the store that
            # can actually persist what they write. One provider for both is
            # what handed actions a store with no project connections on it.
            get_logger=self.get_identity_store,
            get_operation_store=self.get_integration_store,
            get_artifact_plane=lambda: self.artifact_plane,
            get_project_by_id=lambda project_id: self.get_project_store().get_project(project_id),
            resolve_extension_runtime_record=self._resolve_extension_runtime_record,
            extension_identifier=extension_runtime_helpers.extension_identifier,
            extension_operation_handler_mode=(
                extension_runtime_helpers.extension_operation_handler_mode
            ),
            extension_operation_failure_status=(
                extension_runtime_helpers.extension_operation_failure_status
            ),
            get_extension_install_store=self._get_extension_install_store,
            get_extension_runtime_store=self._get_extension_runtime_store,
            extension_query_service_factory=(
                lambda *, bl, install_store, runtime_store: create_extension_query_service(
                    get_logger=lambda: bl,
                    get_install_store=lambda: install_store,
                    get_runtime_store=lambda: runtime_store,
                    list_extension_projects=self._list_extension_projects,
                    resolve_extension_runtime_record=self._resolve_extension_runtime_record,
                    list_extension_runtime_records=(
                        extension_runtime_helpers.list_extension_runtime_records
                    ),
                    build_extension_compiled_manifest=(
                        extension_runtime_helpers.build_extension_compiled_manifest
                    ),
                    extension_validation_summary=(
                        extension_runtime_helpers.extension_validation_summary
                    ),
                )
            ),
        )

    def _resolve_extension_runtime_record(
        self,
        extension_id: str,
        *,
        account_id: str | None = None,
    ) -> tuple[dict | None, Any]:
        from core.infrastructure.extension_runtime import extension_runtime_helpers

        return extension_runtime_helpers.resolve_extension_runtime_record(
            extension_id,
            # The runtime record carries the store an operation persists through,
            # so it is resolved against the operation store too.
            get_logger_only=self.get_integration_store,
            account_id=account_id,
        )

    def _list_extension_projects(self, *, account_id: str) -> list[dict[str, Any]]:
        logger = self.get_project_store()
        return logger.get_projects(account_id=account_id) if logger is not None else []

    def _get_extension_install_store(self) -> Any:
        from core.application.extensions.service_support import get_default_extension_install_store

        return get_default_extension_install_store(get_logger=self.get_identity_store)

    def _get_extension_runtime_store(self) -> Any:
        from core.application.extensions.service_support import get_default_extension_runtime_store

        return get_default_extension_runtime_store(get_logger=self.get_identity_store)


class _OpsHealthSnapshotService:
    """Thin object wrapper around the shared application health collector."""

    def collect(self, **kwargs: Any) -> dict[str, Any]:
        from core.application.jobs.ops_health_snapshot_runtime import collect_ops_health_snapshot

        return collect_ops_health_snapshot(**kwargs)


__all__ = ["CLIRuntime"]
