"""Project-domain public command specifications."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.management.command_models import (
    ProjectNotificationDispatchResultView,
)
from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.management.json_safety import validate_no_sensitive_json_keys
from core.application.management.query_models import (
    ProjectCliDetailView,
    ProjectNotificationDeliveriesView,
    ProjectNotificationRoutesView,
    ProjectsListView,
)

_TOKEN_PROJECTS_AVAILABLE = "projects.available"
_MESSAGE_PAYLOAD_MUST_BE_A_JSON_OBJECT = "payload must be a JSON object"
_TOKEN_PROJECTS_LIST = "projects.list"
_TOKEN_PROJECTS_SHOW = "projects.show"
_TOKEN_PROJECTS_NOTIFICATION_ROUTES = "projects.notification-routes"
_TOKEN_PROJECTS_NOTIFICATION_DELIVERIES = "projects.notification-deliveries"
_TOKEN_PROJECTS_DISPATCH_NOTIFICATION = "projects.dispatch-notification"


class ProjectListCommandInput(BaseModel):
    """Validated input for project list queries."""

    model_config = ConfigDict(extra="forbid")

    page: int = 1
    per_page: int = 25
    search: str = ""

    @field_validator("page")
    @classmethod
    def normalize_page(cls, value: int) -> int:
        return max(1, int(value or 1))

    @field_validator("per_page")
    @classmethod
    def normalize_per_page(cls, value: int) -> int:
        return max(1, min(int(value or 25), 200))


class ProjectIdentifierCommandInput(BaseModel):
    """Validated input for commands targeting one project."""

    model_config = ConfigDict(extra="forbid")

    project_id: str


class ProjectShowCommandInput(ProjectIdentifierCommandInput):
    """Validated input for bounded project detail queries."""

    include_schema: bool = False
    schema_page: int = 1
    schema_per_page: int = 25

    @field_validator("schema_page")
    @classmethod
    def normalize_schema_page(cls, value: int) -> int:
        return max(1, int(value or 1))

    @field_validator("schema_per_page")
    @classmethod
    def normalize_schema_per_page(cls, value: int) -> int:
        return max(1, min(int(value or 25), 200))


class ProjectNotificationRoutesCommandInput(ProjectIdentifierCommandInput):
    """Validated input for project notification route queries."""

    enabled_only: bool = False


class ProjectNotificationDeliveriesCommandInput(ProjectIdentifierCommandInput):
    """Validated input for project notification delivery queries."""

    job_id: str = ""
    limit: int = 200

    @field_validator("limit")
    @classmethod
    def normalize_limit(cls, value: int) -> int:
        return max(1, min(int(value or 200), 1000))


class ProjectDispatchNotificationCommandInput(ProjectIdentifierCommandInput):
    """Validated input for dispatching one project notification event."""

    event_type: str
    severity: str = "info"
    workflow_id: str = ""
    job_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload", mode="before")
    @classmethod
    def parse_payload(cls, value: object) -> dict[str, Any]:
        if value in (None, ""):
            return {}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(_MESSAGE_PAYLOAD_MUST_BE_A_JSON_OBJECT) from exc
            if not isinstance(parsed, dict):
                raise ValueError(_MESSAGE_PAYLOAD_MUST_BE_A_JSON_OBJECT)
            return validate_no_sensitive_json_keys(dict(parsed), field_name="payload")
        if isinstance(value, Mapping):
            return validate_no_sensitive_json_keys(dict(value), field_name="payload")
        raise ValueError(_MESSAGE_PAYLOAD_MUST_BE_A_JSON_OBJECT)


_PROJECT_ID_FIELD = FieldSpec(
    key="project_id",
    label="Project",
    type=FieldType.SINGLE_SELECT,
    required=True,
    description="Project to query or update.",
    examples=("proj_01J8K4M2Q7",),
    choice_provider=_TOKEN_PROJECTS_AVAILABLE,
    option_name="--project-id",
)

PROJECT_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_PROJECTS_LIST,
        path=("projects", "list"),
        summary="List projects.",
        description="List visible projects through the shared project query service.",
        examples=("flow-steward projects list --search api --per-page 50",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            FieldSpec(
                key="page",
                label="Page",
                type=FieldType.INTEGER,
                description="One-based results page to return.",
                examples=("2",),
                default=1,
                option_name="--page",
            ),
            FieldSpec(
                key="per_page",
                label="Per page",
                type=FieldType.INTEGER,
                description="Maximum projects to return per page, from 1 to 200.",
                examples=("50",),
                default=25,
                option_name="--per-page",
            ),
            FieldSpec(
                key="search",
                label="Search",
                type=FieldType.TEXT,
                description="Text matched against visible project records.",
                examples=("payments api",),
                default="",
                option_name="--search",
            ),
        ),
        input_model=ProjectListCommandInput,
        result_model=ProjectsListView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_PROJECTS_LIST,
        result_renderer_ref=_TOKEN_PROJECTS_LIST,
    ),
    CommandSpec(
        command_id=_TOKEN_PROJECTS_SHOW,
        path=("projects", "show"),
        summary="Show project details.",
        description="Show one project detail page through the shared project query service.",
        examples=(
            "flow-steward projects show --project-id proj_123",
            "flow-steward projects show --project-id proj_123 --include-schema --schema-per-page 25",
        ),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _PROJECT_ID_FIELD,
            FieldSpec(
                key="include_schema",
                label="Include schema",
                type=FieldType.BOOLEAN,
                description="Include a paginated page of assigned database schema resources.",
                examples=("true",),
                default=False,
                option_name="--include-schema",
            ),
            FieldSpec(
                key="schema_page",
                label="Schema page",
                type=FieldType.INTEGER,
                description="One-based schema resource page to return.",
                examples=("2",),
                default=1,
                option_name="--schema-page",
            ),
            FieldSpec(
                key="schema_per_page",
                label="Schema per page",
                type=FieldType.INTEGER,
                description="Maximum schema resources to return, from 1 to 200.",
                examples=("50",),
                default=25,
                option_name="--schema-per-page",
            ),
        ),
        input_model=ProjectShowCommandInput,
        result_model=ProjectCliDetailView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_PROJECTS_SHOW,
        result_renderer_ref=_TOKEN_PROJECTS_SHOW,
    ),
    CommandSpec(
        command_id=_TOKEN_PROJECTS_NOTIFICATION_ROUTES,
        path=("projects", "notification-routes"),
        summary="List project notification routes.",
        description="List notification routes for one project.",
        examples=("flow-steward projects notification-routes --project-id proj_123",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _PROJECT_ID_FIELD,
            FieldSpec(
                key="enabled_only",
                label="Enabled only",
                type=FieldType.BOOLEAN,
                description="Return only notification routes that are currently enabled.",
                examples=("true",),
                default=False,
                option_name="--enabled-only",
            ),
        ),
        input_model=ProjectNotificationRoutesCommandInput,
        result_model=ProjectNotificationRoutesView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_PROJECTS_NOTIFICATION_ROUTES,
        result_renderer_ref=_TOKEN_PROJECTS_NOTIFICATION_ROUTES,
    ),
    CommandSpec(
        command_id=_TOKEN_PROJECTS_NOTIFICATION_DELIVERIES,
        path=("projects", "notification-deliveries"),
        summary="List project notification deliveries.",
        description="List recent notification delivery records for one project.",
        examples=(
            "flow-steward projects notification-deliveries --project-id proj_123 --limit 50",
        ),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _PROJECT_ID_FIELD,
            FieldSpec(
                key="job_id",
                label="Job id",
                type=FieldType.TEXT,
                description="Limit deliveries to the job that emitted the notification.",
                examples=("job_01J8K5R9W3",),
                default="",
                option_name="--job-id",
            ),
            FieldSpec(
                key="limit",
                label="Limit",
                type=FieldType.INTEGER,
                description="Maximum recent delivery records to return, from 1 to 1000.",
                examples=("50",),
                default=200,
                option_name="--limit",
            ),
        ),
        input_model=ProjectNotificationDeliveriesCommandInput,
        result_model=ProjectNotificationDeliveriesView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_PROJECTS_NOTIFICATION_DELIVERIES,
        result_renderer_ref=_TOKEN_PROJECTS_NOTIFICATION_DELIVERIES,
    ),
    CommandSpec(
        command_id=_TOKEN_PROJECTS_DISPATCH_NOTIFICATION,
        path=("projects", "dispatch-notification"),
        summary="Dispatch a project notification.",
        description="Dispatch a project notification event through configured routes.",
        examples=(
            "flow-steward projects dispatch-notification --project-id proj_123 --event-type workflow.completed",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _PROJECT_ID_FIELD,
            FieldSpec(
                key="event_type",
                label="Event type",
                type=FieldType.TEXT,
                required=True,
                description="Notification event name used to match configured routes.",
                examples=("workflow.completed",),
                option_name="--event-type",
            ),
            FieldSpec(
                key="severity",
                label="Severity",
                type=FieldType.SINGLE_SELECT,
                description="Severity assigned to the notification event.",
                examples=("warning",),
                choices=(
                    ChoiceItem(value="info", label="Info"),
                    ChoiceItem(value="warning", label="Warning"),
                    ChoiceItem(value="error", label="Error"),
                ),
                default="info",
                option_name="--severity",
            ),
            FieldSpec(
                key="workflow_id",
                label="Workflow id",
                type=FieldType.RUNTIME_IDENTIFIER,
                description="Workflow associated with the event, when applicable.",
                examples=("release-report",),
                default="",
                option_name="--workflow-id",
            ),
            FieldSpec(
                key="job_id",
                label="Job id",
                type=FieldType.TEXT,
                description="Job associated with the event, when applicable.",
                examples=("job_01J8K5R9W3",),
                default="",
                option_name="--job-id",
            ),
            FieldSpec(
                key="payload",
                label="Payload",
                type=FieldType.TEXT,
                description="JSON object containing non-sensitive event-specific data.",
                examples=('{"duration_seconds": 42, "status": "succeeded"}',),
                default="{}",
                option_name="--payload",
                advanced=True,
            ),
        ),
        input_model=ProjectDispatchNotificationCommandInput,
        result_model=ProjectNotificationDispatchResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_PROJECTS_DISPATCH_NOTIFICATION,
        result_renderer_ref=_TOKEN_PROJECTS_DISPATCH_NOTIFICATION,
    ),
)
