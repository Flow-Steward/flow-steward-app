"""Report-domain public command specifications."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.management.json_safety import validate_no_sensitive_json_keys
from core.application.management.query_models import ViewModel

_MESSAGE_VALUE_MUST_BE_A_JSON_OBJECT = "value must be a JSON object"
_TOKEN_REPORTS_GENERATE = "reports.generate"
_TOKEN_REPORTS_GET = "reports.get"
_TOKEN_REPORTS_DIAGNOSTICS = "reports.diagnostics"

REPORT_TYPE_CHOICES = (
    ChoiceItem(value="job_summary", label="Job summary"),
    ChoiceItem(value="job_cost_breakdown", label="Job cost breakdown"),
    ChoiceItem(value="activity_daily", label="Daily activity"),
    ChoiceItem(value="core_health_summary", label="Core health summary"),
    ChoiceItem(value="project_summary", label="Project summary"),
    ChoiceItem(value="project_stats", label="Project stats"),
)


class ReportGenerateCommandInput(BaseModel):
    """Validated input for report generation."""

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

    @field_validator("params", mode="before")
    @classmethod
    def parse_params(cls, value: object) -> dict[str, Any]:
        return validate_no_sensitive_json_keys(_json_object(value), field_name="params")


class ReportGetCommandInput(BaseModel):
    """Validated input for fetching a persisted report."""

    model_config = ConfigDict(extra="forbid")

    report_id: str

    @field_validator("report_id")
    @classmethod
    def require_report_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("report_id is required")
        return normalized


class ReportDiagnosticsCommandInput(BaseModel):
    """Validated input for the installation diagnostic report.

    ``host`` and ``doctor`` are facts only the installation host can see; the
    launcher collects them there and hands them in through ``--input-json -``.
    """

    model_config = ConfigDict(extra="forbid")

    host: dict[str, Any] | None = None
    doctor: dict[str, Any] | None = None

    @field_validator("host", "doctor", mode="before")
    @classmethod
    def parse_object(cls, value: object) -> dict[str, Any] | None:
        if value in (None, ""):
            return None
        return _json_object(value)


class SystemDiagnosticReportView(ViewModel):
    """The redacted installation report, as one JSON document."""

    report: dict[str, Any] = Field(default_factory=dict)


class ReportResultView(ViewModel):
    """Structured report payload returned by generate/get."""

    id: str | None = None
    report_type: str = ""
    status: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    summary_text: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    chart_payload: dict[str, Any] = Field(default_factory=dict)
    directives: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    generated_by: str | None = None


REPORT_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_REPORTS_GENERATE,
        path=("reports", "generate"),
        summary="Generate a report.",
        description="Generate an operator report through the shared reporting service.",
        examples=(
            'flow-steward reports generate activity_daily --params \'{"project_id": "proj_123"}\'',
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="report_type",
                label="Report type",
                type=FieldType.SINGLE_SELECT,
                required=True,
                choices=REPORT_TYPE_CHOICES,
                positional=True,
            ),
            FieldSpec(
                key="params",
                label="Params",
                type=FieldType.TEXT,
                default={},
                option_name="--params",
                description="JSON object passed to the report generator.",
            ),
            FieldSpec(
                key="persist",
                label="Persist",
                type=FieldType.BOOLEAN,
                default=True,
                option_name="--persist",
            ),
        ),
        input_model=ReportGenerateCommandInput,
        result_model=ReportResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_REPORTS_GENERATE,
        result_renderer_ref=_TOKEN_REPORTS_GENERATE,
    ),
    CommandSpec(
        command_id=_TOKEN_REPORTS_GET,
        path=("reports", "get"),
        summary="Get a persisted report.",
        description="Fetch one persisted report by id through the shared reporting service.",
        examples=("flow-steward reports get report_123",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            FieldSpec(
                key="report_id",
                label="Report id",
                type=FieldType.RUNTIME_IDENTIFIER,
                required=True,
                positional=True,
            ),
        ),
        input_model=ReportGetCommandInput,
        result_model=ReportResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_REPORTS_GET,
        result_renderer_ref=_TOKEN_REPORTS_GET,
    ),
    CommandSpec(
        command_id=_TOKEN_REPORTS_DIAGNOSTICS,
        path=("reports", "diagnostics"),
        summary="Write the installation diagnostic report.",
        description=(
            "Describe this installation for a bug report: version and images, deployment "
            "profile, CPU architecture and resources, database and broker versions, services "
            "and their health, installed extensions and customised settings. Secrets, logs "
            "and job content are never included. On the host, ./flow-steward adds Docker and "
            "doctor facts and saves the report to a file."
        ),
        examples=("flow-steward reports diagnostics",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            FieldSpec(
                key="host",
                label="Host facts",
                type=FieldType.TEXT,
                advanced=True,
                description="JSON object of installation-host facts, supplied by the launcher.",
                examples=('{"platform": {"system": "Linux", "machine": "aarch64"}}',),
            ),
            FieldSpec(
                key="doctor",
                label="Doctor report",
                type=FieldType.TEXT,
                advanced=True,
                description="JSON object of `flow-steward doctor --json`, supplied by the launcher.",
                examples=('{"schema": "flow-steward.doctor.v1", "exit_code": 0}',),
            ),
        ),
        input_model=ReportDiagnosticsCommandInput,
        result_model=SystemDiagnosticReportView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_REPORTS_DIAGNOSTICS,
        result_renderer_ref=_TOKEN_REPORTS_DIAGNOSTICS,
        accepts_input_json=True,
    ),
)


def _json_object(value: object) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(_MESSAGE_VALUE_MUST_BE_A_JSON_OBJECT) from exc
        if not isinstance(parsed, dict):
            raise ValueError(_MESSAGE_VALUE_MUST_BE_A_JSON_OBJECT)
        return dict(parsed)
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(_MESSAGE_VALUE_MUST_BE_A_JSON_OBJECT)
