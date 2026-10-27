"""Thin HTTP transport from MCP tool adapters to the canonical app API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict, cast

import httpx

from core.entrypoints.mcp.server_api_auth import app_api_base_url
from core.infrastructure.mcp_dev_proxy import MCP_DEV_AUTH_HEADER, mcp_dev_proxy_enabled


@dataclass(frozen=True, slots=True)
class AppApiRoute:
    method: str
    path: str


class _RequestKwargs(TypedDict, total=False):
    headers: dict[str, str]
    params: dict[str, Any]
    json: object


def _route(method: str, path: str) -> AppApiRoute:
    return AppApiRoute(method=method, path=path)


MCP_TOOL_ROUTES: dict[str, AppApiRoute] = {
    "capability_search": _route(
        "GET", "/api/projects/{project_id}/workflow-authoring/capabilities"
    ),
    "capability_describe": _route(
        "GET", "/api/projects/{project_id}/workflow-authoring/capability"
    ),
    "workflow_examples_search": _route(
        "GET", "/api/projects/{project_id}/workflow-authoring/examples"
    ),
    "workflow_authoring_schema": _route("GET", "/api/workflow-authoring/schema"),
    "workflow_draft_validate": _route(
        "POST", "/api/projects/{project_id}/workflow-authoring/validate"
    ),
    "workflow_draft_save": _route("POST", "/api/projects/{project_id}/workflow-authoring/drafts"),
    "workflow_definition": _route("GET", "/api/workflows/{workflow_id}"),
    "workflow_convert_group": _route(
        "POST", "/api/workflows/{workflow_id}/groups/{group_id}/convert-to-child"
    ),
    "workflow_apply_changes": _route("POST", "/api/workflows/{workflow_id}/changes"),
    "job_list": _route("GET", "/api/jobs"),
    "job_approvals_pending": _route("GET", "/api/jobs/awaiting-approval"),
    "job_result": _route("GET", "/api/jobs/{job_id}/result"),
    "job_status": _route("GET", "/api/jobs/{job_id}/status"),
    "job_runtime_journal": _route("GET", "/api/jobs/{job_id}/runtime-journal"),
    "job_decision_overrides": _route("GET", "/api/jobs/{job_id}/decision-overrides"),
    "job_errors": _route("GET", "/api/jobs/{job_id}/errors"),
    "job_trace": _route("GET", "/api/jobs/{job_id}/trace"),
    "job_incident": _route("GET", "/api/jobs/{job_id}/incident"),
    "job_cost_breakdown": _route("GET", "/api/jobs/{job_id}/cost-breakdown"),
    "job_events": _route("GET", "/api/jobs/{job_id}/events"),
    "job_messages": _route("GET", "/api/jobs/{job_id}/messages"),
    "job_logs": _route("GET", "/api/jobs/{job_id}/logs"),
    "job_approval": _route("GET", "/api/jobs/{job_id}/approval"),
    "job_approvals": _route("GET", "/api/jobs/{job_id}/approvals"),
    "job_cancel": _route("POST", "/api/jobs/{job_id}/cancel"),
    "job_acknowledge": _route("POST", "/api/jobs/{job_id}/acknowledge"),
    "job_unacknowledge": _route("POST", "/api/jobs/{job_id}/unacknowledge"),
    "job_restart": _route("POST", "/api/jobs/{job_id}/restart"),
    "job_approval_decide": _route("POST", "/api/approvals/{thread_id}"),
    "project_list": _route("GET", "/api/projects"),
    "project_jobs": _route("GET", "/api/projects/{project_id}/jobs"),
    "workflow_list": _route("GET", "/api/projects/{project_id}/workflow-catalog"),
    "workflow_validate": _route(
        "GET", "/api/projects/{project_id}/workflows/{workflow_id}/validate"
    ),
    "workflow_contract": _route(
        "GET", "/api/projects/{project_id}/workflows/{workflow_id}/contract"
    ),
    "workflow_suggestions": _route(
        "GET", "/api/projects/{project_id}/workflows/{workflow_id}/suggestions"
    ),
    "workflow_hooks_list": _route(
        "GET", "/api/projects/{project_id}/workflows/{workflow_id}/hooks"
    ),
    "workflow_hook_get": _route(
        "GET", "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}"
    ),
    "workflow_hook_preview": _route(
        "POST", "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}/preview"
    ),
    "workflow_hook_bind": _route(
        "POST", "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}/bind"
    ),
    "workflow_hook_unbind": _route(
        "POST", "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}/unbind"
    ),
    "workflow_hook_create_and_bind": _route(
        "POST", "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}/create-and-bind"
    ),
    "workflow_where_used": _route(
        "GET", "/api/projects/{project_id}/workflows/{workflow_id}/where-used"
    ),
    "workflow_export": _route("GET", "/api/projects/{project_id}/workflows/{workflow_id}/export"),
    "workflow_import": _route("POST", "/api/projects/{project_id}/workflow-imports"),
    "workflow_stats": _route("GET", "/api/projects/{project_id}/workflows/{workflow_id}/stats"),
    "workflow_trigger": _route("POST", "/api/workflow-runs"),
    "report_generate": _route("POST", "/api/reports/generate"),
    "report_get": _route("GET", "/api/reports/{report_id}"),
    "project_notification_routes": _route("GET", "/api/projects/{project_id}/notification-routes"),
    "project_notification_deliveries": _route(
        "GET", "/api/projects/{project_id}/notification-deliveries"
    ),
    "dispatch_project_notification": _route(
        "POST", "/api/projects/{project_id}/notifications/dispatch"
    ),
}


class AppApiError(RuntimeError):
    """An application API response that could not be mapped as a tool success."""

    def __init__(self, status_code: int, payload: object) -> None:
        super().__init__(f"Flow Steward app API returned HTTP {status_code}")
        self.status_code = int(status_code)
        self.payload = payload


class AppApiClient:
    """Bounded request client that never substitutes caller credentials."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        allow_anonymous_dev: bool | None = None,
    ) -> None:
        self._base_url = str(base_url or app_api_base_url()).rstrip("/")
        self._transport = transport
        self._timeout = httpx.Timeout(timeout_seconds)
        self._allow_anonymous_dev = (
            mcp_dev_proxy_enabled() if allow_anonymous_dev is None else bool(allow_anonymous_dev)
        )

    def _request_kwargs(
        self,
        *,
        bearer: str,
        params: dict[str, Any] | None,
        json_body: object | None,
        correlation_id: str,
        idempotency_key: str,
        additional_headers: dict[str, str] | None = None,
    ) -> _RequestKwargs:
        normalized_bearer = str(bearer or "").strip()
        if not normalized_bearer and not self._allow_anonymous_dev:
            raise ValueError("caller bearer is required for app API delegation")
        headers = {"Accept": "application/json"}
        if normalized_bearer:
            headers["Authorization"] = f"Bearer {normalized_bearer}"
        else:
            headers[MCP_DEV_AUTH_HEADER] = "1"
        if str(correlation_id or "").strip():
            headers["X-Correlation-ID"] = str(correlation_id).strip()
        if str(idempotency_key or "").strip():
            headers["Idempotency-Key"] = str(idempotency_key).strip()
        for key, value in dict(additional_headers or {}).items():
            normalized = str(key or "").strip()
            if normalized and normalized.lower() not in {
                "authorization",
                "cookie",
                "host",
                MCP_DEV_AUTH_HEADER.lower(),
            }:
                headers[normalized] = str(value)
        request_kwargs: _RequestKwargs = {"headers": headers, "params": params or {}}
        if json_body is not None:
            request_kwargs["json"] = json_body
        return request_kwargs

    @staticmethod
    def _response_payload(response: httpx.Response) -> object:
        try:
            payload: object = response.json()
        except ValueError:
            payload = {"error": response.text[:2000]}
        if response.is_error:
            raise AppApiError(response.status_code, payload)
        return payload

    def request(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        params: dict[str, Any] | None = None,
        json_body: object | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
        additional_headers: dict[str, str] | None = None,
    ) -> object:
        request_kwargs = self._request_kwargs(
            bearer=bearer,
            params=params,
            json_body=json_body,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            additional_headers=additional_headers,
        )
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=cast(httpx.BaseTransport | None, self._transport),
            ) as client:
                response = client.request(str(method).upper(), path, **request_kwargs)
        except httpx.HTTPError as exc:
            raise AppApiError(503, {"error": type(exc).__name__}) from exc
        return self._response_payload(response)

    async def request_async(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        params: dict[str, Any] | None = None,
        json_body: object | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
        additional_headers: dict[str, str] | None = None,
    ) -> object:
        request_kwargs = self._request_kwargs(
            bearer=bearer,
            params=params,
            json_body=json_body,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            additional_headers=additional_headers,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=cast(httpx.AsyncBaseTransport | None, self._transport),
            ) as client:
                response = await client.request(str(method).upper(), path, **request_kwargs)
        except httpx.HTTPError as exc:
            raise AppApiError(503, {"error": type(exc).__name__}) from exc
        return self._response_payload(response)


__all__ = ["MCP_TOOL_ROUTES", "AppApiClient", "AppApiError", "AppApiRoute"]
