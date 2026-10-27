# mypy: ignore-errors
"""Shared schemas and helpers for workflow definition, run and I/O admin routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.management.query_models import (
    WorkflowDefinitionChangeResultView,
    WorkflowEditorAvailableVariablesView,
    WorkflowEditorGroupView,
    WorkflowEditorInputDefaultsView,
    WorkflowEditorIOIssueView,
    WorkflowEditorOutputView,
    WorkflowEditorSimulationResultView,
    WorkflowEditorStepContractPreviewView,
    WorkflowEditorStepView,
    WorkflowEditorTestRunResultView,
)
from core.application.registry.contract_validation import (
    format_validation_error_detail,
    validate_contract_items,
    validate_invoke_workflow_step,
)
from core.application.workflows.workflow_authoring_requests import WorkflowChangeSet
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_AGENT,
    STEP_KIND_CONNECTOR,
    STEP_KIND_INVOKE_WORKFLOW,
    SUPPORTED_STEP_KINDS,
    normalize_step_kind,
    step_requires_agent,
)

ASSESS_WHETHER_VERSION_2_4_0_IS_READY_TO_SHIP_MESSAGE = (
    "Assess whether version 2.4.0 is ready to ship."
)
PROJECT_ID_IS_REQUIRED_MESSAGE = "project_id is required"
RELEASE_READINESS_REPORT_VALUE = "Release readiness report"
VALIDATE_WORKFLOW_PATH_VALUE = "Validate workflow path"


logger = logging.getLogger(__name__)

SIMULATE_TASK_ENVELOPE_EXAMPLE = {
    "title": RELEASE_READINESS_REPORT_VALUE,
    "description": ASSESS_WHETHER_VERSION_2_4_0_IS_READY_TO_SHIP_MESSAGE,
    "project_id": "proj_42",
    "requested_workflow_id": "release-report",
    "task_type": "release_analysis",
    "inputs": {"release_version": "2.4.0"},
    "metadata": {"source": "workflow_editor"},
}

TEST_RUN_TASK_ENVELOPE_EXAMPLE = {
    "title": RELEASE_READINESS_REPORT_VALUE,
    "description": ASSESS_WHETHER_VERSION_2_4_0_IS_READY_TO_SHIP_MESSAGE,
    "project_id": "proj_42",
    "requested_workflow_id": "release-report",
    "inputs": {"release_version": "2.4.0"},
    "metadata": {"source": "workflow_test_run"},
}

STEP_TEST_RUN_TASK_ENVELOPE_EXAMPLE = {
    "title": RELEASE_READINESS_REPORT_VALUE,
    "description": ASSESS_WHETHER_VERSION_2_4_0_IS_READY_TO_SHIP_MESSAGE,
    "project_id": "proj_42",
    "requested_workflow_id": "release-report",
    "inputs": {"release_version": "2.4.0"},
    "metadata": {"source": "step_test_run"},
}

SIMULATE_RESPONSE_EXAMPLE = {
    "workflow_id": "release-report",
    "validation": {"valid": True, "errors": [], "warnings": []},
    "classification": {
        "task_type": "release_analysis",
        "effort_hint": "medium",
        "confidence": 0.92,
        "risk_level": "moderate",
        "automation_recommendation": "auto_run",
        "reasoning": "The request matches the release-report workflow and has explicit inputs.",
    },
    "steps": [
        {
            "step_id": "draft_release_notes",
            "step_kind": "agent",
            "group_id": None,
            "lane_id": None,
            "participant_key": "writer",
            "agent": "Release Writer",
            "agent_id": "release_writer",
            "skill_id": "draft_release_notes",
            "workflow_id": "release-report",
            "optional": False,
        }
    ],
}

TEST_RUN_RESPONSE_EXAMPLE = {
    "status": "dry_run",
    "dry_run": True,
    "workflow_id": "release-report",
    "project_id": "proj_42",
    "task_id": "task_123",
    "task_summary": VALIDATE_WORKFLOW_PATH_VALUE,
    "test_mode": True,
}

SIMULATE_REQUEST_BODY = Body(examples=[{"task_envelope": SIMULATE_TASK_ENVELOPE_EXAMPLE}])

TEST_RUN_REQUEST_BODY = Body(
    examples=[
        {
            "project_id": "proj_42",
            "task_summary": VALIDATE_WORKFLOW_PATH_VALUE,
            "task_id": "task_123",
            "mode": "dry_run",
            "dry_run": True,
            "task_envelope": TEST_RUN_TASK_ENVELOPE_EXAMPLE,
        }
    ]
)

STEP_TEST_RUN_REQUEST_BODY = Body(
    examples=[
        {
            "project_id": "proj_42",
            "task_summary": "Validate workflow step",
            "task_id": "task_123",
            "task_envelope": STEP_TEST_RUN_TASK_ENVELOPE_EXAMPLE,
        }
    ]
)


class SimulateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"task_envelope": SIMULATE_TASK_ENVELOPE_EXAMPLE}}
    )

    task_envelope: dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra={"example": SIMULATE_TASK_ENVELOPE_EXAMPLE},
    )


class TestRunRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "project_id": "proj_42",
                "task_summary": VALIDATE_WORKFLOW_PATH_VALUE,
                "task_id": "task_123",
                "mode": "dry_run",
                "dry_run": True,
                "task_envelope": TEST_RUN_TASK_ENVELOPE_EXAMPLE,
            }
        }
    )

    project_id: str
    task_summary: str = Field(min_length=1)
    task_id: str = ""
    mode: str = ""
    dry_run: bool = False
    task_envelope: dict[str, Any] | None = Field(
        default=None,
        json_schema_extra={"example": TEST_RUN_TASK_ENVELOPE_EXAMPLE},
    )
    expected_revision: int | None = Field(
        default=None,
        ge=1,
        description="The revision the builder shows; a changed workflow is a 409, not a run.",
    )

    @field_validator("project_id", mode="before")
    @classmethod
    def _normalize_project_id(cls, value: Any) -> str:
        if value is None:
            raise ValueError(PROJECT_ID_IS_REQUIRED_MESSAGE)
        if isinstance(value, (bool, list, tuple, set, dict)):
            raise ValueError("project_id must be a scalar identifier")
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(PROJECT_ID_IS_REQUIRED_MESSAGE)
        return normalized


class StepTestRunRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "project_id": "proj_42",
                "task_summary": "Validate workflow step",
                "task_id": "task_123",
                "task_envelope": STEP_TEST_RUN_TASK_ENVELOPE_EXAMPLE,
            }
        }
    )

    project_id: str
    task_summary: str = Field(min_length=1)
    task_id: str = ""
    task_envelope: dict[str, Any] | None = Field(
        default=None,
        json_schema_extra={"example": STEP_TEST_RUN_TASK_ENVELOPE_EXAMPLE},
    )
    expected_revision: int | None = Field(
        default=None,
        ge=1,
        description="The revision the builder shows; a changed workflow is a 409, not a run.",
    )

    @field_validator("project_id", mode="before")
    @classmethod
    def _normalize_step_project_id(cls, value: Any) -> str:
        if value is None:
            raise ValueError(PROJECT_ID_IS_REQUIRED_MESSAGE)
        if isinstance(value, (bool, list, tuple, set, dict)):
            raise ValueError("project_id must be a scalar identifier")
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(PROJECT_ID_IS_REQUIRED_MESSAGE)
        return normalized


class TransformPreviewRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    sample_data: Any = Field(default_factory=dict)


def _test_run_task_envelope(
    workflow_id: str,
    project_id: str,
    task_summary: str,
    envelope: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(envelope, dict) and envelope:
        payload = dict(envelope)
        payload.setdefault("project_id", project_id)
        payload.setdefault("requested_workflow_id", workflow_id)
        payload.setdefault("title", task_summary)
        payload.setdefault("description", task_summary)
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.setdefault("source", "workflow_test_run")
        metadata.setdefault("requested_workflow_id", workflow_id)
        payload["metadata"] = metadata
        inputs = payload.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {}
        inputs.setdefault("project_id", project_id)
        payload["inputs"] = inputs
        return payload
    return {
        "title": task_summary,
        "description": task_summary,
        "project_id": project_id,
        "requested_workflow_id": workflow_id,
        "inputs": {"project_id": project_id},
        "metadata": {
            "source": "workflow_test_run",
            "requested_workflow_id": workflow_id,
        },
    }


__all__ = [
    "ASSESS_WHETHER_VERSION_2_4_0_IS_READY_TO_SHIP_MESSAGE",
    "PROJECT_ID_IS_REQUIRED_MESSAGE",
    "RELEASE_READINESS_REPORT_VALUE",
    "SIMULATE_REQUEST_BODY",
    "SIMULATE_RESPONSE_EXAMPLE",
    "SIMULATE_TASK_ENVELOPE_EXAMPLE",
    "STEP_KIND_AGENT",
    "STEP_KIND_CONNECTOR",
    "STEP_KIND_INVOKE_WORKFLOW",
    "STEP_TEST_RUN_REQUEST_BODY",
    "STEP_TEST_RUN_TASK_ENVELOPE_EXAMPLE",
    "SUPPORTED_STEP_KINDS",
    "TEST_RUN_REQUEST_BODY",
    "TEST_RUN_RESPONSE_EXAMPLE",
    "TEST_RUN_TASK_ENVELOPE_EXAMPLE",
    "VALIDATE_WORKFLOW_PATH_VALUE",
    "APIRouter",
    "BaseModel",
    "Body",
    "ConfigDict",
    "Field",
    "HTTPException",
    "Query",
    "Request",
    "SimulateRequest",
    "StepTestRunRequest",
    "TestRunRequest",
    "TransformPreviewRequest",
    "WorkflowChangeSet",
    "WorkflowDefinitionChangeResultView",
    "WorkflowEditorAvailableVariablesView",
    "WorkflowEditorGroupView",
    "WorkflowEditorIOIssueView",
    "WorkflowEditorInputDefaultsView",
    "WorkflowEditorOutputView",
    "WorkflowEditorSimulationResultView",
    "WorkflowEditorStepContractPreviewView",
    "WorkflowEditorStepView",
    "WorkflowEditorTestRunResultView",
    "_test_run_task_envelope",
    "field_validator",
    "format_validation_error_detail",
    "logger",
    "normalize_step_kind",
    "step_requires_agent",
    "validate_contract_items",
    "validate_invoke_workflow_step",
]
