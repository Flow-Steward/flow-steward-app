# ruff: noqa: F401
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .project_policy_validation import ProjectConcurrencyCap, ProjectPolicyConfidence
from .query_models import (
    AuthAccountMembershipView,
    AuthAccountView,
    AuthTokenRecordView,
    AuthUserView,
    DatasetConnectionView,
    DatasetQueryTemplateView,
    DatasetRecordView,
    JobDetailContractView,
    JobsAttentionGroupsView,
    JobsListView,
    ProjectJobsListView,
    ProjectsListView,
    ViewModel,
)

GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE = (
    "Generate a release-readiness report for version 2.4."
)
DEFAULT_FILE_MIME_TYPE = "application/octet-stream"


#: Action hints that unambiguously express a rejection / rework decision. When a
#: caller supplies only ``action`` (no explicit ``approved``), these map to
#: ``approved=False`` so an action-only reject can never be recorded as an approval.
_REJECT_ACTION_HINTS = frozenset(
    {
        "reject",
        "rejected",
        "rework",
        "reworked",
        "deny",
        "denied",
        "decline",
        "declined",
        "request_changes",
        "changes_requested",
    }
)
#: Action hints that express an approval / continue decision.
_APPROVE_ACTION_HINTS = frozenset(
    {
        "approve",
        "approved",
        "accept",
        "accepted",
        "resume",
        "continue",
        "proceed",
        "override",
    }
)


