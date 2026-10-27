"""Public API route selection and OpenAPI presentation policy."""

from __future__ import annotations

from typing import Any, ClassVar

from fastapi import FastAPI
from fastapi.routing import APIRoute

from core.entrypoints.api.public_openapi_contract import (
    apply_public_token_approval_contract,
)


class PublicOpenApiPolicy:
    """Own the deliberately small external API surface and its documentation."""

    allowlist: ClassVar[dict[tuple[str, str], tuple[str, str]]] = {
        ("POST", "/api/workflow-runs"): (
            "Workflows",
            "Start a concrete workflow for asynchronous execution.",
        ),
        ("GET", "/api/jobs"): ("Jobs", "List jobs."),
        ("GET", "/api/jobs/{job_id}"): ("Jobs", "Get job details."),
        ("GET", "/api/jobs/{job_id}/approval"): (
            "Approvals",
            "Get the current approval gate.",
        ),
        ("POST", "/api/jobs/{job_id}/cancel"): ("Jobs", "Cancel a job."),
        ("GET", "/api/events/stream"): ("Jobs", "Stream one job's public workflow state."),
        ("POST", "/api/approvals/{thread_id}"): (
            "Approvals",
            "Submit an approval decision.",
        ),
        ("GET", "/api/projects/{project_id}/workflow-catalog"): (
            "Workflows",
            "List callable workflows.",
        ),
        ("GET", "/api/projects/{project_id}/workflow-entrypoints"): (
            "Workflows",
            "List workflow entrypoints.",
        ),
        ("GET", "/api/projects/{project_id}/workflows/{workflow_id}/contract"): (
            "Workflows",
            "Get a workflow contract.",
        ),
    }
    allowed_job_query_params: ClassVar[set[str]] = {"project_id", "limit", "page", "status"}
    schema_aliases: ClassVar[dict[str, str]] = {
        "WorkflowRunInput": "WorkflowRunRequest",
        "WorkflowRunAcceptedView": "WorkflowRunResult",
        "JobsListView": "JobListResponse",
        "AttentionJobView": "JobListItem",
        "JobDetailContractView": "JobDetailResponse",
        "JobCancelAcceptedView": "JobCancellationResult",
        "ApprovalResumeAcceptedView": "ApprovalDecisionResult",
        "PublicProjectWorkflowCatalogView": "WorkflowCatalogResponse",
        "PublicWorkflowCatalogItemView": "WorkflowCatalogItem",
        "ProjectWorkflowEntrypointsView": "WorkflowEntrypointsResponse",
        "WorkflowEntrypointView": "WorkflowEntrypoint",
        "WorkflowContractResultView": "WorkflowContractResponse",
    }
    tags: ClassVar[list[dict[str, str]]] = [
        {"name": "Jobs", "description": "Observe jobs and manage safe lifecycle actions."},
        {"name": "Approvals", "description": "Decide paused workflow gates."},
        {"name": "Workflows", "description": "Discover callable workflow contracts."},
    ]
    description = (
        "Stable external integration contract for Flow Steward. Authenticate with a bearer "
        "token. `project_id` is provisioned out-of-band and is not discoverable through this schema."
    )

    def select_routes(self, app: FastAPI) -> list[APIRoute]:
        return [
            route
            for route in app.routes
            if isinstance(route, APIRoute)
            and route.include_in_schema
            and any(
                str(route.path) == path and method in (route.methods or set())
                for method, path in self.allowlist
            )
        ]

    def apply(self, schema: dict[str, Any]) -> None:
        apply_public_token_approval_contract(schema)
        for (method, path), (tag, summary) in self.allowlist.items():
            operation = schema.get("paths", {}).get(path, {}).get(method.lower())
            if not isinstance(operation, dict):
                continue
            operation["tags"] = [tag]
            operation["summary"] = summary
            operation["security"] = [{"BearerAuth": []}]
            if path == "/api/approvals/{thread_id}" and method == "POST":
                operation["description"] = (
                    "Approve or reject a paused workflow thread and durably record a recoverable "
                    "resume. Send approval_id and an explicit approved/action decision in JSON, "
                    "plus Idempotency-Key in the request header."
                )
            if path == "/api/events/stream" and method == "GET":
                self._apply_stream_contract(operation)
        jobs_operation = schema.get("paths", {}).get("/api/jobs", {}).get("get")
        if isinstance(jobs_operation, dict):
            jobs_operation["parameters"] = [
                parameter
                for parameter in (jobs_operation.get("parameters") or [])
                if str(parameter.get("name") or "") in self.allowed_job_query_params
            ]
            response = jobs_operation.get("responses", {}).get("200")
            if isinstance(response, dict):
                response.setdefault("content", {}).setdefault("application/json", {})["schema"] = {
                    "$ref": "#/components/schemas/JobsListView"
                }

    @staticmethod
    def _apply_stream_contract(operation: dict[str, Any]) -> None:
        operation["parameters"] = [
            {
                "name": "job_id",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
                "description": "Job whose durable public workflow state should be streamed.",
            },
            {
                "name": "cursor",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "maxLength": 255},
                "description": "An opaque public-state marker cursor used to reconnect.",
            },
            {
                "name": "Last-Event-ID",
                "in": "header",
                "required": False,
                "schema": {"type": "string", "maxLength": 255},
                "description": (
                    "SSE reconnect marker that takes precedence over the cursor query parameter "
                    "when both are supplied."
                ),
            },
        ]
        responses = operation.setdefault("responses", {})
        for code, description in {
            "400": "Invalid cursor or unsupported job-stream filters.",
            "403": "The token lacks project, workflow, or jobs.read access.",
            "404": "The job or its project does not exist.",
            "409": "The job has an unsupported concurrent approval state.",
            "410": "The requested cursor is expired or invalid.",
            "429": "The job-scoped SSE connection limit is reached.",
            "503": "The public workflow state stream is unavailable.",
        }.items():
            responses[code] = {
                "description": description,
                "content": {"application/json": {"schema": {"type": "object"}}},
            }
        responses["429"]["headers"] = {
            "Retry-After": {
                "schema": {"type": "integer", "minimum": 1},
                "description": "Seconds to wait before reconnecting.",
            }
        }
        for status_code, response in responses.items():
            if str(status_code).startswith("2") and isinstance(response, dict):
                response["content"] = {"text/event-stream": {"schema": {"type": "string"}}}


PUBLIC_OPENAPI_POLICY = PublicOpenApiPolicy()

__all__ = ["PUBLIC_OPENAPI_POLICY", "PublicOpenApiPolicy"]
