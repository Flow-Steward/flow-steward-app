"""Typed application-service contracts consumed by CLI transport bindings."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from core.application.auth.user_admin.contracts import (
    AccountListView,
    UserActiveUpdateInput,
    UserAuditContext,
    UserCreateInput,
    UserListView,
    UserMutationResultView,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
    UserSelectorInput,
)
from core.application.management.command_models import (
    ApprovalDecisionInput,
    ApprovalResumeAcceptedView,
    ExtensionArchiveInstallResultView,
    ExtensionArchiveUninstallResultView,
    ExtensionEnableResultView,
    JobAcknowledgeInput,
    JobLifecycleResultView,
    ProjectNotificationDispatchInput,
    ProjectNotificationDispatchResultView,
    ProjectTaskEnqueueInput,
    ProjectTaskEnqueueResultView,
    WorkflowRunInput,
)
from core.application.management.query_models import (
    ExtensionDetailView,
    ExtensionHealthView,
    ExtensionListFilterInput,
    ExtensionListView,
    JobDetailContractView,
    JobErrorsView,
    JobLogsView,
    JobMessagesView,
    JobsAttentionGroupsView,
    JobsListView,
    JobStatusView,
    ProjectDetailPageView,
    ProjectNotificationDeliveriesView,
    ProjectNotificationRoutesView,
    ProjectsListView,
    ProjectWorkflowCatalogView,
    ProjectWorkflowEntrypointsView,
    WorkflowContractResultView,
    WorkflowSuggestionsResultView,
    WorkflowValidationResultView,
)
from core.application.management.scope import ScopeContext


class UserQueryServiceProtocol(Protocol):
    def list_users(
        self,
        *,
        search: str = "",
        include_inactive: bool = False,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
        role: str = "",
        active_filter: bool | None = None,
        managed_account_ids: list[str] | None = None,
    ) -> UserListView: ...

    def list_accounts(self, *, limit: int = 200) -> AccountListView: ...


class UserCommandServiceProtocol(Protocol):
    def create_user(
        self,
        command: UserCreateInput,
        *,
        allowed_account_ids: list[str] | None = None,
        notification_locale: str = "en",
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView: ...

    def set_password(
        self,
        command: UserPasswordUpdateInput,
        *,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView: ...

    def force_password_reset(
        self,
        command: UserSelectorInput,
        *,
        allowed_account_ids: list[str] | None = None,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView: ...

    def unlock_user(
        self,
        command: UserSelectorInput,
        *,
        allowed_account_ids: list[str] | None = None,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView: ...

    def set_active(
        self,
        command: UserActiveUpdateInput,
        *,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView: ...

    def set_roles(
        self,
        command: UserRolesUpdateInput,
        *,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView: ...


class ProjectQueryServiceProtocol(Protocol):
    def list_projects(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        search: str = "",
        scope: ScopeContext | None = None,
    ) -> ProjectsListView: ...

    def get_project_detail(
        self,
        project_id: str,
        *,
        page: int = 1,
        per_page: int = 25,
        scope: ScopeContext | None = None,
    ) -> ProjectDetailPageView: ...

    def list_notification_routes(
        self,
        project_id: str,
        *,
        enabled_only: bool = False,
        scope: ScopeContext | None = None,
    ) -> ProjectNotificationRoutesView: ...

    def list_notification_deliveries(
        self,
        project_id: str,
        *,
        job_id: str | None = None,
        limit: int = 200,
        scope: ScopeContext | None = None,
    ) -> ProjectNotificationDeliveriesView: ...


class ProjectCommandServiceProtocol(Protocol):
    def dispatch_project_notification(
        self,
        project_id: str,
        command: ProjectNotificationDispatchInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectNotificationDispatchResultView: ...

    def start_mcp_workflow_run(
        self,
        command: WorkflowRunInput | dict[str, Any],
        *,
        idempotency_key: str = "",
        scope: ScopeContext | None = None,
    ) -> ProjectTaskEnqueueResultView: ...

    def enqueue_manual_task(
        self,
        command: ProjectTaskEnqueueInput,
        *,
        allow_implicit_project: bool = False,
        scope: ScopeContext | None = None,
    ) -> ProjectTaskEnqueueResultView: ...


class JobQueryServiceProtocol(Protocol):
    def list_jobs(
        self,
        *,
        group_by: str = "",
        project_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
        page: int = 1,
        per_page: int = 0,
        status: str = "",
        workspace_id: str | None = None,
        scope: ScopeContext | None = None,
    ) -> JobsListView | JobsAttentionGroupsView: ...

    async def get_job_detail_contract(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
        user_role: str = "operator",
    ) -> JobDetailContractView: ...

    async def get_job_status(
        self, job_id: str, *, scope: ScopeContext | None = None
    ) -> JobStatusView: ...

    def list_logs(
        self,
        job_id: str,
        *,
        since_id: object = 0,
        before_id: object = 0,
        newest: bool = False,
        limit: int = 25,
        scope: ScopeContext | None = None,
    ) -> JobLogsView: ...

    def list_messages(
        self,
        job_id: str,
        *,
        since_id: object = 0,
        before_id: object = 0,
        newest: bool = False,
        step_id: str = "",
        producer: str = "",
        skill: str = "",
        status: str = "",
        limit: int = 25,
        scope: ScopeContext | None = None,
    ) -> JobMessagesView: ...

    def list_errors(
        self,
        job_id: str,
        *,
        limit: int = 200,
        scope: ScopeContext | None = None,
    ) -> JobErrorsView: ...


class JobCommandServiceProtocol(Protocol):
    def acknowledge_job(
        self,
        job_id: str,
        command: JobAcknowledgeInput,
        *,
        scope: ScopeContext | None = None,
    ) -> JobLifecycleResultView: ...

    def unacknowledge_job(
        self, job_id: str, *, scope: ScopeContext | None = None
    ) -> JobLifecycleResultView: ...

    def cancel_job(
        self,
        job_id: str,
        *,
        actor: str = "operator",
        comment: str = "Cancelled by user",
        scope: ScopeContext | None = None,
    ) -> JobLifecycleResultView: ...

    def restart_job(
        self, job_id: str, *, scope: ScopeContext | None = None
    ) -> JobLifecycleResultView: ...


class ApprovalCommandServiceProtocol(Protocol):
    def resume_task(
        self,
        *,
        thread_id: str,
        scope: ScopeContext | None,
        decision: ApprovalDecisionInput,
    ) -> ApprovalResumeAcceptedView: ...


class WorkflowOperatorServiceProtocol(Protocol):
    def convert_group(self, **kwargs: Any) -> dict[str, Any]: ...

    def hooks_list(self, **kwargs: Any) -> dict[str, Any]: ...

    def hook_get(self, **kwargs: Any) -> dict[str, Any]: ...

    def hook_preview(self, **kwargs: Any) -> dict[str, Any]: ...

    def hook_bind(self, **kwargs: Any) -> dict[str, Any]: ...

    def hook_unbind(self, **kwargs: Any) -> dict[str, Any]: ...

    def hook_create_and_bind(self, **kwargs: Any) -> dict[str, Any]: ...

    def where_used(self, **kwargs: Any) -> dict[str, Any]: ...

    def authoring_schema(self, **kwargs: Any) -> Any: ...

    def draft_validate(self, **kwargs: Any) -> Any: ...

    def draft_save(self, **kwargs: Any) -> Any: ...

    def workflow_definition(self, **kwargs: Any) -> Any: ...

    def apply_changes(self, **kwargs: Any) -> dict[str, Any]: ...

    def export_workflow(self, **kwargs: Any) -> dict[str, Any]: ...

    def import_workflow(self, **kwargs: Any) -> dict[str, Any]: ...

    def workflow_stats(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_project_workflows(
        self,
        project_id: str,
        *,
        enabled_only: bool = True,
        include_validation: bool = False,
        scope: ScopeContext | None = None,
    ) -> ProjectWorkflowCatalogView: ...

    def list_project_entrypoints(
        self,
        project_id: str,
        *,
        enabled_only: bool = True,
        scope: ScopeContext | None = None,
    ) -> ProjectWorkflowEntrypointsView: ...

    def project_workflow_validate(
        self,
        project_id: str,
        workflow_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> WorkflowValidationResultView: ...

    def project_workflow_contract(
        self,
        project_id: str,
        workflow_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> WorkflowContractResultView: ...

    def project_workflow_suggestions(
        self,
        project_id: str,
        workflow_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> WorkflowSuggestionsResultView: ...


class ExtensionQueryServiceProtocol(Protocol):
    def list_extensions(
        self,
        *,
        filters: ExtensionListFilterInput | dict[str, Any] | None = None,
        scope: ScopeContext | None = None,
    ) -> ExtensionListView: ...

    def get_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionDetailView: ...

    def get_health(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionHealthView: ...


class ExtensionBindingServiceProtocol(Protocol):
    def install_extension_archive_path(
        self,
        *,
        archive_path: Path,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView: ...

    def preview_extension_uninstall(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]: ...

    def uninstall_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        plan_revision: str = "",
    ) -> ExtensionArchiveUninstallResultView: ...

    def set_enabled_state(
        self,
        extension_id: str,
        *,
        version: str | None,
        enabled: bool,
        config: dict[str, Any],
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
        _skip_rescan: bool = False,
    ) -> ExtensionEnableResultView: ...


class OpsHealthSnapshotServiceProtocol(Protocol):
    def collect(
        self,
        *,
        base_url: str,
        verify_ssl: bool,
        timeout_seconds: int,
        dsn: str,
        stale_heartbeat_seconds: int,
    ) -> dict[str, Any]: ...