class ExternalActorInput(BaseModel):
    """Bounded delegated identity retained strictly as audit metadata."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    display_name: str | None = None

    @field_validator("id", "display_name", mode="before")
    @classmethod
    def _normalize_external_text(cls, value: Any, info) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("external_actor value must be a string")
        result = value.strip()
        limit = 255 if info.field_name == "id" else 200
        if (
            not result
            or len(result) > limit
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in result)
        ):
            raise ValueError("external_actor value is invalid")
        return result

    @model_validator(mode="after")
    def _require_id(self) -> ExternalActorInput:
        if not self.id:
            raise ValueError("external_actor.id is required")
        return self


class ApprovalDecisionInput(ViewModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    approved: bool
    comment: str = ""
    action: str | None = None
    override_content: str = ""
    approval_id: str
    external_actor: ExternalActorInput | None = None

    @field_validator("approval_id", mode="before")
    @classmethod
    def _normalize_approval_id(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("approval_id must be a string")
        value = value.strip()
        if not value or len(value) > 255 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("approval_id is invalid")
        return value

    @model_validator(mode="before")
    @classmethod
    def _resolve_approved_from_action(cls, data: Any) -> Any:
        """Reconcile ``approved`` with ``action`` when ``approved`` is not given.

        Historically ``approved`` defaulted to True, so a body like
        ``{"action": "reject"}`` (no ``approved``) was recorded as an approval.
        When ``approved`` is absent/None we derive it from a recognized ``action``
        hint (reject -> False, approve -> True). An explicitly supplied
        ``approved`` is always authoritative.
        """
        if not isinstance(data, Mapping):
            return data
        resolved = dict(data)
        approved = resolved.get("approved")
        if approved is not None:
            return resolved
        action = str(resolved.get("action") or "").strip().lower()
        if action in _REJECT_ACTION_HINTS:
            resolved["approved"] = False
        elif action in _APPROVE_ACTION_HINTS:
            resolved["approved"] = True
        else:
            raise ValueError("explicit approval decision is required")
        return resolved

    @field_validator("comment", "override_content", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("approval text fields must be strings")
        return value.strip()

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("action must be a string")
        normalized = value.strip().lower()
        if len(normalized) > 255:
            raise ValueError("action exceeds maximum length")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
            raise ValueError("action contains control characters")
        return normalized or None

    @model_validator(mode="after")
    def _validate_external_contract(self) -> ApprovalDecisionInput:
        if len(self.comment) > 10_000:
            raise ValueError("comment exceeds maximum length")
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in self.comment):
            raise ValueError("comment contains control characters")
        return self


ApprovalDecisionInput.model_rebuild()


class ApprovalResumeAcceptedView(ViewModel):
    model_config = ConfigDict(
        title="Approval Decision Result",
        json_schema_extra={
            "description": (
                "Accepted approval decision showing which workflow thread resumed and its new state."
            ),
            "example": {
                "job_id": "42",
                "thread_id": "thread-7",
                "approved": True,
                "action": "resume",
                "comment": "Approved after confirming the rollout window.",
                "override_content": "",
                "actor": "api-client",
                "actor_type": "service_account",
                "status": "in_progress",
            },
        },
    )

    thread_id: str = Field(
        description="Thread that was resumed after the approval decision was recorded."
    )
    job_id: str = Field(description="Durable job that owns the resumed workflow thread.")
    approval_id: str = Field(description="Immutable approval gate occurrence identifier.")
    deduplicated: bool = Field(
        default=False, description="Whether this response replayed an idempotent decision."
    )
    external_actor: ExternalActorInput | None = Field(
        default=None, description="Delegated audit identity, if supplied."
    )
    approved: bool = Field(description="Final approval decision applied to the paused workflow.")
    action: str | None = Field(
        default=None,
        description="Optional action hint that the workflow can use when resuming execution.",
    )
    comment: str = Field(default="", description="Reviewer note stored with the approval decision.")
    override_content: str = Field(
        default="",
        description="Optional replacement content supplied together with the approval decision.",
    )
    approval_reason: str | None = Field(
        default=None,
        description=(
            "Why the run waited, read from the decided gate: authored (an Approval step), "
            "recovery or autonomy. It decides what the decision does next."
        ),
    )
    actor: str = Field(
        default="operator",
        description="Actor recorded as the source of the approval decision.",
    )
    actor_type: str | None = Field(
        default=None,
        description="Authenticated principal type recorded with the approval decision.",
    )
    status: str = Field(
        default="in_progress",
        description="Workflow status after the approval decision was accepted.",
    )


ApprovalResumeAcceptedView.model_rebuild()


class AuthUserMutationResultView(ViewModel):
    user: AuthUserView = Field(default_factory=AuthUserView)


class AuthUserUnlockResultView(ViewModel):
    status: str = "unlocked"
    user_id: str = ""
    username: str = ""


class AuthUserForcePasswordResetResultView(ViewModel):
    status: str = "password_reset_required"
    user_id: str = ""
    username: str = ""
    revoked_sessions: int = 0


class AuthTokenCreateResultView(ViewModel):
    id: str = ""
    token_id: str = ""
    token: AuthTokenRecordView = Field(default_factory=AuthTokenRecordView)


class AuthTokenStatusResultView(ViewModel):
    status: str = ""
    id: str = ""
    token: AuthTokenRecordView = Field(default_factory=AuthTokenRecordView)


class AuthAccountStatusResultView(ViewModel):
    status: str = ""
    account: AuthAccountView = Field(default_factory=AuthAccountView)


class AuthChangePasswordResultView(ViewModel):
    status: str = ""
    revoked_sessions: int = 0


class AuthSwitchAccountResultView(ViewModel):
    status: str = ""
    account_id: str = ""
    account_ids: list[str] = Field(default_factory=list)
    account_memberships: list[AuthAccountMembershipView] = Field(default_factory=list)


class DatasetConnectionMutationResultView(ViewModel):
    status: str = ""
    connection_id: str = ""
    enabled: bool | None = None
    connection: DatasetConnectionView | None = None


class DatasetQueryTemplateMutationResultView(ViewModel):
    status: str = ""
    query_template_id: str = ""
    enabled: bool | None = None
    query_template: DatasetQueryTemplateView | None = None


class DatasetMutationResultView(ViewModel):
    status: str = ""
    dataset_id: str = ""
    enabled: bool | None = None


class DatasetProjectAssignmentUpdateResultView(ViewModel):
    status: str = ""
    project_id: str = ""
    dataset_ids: list[str] = Field(default_factory=list)
    datasets: list[DatasetRecordView] = Field(default_factory=list)


class DatasetDatasetProjectAssignmentUpdateResultView(ViewModel):
    status: str = ""
    dataset_id: str = ""
    project_ids: list[str] = Field(default_factory=list)


class EventRedriveResultView(ViewModel):
    status: str = "accepted"
    event_id: str
    redriven_dispatches: list[str] = Field(default_factory=list)
    requested_dispatches: list[str] = Field(default_factory=list)


class ProviderWebhookIngestInput(ViewModel):
    provider_key: str
    project_id: str
    project_config: dict[str, Any] = Field(default_factory=dict)
    task_id: str = ""
    source_id: str = ""
    title: str = ""
    description: str = ""
    task_type: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    trigger_config: dict[str, Any] = Field(default_factory=dict)
    files: list[ProviderWebhookFileInput] = Field(default_factory=list)


class ProviderWebhookFileInput(ViewModel):
    attachment_id: str = ""
    filename: str = ""
    mime_type: str = DEFAULT_FILE_MIME_TYPE
    url: str = ""
    size_bytes: int = 0
    content: bytes = b""


class ProviderWebhookIngestResultView(ViewModel):
    provider_key: str
    task_id: str
    job_id: str | None = None
    status: str = "pending"
    queue: str = ""
    file_count: int = 0
    comment_length: int | None = None
    reaction: dict[str, Any] = Field(default_factory=dict)


class ProviderEventIngestInput(ViewModel):
    provider_key: str
    project_id: str
    result: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)


class ProviderEventIngestResultView(ViewModel):
    provider_key: str
    project_id: str
    status: str = "accepted"
    event_ids: list[str] = Field(default_factory=list)
    event_count: int = 0
    notification_deliveries: list[dict[str, Any]] = Field(default_factory=list)


class ReactionEventIngestInput(ViewModel):
    event_type: str
    job_id: Any = None
    workflow_id: str | None = None
    source: str = "external"
    source_id: str = ""
    event_key: str = ""
    dedupe_key: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ReactionEventIngestResultView(ViewModel):
    event: dict[str, Any] = Field(default_factory=dict)
    reactions: list[dict[str, Any]] = Field(default_factory=list)
    notification_deliveries: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowReactionRuleInput(ViewModel):
    reaction_key: str = ""
    display_name: str = ""
    event_type: str
    conditions: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    debounce_seconds: int = 0
    idempotency_key_template: str | None = None
    enabled: bool = True


class WorkflowReactionsUpdateInput(ViewModel):
    reactions: list[WorkflowReactionRuleInput] = Field(default_factory=list)


class JobAcknowledgeInput(ViewModel):
    actor: str = "system"


class ProjectJobsBulkDeleteInput(ViewModel):
    project_id: str
    ids: list[str] = Field(default_factory=list)
    confirm: str = ""


class JobDecisionOverrideInput(ViewModel):
    domain: str
    override_value: Any = None
    original_value: Any = None
    reason: str = ""
    created_by: str = "operator"
    active: bool = True
    message_id: str | int | None = None
    workflow_id: str | None = None
    step_id: str | None = None
    step_execution_id: str | None = None


class JobStepReplayExecutionInput(ViewModel):
    message_id: str | int = ""
    correlation_id: str = ""
    requested_by: str = "operator"
    replay_mode: Literal["step_reexecute", "prompt_reinvoke"] = "step_reexecute"


class JobRetryInterruptedStepInput(ViewModel):
    requested_by: str = "operator"


class ProjectWorkflowBindingsInput(ViewModel):
    workflow_ids: list[str] = Field(default_factory=list)
    #: ``None`` preserves the project's current operator default; ``""`` clears it.
    operator_default_workflow_id: str | None = None


class WorkflowDraftPrepareInput(ViewModel):
    raw_request: str
    workflow_ids: list[str] = Field(default_factory=list)
    operator_default_workflow_id: str = ""


class ProjectPolicyUpdateInput(ViewModel):
    """AC17-R58: None means "not supplied", and the service leaves that field alone."""

    autonomy_mode: str | None = None
    autonomy_confidence_threshold: ProjectPolicyConfidence | None = None
    retry_strategy: str | None = None
    retry_max_attempts: int | None = None
    retry_backoff_initial_seconds: int | None = None
    retry_backoff_max_seconds: int | None = None
    concurrency_cap: ProjectConcurrencyCap | None = None
    tool_policy_mode: str | None = None


class ProjectSettingsRestoreInput(ViewModel):
    author: str = "system"


class ProjectCreateInput(ViewModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    author: str = "system"
    form_data: dict[str, Any] = Field(default_factory=dict)
    name: str
    workspace_id: str
    description: str = ""
    data_connection_ids: list[str] = Field(default_factory=list)
    project_rules: str = ""
    workflow_ids: list[str] = Field(default_factory=list)
    operator_default_workflow_id: str = ""
    default_provider: str = ""
    default_model: str = ""


class ProjectUpdateInput(ViewModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    author: str = "system"
    form_data: dict[str, Any] = Field(default_factory=dict)
    name: str
    workspace_id: str
    description: str = ""
    data_connection_ids: list[str] = Field(default_factory=list)
    project_rules: str = ""
    workflow_ids: list[str] = Field(default_factory=list)
    operator_default_workflow_id: str = ""
    default_provider: str = ""
    default_model: str = ""


class ProjectTaskFileInput(ViewModel):
    """One attachment supplied to the task-enqueue application boundary.

    Multipart transports stage uploads on disk, so public request paths never
    materialize the body as ``bytes``. ``content`` remains only for trusted
    in-process callers that already own a small byte payload.
    """

    filename: str
    input_name: str = ""
    mime_type: str = DEFAULT_FILE_MIME_TYPE
    content: bytes | None = None
    content_path: str = ""
    size_bytes: int = 0
    sha256: str = ""

    def cleanup(self) -> None:
        """Remove a transport-owned staged file, if this input has one."""
        path = str(self.content_path or "").strip()
        if not path:
            return
        try:
            import os

            os.unlink(path)
        except FileNotFoundError:
            return
        except OSError:
            return


class WorkflowRunFileInput(ViewModel):
    """Bounded actual bytes, never a path or a caller-created artifact reference."""

    model_config = ConfigDict(extra="forbid")
    input_name: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default=DEFAULT_FILE_MIME_TYPE, max_length=255)
    content_base64: str = Field(min_length=1, max_length=1_398_104, repr=False)

    def decoded_content(self) -> bytes:
        import base64

        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except ValueError as exc:
            raise ValueError("File content must be strict base64") from exc
        if not content:
            raise ValueError("Uploaded file must contain bytes")
        if self.filename.lower().endswith(".zip") or content.startswith(b"PK\x03\x04"):
            import io

            from core.infrastructure.security.zip_guard import validate_zip_safe

            if not validate_zip_safe(io.BytesIO(content)).safe:
                raise ValueError("Uploaded archive is unsafe")
        return content


class WorkflowRunInput(ViewModel):
    model_config = ConfigDict(
        extra="forbid",
        title="Workflow Run Request",
        json_schema_extra={
            "description": "Explicit project and workflow identity, declared inputs and bounded execution-only context."
        },
    )

    project_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    client_context: dict[str, Any] = Field(default_factory=dict)
    test_mode: bool = False
    files: list[WorkflowRunFileInput] = Field(default_factory=list, max_length=10)

    @field_validator("files")
    @classmethod
    def validate_uploads(cls, value: list[WorkflowRunFileInput]) -> list[WorkflowRunFileInput]:
        if sum(len(row.decoded_content()) for row in value) > 1_048_576:
            raise ValueError("File uploads exceed 1 MiB decoded total")
        names = [row.input_name for row in value]
        if len(set(names)) != len(names):
            raise ValueError("Each file input must have exactly one upload")
        return value

    @field_validator("project_id", "workflow_id", mode="before")
    @classmethod
    def strip_run_identity(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("project_id")
    @classmethod
    def validate_project_identity(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("inputs", "client_context")
    @classmethod
    def bound_execution_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        from core.application.management.json_safety import validate_bounded_json_object

        return validate_bounded_json_object(value)


class BuilderWorkflowRunInput(ViewModel):
    """Acknowledged browser launch of one saved revision; runtime authority is server-owned."""

    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1)
    expected_revision: int = Field(gt=0, strict=True)
    acknowledged: bool = Field(strict=True)
    summary: str = Field(min_length=1, max_length=10000)
    inputs: dict[str, Any] = Field(default_factory=dict)
    files: list[WorkflowRunFileInput] = Field(default_factory=list, max_length=10)

    @field_validator("acknowledged")
    @classmethod
    def require_acknowledgement(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Acknowledge the live effects before running the workflow")
        return value

    @field_validator("summary", "project_id", mode="before")
    @classmethod
    def strip_identity_and_summary(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("project_id")
    @classmethod
    def validate_project_identity(cls, value: str) -> str:
        return WorkflowRunInput.validate_project_identity(value)

    @field_validator("inputs")
    @classmethod
    def bound_execution_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return WorkflowRunInput.bound_execution_payload(value)

    @field_validator("files")
    @classmethod
    def validate_uploads(cls, value: list[WorkflowRunFileInput]) -> list[WorkflowRunFileInput]:
        return WorkflowRunInput.validate_uploads(value)


class ProjectTaskEnqueueInput(ViewModel):
    model_config = ConfigDict(extra="forbid")

    project_id: Any = None
    task_id: str = Field(
        min_length=1,
        description="Required caller correlation identifier for this task submission.",
    )
    task_summary: str
    project_name: str = ""
    workflow_id: str = ""
    mode: str | None = None
    test_mode: Any = False
    task_source: str = "manual"
    entry_path: str = ""
    inputs: dict[str, Any] | None = None
    extra_state: dict[str, Any] = Field(default_factory=dict)
    client_context: dict[str, Any] = Field(default_factory=dict)
    job_meta: dict[str, Any] = Field(default_factory=dict)
    files: list[ProjectTaskFileInput] = Field(default_factory=list)
    idempotency_key: str = ""


class ProjectNotificationRoutesUpdateInput(ViewModel):
    routes: list[dict[str, Any]] = Field(default_factory=list)


class ProjectNotificationRoutesUpdateResultView(ViewModel):
    project_id: str
    notification_routes: list[dict[str, Any]] = Field(default_factory=list)


class ProjectNotificationDispatchInput(ViewModel):
    event_type: str
    severity: str = "info"
    workflow_id: str | None = None
    job_id: Any = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ProjectNotificationDispatchResultView(ViewModel):
    project_id: str
    notification_deliveries: list[dict[str, Any]] = Field(default_factory=list)


class ExtensionProjectCreateInput(ViewModel):
    parent_project_id: str
    name: str
    request_summary: str
    workflow_ids: list[str] = Field(default_factory=list)
    operator_default_workflow_id: str = ""


class ProjectScheduledJobCreateInput(ViewModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    cron_expr: str
    schedule_timezone: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    enabled: Any = True
    created_by: str = "operator"


class ProjectScheduledJobUpdateInput(ViewModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    cron_expr: str
    schedule_timezone: str | None = None
    inputs: dict[str, Any] | None = None
    enabled: bool | None = None


class ProjectScheduledJobToggleInput(ViewModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Any = None
