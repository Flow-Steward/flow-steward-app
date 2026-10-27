"""
Pydantic request models for the Flow Steward API.

Extracted from serve.py to provide typed DTOs for all API endpoints.
Import from here in both route modules and tests.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.application.extensions.direct_effect_recovery_models import DirectEffectResolutionRef
from core.application.extensions.identity import normalize_extension_id
from core.application.management.project_policy_validation import (
    ProjectConcurrencyCap,
    ProjectPolicyConfidence,
)

_TOKEN_A_F0_9_64 = "^(?:|[a-f0-9]{64})$"

# ── Project ─────────────────────────────────────────────────────────────


class ProjectWorkflowSelectionRequest(BaseModel):
    workflow_ids: list[str] = []
    # None means "not mentioned" and preserves the project's current default;
    # an explicit empty string clears it. Collapsing the two silently reset the
    # operator default every time someone linked a workflow.
    operator_default_workflow_id: str | None = None


class ProjectPolicyRequest(BaseModel):
    """AC17-R58: every field is optional, and omitted means *unchanged*.

    These used to carry class defaults, so a body of ``{"concurrency_cap": 2}`` arrived at the
    service indistinguishable from one that had asked for supervised autonomy, bounded retry
    and registry_only tools. The write is an unconditional UPSERT of every policy column, so
    that request silently reset the whole document.
    """

    autonomy_mode: Literal["manual", "supervised", "auto"] | None = None
    autonomy_confidence_threshold: ProjectPolicyConfidence | None = None
    retry_strategy: Literal["bounded", "flat", "failure_class"] | None = None
    retry_max_attempts: int | None = None
    retry_backoff_initial_seconds: int | None = None
    retry_backoff_max_seconds: int | None = None
    concurrency_cap: ProjectConcurrencyCap | None = None
    tool_policy_mode: Literal["registry_only", "restricted", "permissive"] | None = None

    @model_validator(mode="before")
    @classmethod
    def flatten_policy_readback_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("policy"), dict):
            return value
        flattened = dict(value["policy"])
        for key in (
            "autonomy_mode",
            "autonomy_confidence_threshold",
            "retry_strategy",
            "retry_max_attempts",
            "retry_backoff_initial_seconds",
            "retry_backoff_max_seconds",
            "concurrency_cap",
            "tool_policy_mode",
        ):
            if key in value:
                flattened[key] = value[key]
        return flattened


class ProjectJobsBulkDeleteRequest(BaseModel):
    ids: list[Any] = []
    confirm: str = ""


class WorkflowPreviewRequest(BaseModel):
    task_summary: str
    workflow_id: str = ""


# ── Provider / Integration ──────────────────────────────────────────────


class ProviderRequest(BaseModel):
    provider_key: str
    provider_type_key: Literal["task_provider", "notification_provider", "tool_provider"]
    display_name: str = ""
    description: str = ""
    implementation_kind: str = "builtin"
    integration_key: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    enabled: bool = True


class ProviderSecretRequest(BaseModel):
    secret_key: str
    secret_value: str
    project_id: str | None = None
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("project_id", mode="before")
    @classmethod
    def _normalize_project_id(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("project_id must be a scalar identifier")
        return str(value).strip()


class ProjectProviderBindingRequest(BaseModel):
    provider_key: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


# ── Notification ────────────────────────────────────────────────────────


class NotificationRouteRequest(BaseModel):
    route_key: str = ""
    display_name: str = ""
    provider_key: str
    workflow_id: str | None = None
    event_type: str
    severity: str = "any"
    route_config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class NotificationRoutesRequest(BaseModel):
    routes: list[NotificationRouteRequest] = Field(default_factory=list)


class NotificationDispatchRequest(BaseModel):
    event_type: str
    severity: str = "info"
    workflow_id: str | None = None
    job_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("job_id", mode="before")
    @classmethod
    def _normalize_job_id(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("job_id must be a scalar identifier")
        return str(value).strip()


# ── Workflow Reaction ───────────────────────────────────────────────────


class WorkflowReactionRuleRequest(BaseModel):
    reaction_key: str = ""
    display_name: str = ""
    event_type: str
    conditions: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    debounce_seconds: int = 0
    idempotency_key_template: str | None = None
    enabled: bool = True


class WorkflowReactionsRequest(BaseModel):
    reactions: list[WorkflowReactionRuleRequest] = Field(default_factory=list)


class ReactionIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event_type: str
    job_id: Any = None
    workflow_id: str | None = None
    source: str = "external"
    source_id: str = ""
    event_key: str = ""
    dedupe_key: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _normalize_event_type(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("event_type is required")
        return normalized

    @field_validator("workflow_id", "source", "source_id", "event_key", "dedupe_key")
    @classmethod
    def _normalize_reaction_text_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()


# ── Auth ────────────────────────────────────────────────────────────────


class AuthBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    display_name: str = ""

    @property
    def username(self) -> str:
        return str(self.email or "")


class AuthLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    remember_me: bool = False
    account_id: str = ""
    # Sent together with a temporary password: the only thing a temporary
    # password can do is replace itself with this one (B3).
    new_password: str = ""

    @property
    def username(self) -> str:
        return str(self.email or "")


class AuthChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    revoke_other_sessions: bool = True


class AuthPersonalTokenCreateRequest(BaseModel):
    label: str = ""
    name: str = ""
    expires_in_days: int | None = None
    scopes: list[str] = Field(default_factory=list)
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_workflow_ids: list[str] = Field(default_factory=list)


class AuthServiceAccountCreateRequest(BaseModel):
    service_account_key: str
    display_name: str = ""
    description: str = ""
    scopes: list[str] = Field(default_factory=list)
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_workflow_ids: list[str] = Field(default_factory=list)
    is_active: bool = True


class AuthServiceAccountUpdateRequest(BaseModel):
    display_name: str = ""
    description: str = ""
    scopes: list[str] = Field(default_factory=list)
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_workflow_ids: list[str] = Field(default_factory=list)
    is_active: bool = True


class AuthMachineTokenCreateRequest(BaseModel):
    label: str = ""
    name: str = ""
    expires_in_days: int | None = None
    scopes: list[str] = Field(default_factory=list)
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_workflow_ids: list[str] = Field(default_factory=list)


class AuthMachineTokenUpdateRequest(BaseModel):
    label: str = ""
    name: str = ""
    scopes: list[str] | None = None
    allowed_project_ids: list[str] | None = None
    allowed_workflow_ids: list[str] | None = None


class AuthAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account_id: str
    display_name: str = ""
    is_active: bool = True

    @field_validator("account_id")
    @classmethod
    def _normalize_account_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("account_id is required")
        return normalized

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, value: str) -> str:
        return str(value or "").strip()


class AuthAccountDisableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str = ""

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        return str(value or "").strip()


class AuthCurrentAccountUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    display_name: str

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("display_name is required")
        return normalized


class AuthPasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str

    @property
    def username(self) -> str:
        return str(self.email or "")


class AuthPasswordResetVerifyRequest(BaseModel):
    token: str


class AuthPasswordResetConfirmRequest(BaseModel):
    token: str
    new_password: str
    revoke_other_sessions: bool = True


class AuthEmailChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str

    @property
    def username(self) -> str:
        return str(self.email or "")


class AuthUserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    display_name: str = ""
    roles: list[str] = Field(default_factory=lambda: ["viewer"])
    is_active: bool = True
    must_change_password: bool = False
    account_ids: list[str] = Field(default_factory=list)
    primary_account_id: str = ""

    @property
    def username(self) -> str:
        return str(self.email or "")


class AuthUserUpdateRequest(BaseModel):
    display_name: str | None = None
    password: str | None = None
    roles: list[str] | None = None
    is_active: bool | None = None
    must_change_password: bool | None = None
    account_ids: list[str] | None = None
    primary_account_id: str | None = None


class AuthUserProfileUpdateRequest(BaseModel):
    display_name: str


# ── Extension ───────────────────────────────────────────────────────────


class ExtensionScanRequest(BaseModel):
    root: str = ""


class ExtensionEnableRequest(BaseModel):
    version: str | None = None
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    approved_browser_authority_impact_hash: str = Field(
        default="", max_length=64, pattern=_TOKEN_A_F0_9_64
    )


class ExtensionInvokeRequest(BaseModel):
    request: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int | None = None
    artifact_inputs: list[Any] = Field(
        default_factory=list
    )  # artifact IDs to resolve as read grants
    artifact_outputs: list[Any] = Field(
        default_factory=list
    )  # pre-registered artifact IDs → write grants
    # Producer path: specs auto-register new artifacts and emit write grants.
    # Each dict must contain at minimum "filename" and "content_type".
    # Optional keys: "retention_class" (default "run_window"), "step_id", "run_id",
    # "max_size_bytes" (default host artifact output cap).
    artifact_output_specs: list[dict[str, Any]] = Field(default_factory=list)
    dataset_inputs: list[Any] = Field(default_factory=list)
    dataset_outputs: list[Any] = Field(default_factory=list)
    dataset_output_specs: list[dict[str, Any]] = Field(default_factory=list)


class ExtensionQueryRequest(BaseModel):
    page_id: str = ""
    component_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int | None = None


class ExtensionActionRequest(BaseModel):
    resolution_ref: DirectEffectResolutionRef | None = None
    page_id: str = ""
    component_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    scope: str = ""
    item_ids: list[str] = Field(default_factory=list)
    timeout_seconds: int | None = None
    artifact_inputs: list[Any] = Field(default_factory=list)
    artifact_outputs: list[Any] = Field(default_factory=list)
    artifact_output_specs: list[dict[str, Any]] = Field(default_factory=list)
    dataset_inputs: list[Any] = Field(default_factory=list)
    dataset_outputs: list[Any] = Field(default_factory=list)
    dataset_output_specs: list[dict[str, Any]] = Field(default_factory=list)


# ── Commands ────────────────────────────────────────────────────────────


class CommandDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command_type: str
    target: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"
    correlation_id: str = ""

    @field_validator("command_type")
    @classmethod
    def _normalize_command_type(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("command_type is required")
        return normalized

    @field_validator("source", "correlation_id")
    @classmethod
    def _normalize_command_text_fields(cls, value: str) -> str:
        return value.strip()


class CommandBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    commands: list[CommandDispatchRequest] = Field(default_factory=list)

    @field_validator("commands")
    @classmethod
    def _validate_commands_not_empty(
        cls, value: list[CommandDispatchRequest]
    ) -> list[CommandDispatchRequest]:
        if not value:
            raise ValueError("commands must contain at least one command")
        return value


# ── Project Connections ─────────────────────────────────────────────────


class ProjectConnectionCreateRequest(BaseModel):
    extension_id: str = ""
    extension_install_id: str = ""
    connection_kind: str = "extension"
    display_name: str = ""
    connection_type: str = ""
    environment: str = ""
    runtime_mode: str | None = None
    remote_base_url: str = ""
    enabled: bool = True
    connection_config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, Any] = Field(default_factory=dict)
    connection_routes: list[dict[str, Any]] = Field(default_factory=list)
    ingress_config: dict[str, Any] = Field(default_factory=dict)
    workflow_mappings: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @field_validator("extension_id", mode="before")
    @classmethod
    def _validate_extension_id(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        return normalize_extension_id(normalized) if normalized else ""

    @field_validator("runtime_mode", mode="before")
    @classmethod
    def _validate_runtime_mode(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"local", "remote_https"}:
            raise ValueError("runtime_mode must be local or remote_https")
        return normalized

    @field_validator("secret_refs", mode="before")
    @classmethod
    def _normalize_secret_refs(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        from core.application.management.project_connection import normalize_secret_refs_payload

        return normalize_secret_refs_payload(value)


class ProjectConnectionUpdateRequest(BaseModel):
    enabled: bool | None = None
    display_name: str | None = None
    connection_type: str | None = None
    environment: str | None = None
    extension_install_id: str | None = None
    runtime_mode: str | None = None
    remote_base_url: str | None = None
    health_status: str | None = None
    runtime_status: str | None = None
    last_seen_ok_at: float | None = None
    last_error: str | None = None
    connection_config: dict[str, Any] | None = None
    secret_refs: dict[str, Any] | None = None
    connection_routes: list[dict[str, Any]] | None = None
    ingress_config: dict[str, Any] | None = None
    workflow_mappings: dict[str, str] | None = None
    tags: list[str] | None = None

    @field_validator("extension_install_id", mode="before")
    @classmethod
    def _normalize_extension_install_id(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("runtime_mode", mode="before")
    @classmethod
    def _validate_runtime_mode(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"local", "remote_https"}:
            raise ValueError("runtime_mode must be local or remote_https")
        return normalized

    @field_validator("secret_refs", mode="before")
    @classmethod
    def _normalize_secret_refs(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        from core.application.management.project_connection import normalize_secret_refs_payload

        return normalize_secret_refs_payload(value)


class ProjectConnectionSecretsSaveRequest(BaseModel):
    secrets: dict[str, str] = Field(default_factory=dict)


class ProjectConnectionOAuthStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    optional_scopes: list[str] = Field(default_factory=list)
    return_path: str = ""
    intent: str | None = None

    @field_validator("intent")
    @classmethod
    def _normalize_oauth_intent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) > 64 or not all(
            character.islower() or character.isdigit() or character in "_-"
            for character in normalized
        ):
            raise ValueError("intent must be a lowercase stable identifier")
        return normalized


class ExtensionProjectStatePutRequest(BaseModel):
    value_jsonb: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None
    expected_revision: int | None = None
    expires_at: str | None = None


# ── Extension ────────────────────────────────────────────────────────────────


class ExtensionInstallRequest(BaseModel):
    extension_id: str
    version: str = "1.0.0"
    config: dict[str, Any] = Field(default_factory=dict)


class ExtensionDescriptorValidateRequest(BaseModel):
    source_type: str = "file"  # file | url | builtin | payload
    descriptor_path: str = ""
    descriptor_url: str = ""
    descriptor_payload: dict[str, Any] = Field(default_factory=dict)
    source_root: str = ""
    platform_version: str = "1.0.0"
    allow_insecure_remote: bool = False


class ExtensionDescriptorImportFileRequest(BaseModel):
    descriptor_path: str
    install_after_import: bool = False
    runtime_mode: str = ""
    remote_base_url: str = ""


class ExtensionDescriptorImportUrlRequest(BaseModel):
    descriptor_url: str
    install_after_import: bool = False
    runtime_mode: str = ""
    remote_base_url: str = ""


class ExtensionReleaseInstallRequest(BaseModel):
    release_id: str
    runtime_mode: str = ""
    remote_base_url: str = ""
    approved_oauth_impact_hash: str = Field(default="", max_length=64, pattern=_TOKEN_A_F0_9_64)
    approved_browser_authority_impact_hash: str = Field(
        default="", max_length=64, pattern=_TOKEN_A_F0_9_64
    )
    entitlement_status: Literal[
        "trial_active",
        "active",
        "grace_period",
        "trial_expired",
        "suspended_nonpayment",
        "disabled",
    ] = "trial_active"


class ExtensionRuntimeTokenRequest(BaseModel):
    install_id: str
    connection_id: str = ""
    project_id: str | None = None
    ttl_seconds: int = 3600

    @field_validator("project_id", mode="before")
    @classmethod
    def _normalize_project_id(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("project_id must be a scalar identifier")
        return str(value).strip()


class ExtensionGeneratedJobCreateRequest(BaseModel):
    source_job_id: str | None = None
    request_payload: dict[str, Any] = Field(default_factory=dict)
    visibility: str = "private"

    @field_validator("source_job_id", mode="before")
    @classmethod
    def _normalize_source_job_id(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("source_job_id must be a scalar identifier")
        return str(value).strip()


class ExtensionListQueryRequest(BaseModel):
    q: str = ""
    state: str = ""
    status_group: str = ""
    runtime_mode: str = ""
    entitlement_status: str = ""
    origin: str = ""
    source: str = ""
    visibility: str = ""
    has_runtime_module: bool | None = None
    connected: bool | None = None


class ExtensionMarketplaceQueryRequest(BaseModel):
    q: str = ""
    category: str = ""
    tags: str = ""
    vendor: str = ""
    contains: str = ""
    channel: str = ""
    execution_mode: str = ""
    allows_self_hosted: bool | None = None
    requires_connected_control_plane: bool | None = None
    import_source: str = ""
    compatibility: str = ""
    origin: str = ""
    billing_mode: str = ""
    plan_id: str = ""
    installed: bool | None = None
    page: int = 1
    per_page: int = 20
    sort: str = "updated_desc"
    facets: bool = False


# ── MCP Connector ──────────────────────────────────────────────────────


class McpConnectorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    description: str = ""
    transport: str = "streamable_http"
    base_url: str
    auth_mode: str = "none"
    headers: dict[str, str] = Field(default_factory=dict)
    oauth_config: dict[str, Any] = Field(default_factory=dict)
    allowlisted_tools: list[str] = Field(default_factory=list)
    namespace_prefix: str = ""
    enabled: bool = True


class McpConnectorUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    description: str | None = None
    transport: str | None = None
    base_url: str | None = None
    auth_mode: str | None = None
    headers: dict[str, str] | None = None
    headers_mode: Literal["keep", "replace", "clear"] | None = None
    oauth_config: dict[str, Any] | None = None
    allowlisted_tools: list[str] | None = None
    namespace_prefix: str | None = None
    enabled: bool | None = None


class McpConnectorDeleteConfirmRequest(BaseModel):
    revision: str


class McpConnectorOAuthStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    return_path: str = ""


class McpConnectorOAuthCredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    client_secret: str

    @field_validator("client_id")
    @classmethod
    def _validate_client_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_id is required")
        return normalized

    @field_validator("client_secret")
    @classmethod
    def _validate_client_secret(cls, value: str) -> str:
        if value == "":
            raise ValueError("client_secret is required")
        # OAuth client secrets are opaque. Leading and trailing bytes are part
        # of the credential and must not be normalized by the request layer.
        return value


# ── Job ─────────────────────────────────────────────────────────────────


class JobDecisionOverrideRequest(BaseModel):
    domain: str
    override_value: Any = None
    original_value: Any = None
    reason: str
    created_by: str = "operator"
    active: bool = True
    message_id: str | int | None = None
    workflow_id: str | None = None
    step_id: str | None = None
    step_execution_id: str | None = None


class JobStepReplayRequest(BaseModel):
    message_id: str | int = ""
    correlation_id: str = ""
    requested_by: str = "operator"
    replay_mode: Literal["step_reexecute", "prompt_reinvoke"] = Field(default="step_reexecute")


class JobRetryInterruptedStepRequest(BaseModel):
    requested_by: str = "operator"


# ── Task ────────────────────────────────────────────────────────────────

_MANUAL_TASK_SUMMARY_MAX_LENGTH = 20_000


class RecoveryActionRequest(BaseModel):
    action: Literal["retry", "kill"]
    reason: str = ""
    force: bool = False


class ReportGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: Literal[
        "job_summary",
        "job_cost_breakdown",
        "activity_daily",
        "core_health_summary",
        "project_summary",
        "project_stats",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    persist: bool = True
