"""Shared protocol mapping for API-backed MCP tool functions."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from core.entrypoints.mcp.server_api_client import (
    MCP_TOOL_ROUTES,
    AppApiClient,
    AppApiError,
)
from core.entrypoints.mcp.server_caller_identity import current_caller_bearer
from mcp.server.mcpserver.exceptions import ToolError

_AUTHORING_TOOLS = frozenset(
    {
        "capability_search",
        "capability_describe",
        "workflow_examples_search",
        "workflow_authoring_schema",
        "workflow_draft_validate",
        "workflow_draft_save",
        "workflow_definition",
        "workflow_apply_changes",
        "workflow_convert_group",
    }
)
_AUTHORING_ISSUE_KEYS = frozenset(
    {"code", "path", "message", "severity", "blocking", "repair_guidance"}
)
_AUTHORING_CODE_RE = re.compile(r"^[a-z][a-z0-9_.]{0,99}$")


def _request_headers(ctx: object | None) -> dict[str, str]:
    if ctx is None:
        return {}
    try:
        request_context = getattr(ctx, "request_context", None)
        request = getattr(request_context, "request", None)
        headers = getattr(request, "headers", None)
        return {str(key).lower(): str(value) for key, value in dict(headers or {}).items()}
    except (AttributeError, TypeError, ValueError):
        return {}


def _caller_bearer() -> str:
    return current_caller_bearer()


def _payload_data(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    if "data" in payload:
        return payload["data"]
    return payload


def _project_id_from_name(client: AppApiClient, *, bearer: str, project_name: str) -> str:
    payload = _payload_data(
        client.request(
            "GET",
            "/api/projects",
            bearer=bearer,
            params={"page": 1, "per_page": 200, "search": project_name},
        )
    )
    if isinstance(payload, dict):
        rows = payload.get("projects") or payload.get("items") or []
    else:
        rows = payload
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("name") or "") != str(project_name or ""):
            continue
        project_id = str(row.get("id") or "").strip()
        if project_id:
            return project_id
    raise AppApiError(404, {"error": "Project was not found or is not available"})


def _delegation_parts(
    tool: str,
    *,
    route_method: str,
    route_path: str,
    arguments: dict[str, Any],
    project_id: str,
) -> tuple[str, str, dict[str, Any] | None, object | None, dict[str, str] | None]:
    method = route_method
    path = route_path
    params: dict[str, Any] | None = arguments if method == "GET" else None
    body: object | None = arguments if method != "GET" else None
    additional_headers: dict[str, str] | None = None

    if tool in _AUTHORING_TOOLS and params is not None:
        # httpx encodes None as an empty query value, not an absent optional field.
        params = {key: value for key, value in params.items() if value is not None}
    if tool == "project_list":
        params = {"page": 1, "per_page": 200}
    elif tool == "project_jobs":
        params = {"page": 1, "per_page": max(1, min(int(arguments.get("limit") or 10), 50))}
    elif tool in {"job_acknowledge", "job_unacknowledge", "job_restart"}:
        body = None
    elif tool == "job_approval_decide":
        body = {
            key: value
            for key, value in arguments.items()
            if key != "idempotency_key" and value is not None
        }
    elif (tool.startswith("workflow_hook_") or tool == "workflow_import") and body is not None:
        # Absent optional fields stay absent; the shared DTO owns their defaults.
        body = {key: value for key, value in arguments.items() if value is not None}
    elif tool == "workflow_trigger":
        body = {
            "project_id": project_id,
            "workflow_id": str(arguments["workflow_id"]).strip(),
            "inputs": dict(arguments.get("payload") or {}),
            "client_context": dict(arguments.get("client_context") or {}),
            "test_mode": bool(arguments.get("test_mode")),
            **({"files": list(arguments["files"])} if arguments.get("files") else {}),
        }

    return method, path, params, body, additional_headers


def _error_code(status_code: int) -> str:
    return {
        400: "invalid_request",
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_failed",
        429: "rate_limited",
        503: "dependency_unavailable",
    }.get(int(status_code), "app_api_error")


def _error_message(payload: object, status_code: int) -> str:
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"Flow Steward app API returned HTTP {status_code}"


def _authoring_error(payload: object, status_code: int) -> dict[str, Any]:
    """Keep the authoring API's safe repair contract, never arbitrary error context."""
    data = payload if isinstance(payload, dict) else {}
    nested = data.get("error")
    if not isinstance(nested, dict) and isinstance(data.get("detail"), dict):
        # Registry routes answer with the validation detail under ``detail``.
        nested = data["detail"]
    error_data = nested if isinstance(nested, dict) else data
    error: dict[str, Any] = {
        "code": _error_code(status_code),
        "message": _error_message(error_data, status_code),
        "status_code": status_code,
    }
    candidate = str(error_data.get("code") or data.get("code") or nested or "")
    if (
        candidate == _error_code(status_code)
        and isinstance(nested, str)
        and _AUTHORING_CODE_RE.fullmatch(nested)
    ):
        # The API helper adds a generic status code around legacy domain errors.
        candidate = nested
    if _AUTHORING_CODE_RE.fullmatch(candidate):
        error["code"] = candidate
    issues = error_data.get("issues")
    if isinstance(issues, list):
        error["issues"] = [
            {
                key: value
                for key, value in issue.items()
                if key in _AUTHORING_ISSUE_KEYS and isinstance(value, (str, bool))
            }
            for issue in issues[:200]
            if isinstance(issue, dict)
        ]
    workflow_id = error_data.get("workflow_id")
    if isinstance(workflow_id, str):
        error["workflow_id"] = workflow_id
    for key in ("expected_revision", "revision"):
        value = error_data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            error[key] = value
    return error


_DEPENDENCY_KEYS = ("dependency_key", "dependency_type", "ref", "status", "message")


def _blocking_dependencies(payload: object) -> list[dict[str, Any]]:
    """What a blocked import must resolve, and the actions each one allows.

    Only these fixed fields leave the API error: the caller needs them to send
    resolutions, and nothing else of the preflight context is exposed.
    """
    details = payload.get("details") if isinstance(payload, dict) else None
    context = details.get("context") if isinstance(details, dict) else None
    errors = context.get("errors") if isinstance(context, dict) else None
    rows: list[dict[str, Any]] = []
    for item in errors[:200] if isinstance(errors, list) else []:
        if not isinstance(item, dict) or not item.get("blocking"):
            continue
        row: dict[str, Any] = {
            key: item[key] for key in _DEPENDENCY_KEYS if isinstance(item.get(key), str)
        }
        actions = item.get("resolution_actions")
        if isinstance(actions, list):
            row["resolution_actions"] = [action for action in actions if isinstance(action, str)]
        rows.append(row)
    return rows


def api_tool_response(tool: str, raw_arguments: dict[str, Any]) -> str:
    """Map one registered MCP call to one authenticated application API call."""
    route = MCP_TOOL_ROUTES[tool]
    arguments = dict(raw_arguments)
    ctx = arguments.pop("ctx", None)
    bearer = _caller_bearer()
    client = AppApiClient()
    try:
        project_name = str(arguments.pop("project_name", "") or "").strip()
        if tool in {"workflow_trigger", "workflow_contract"}:
            if tool == "workflow_trigger" and "entry_key" in arguments:
                raise AppApiError(422, {"error": "MCP workflow_trigger accepts workflow_id only"})
            if not project_name or not str(arguments.get("workflow_id") or "").strip():
                raise AppApiError(422, {"error": "project_name and workflow_id are required"})
        route_path = route.path
        if tool in {"workflow_validate", "workflow_suggestions"} and not project_name:
            route_path = route.path.replace("/api/projects/{project_id}", "/api")
        needs_workflow_project = tool == "workflow_trigger"
        project_id = ""
        if "{project_id}" in route_path:
            project_id = str(arguments.pop("project_id", "") or "").strip()
            if not project_id:
                project_id = _project_id_from_name(
                    client,
                    bearer=bearer,
                    project_name=project_name,
                )
            arguments["project_id"] = project_id
        elif needs_workflow_project:
            project_id = _project_id_from_name(
                client,
                bearer=bearer,
                project_name=project_name,
            )

        path_values = {
            key: str(arguments.pop(key, "") or "").strip()
            for key in (
                "project_id",
                "workflow_id",
                "group_id",
                "hook_id",
                "job_id",
                "thread_id",
                "report_id",
            )
            if f"{{{key}}}" in route_path
        }
        path = route_path.format(**path_values)
        headers = _request_headers(ctx)
        correlation_id = str(
            headers.get("x-fs-correlation-id")
            or headers.get("x-correlation-id")
            or arguments.pop("correlation_id", "")
            or uuid4().hex
        ).strip()
        idempotency_key = str(arguments.get("idempotency_key") or "").strip()
        method, path, params, body, additional_headers = _delegation_parts(
            tool,
            route_method=route.method,
            route_path=path,
            arguments=arguments,
            project_id=project_id,
        )
        request_kwargs: dict[str, Any] = {
            "bearer": bearer,
            "params": params,
            "json_body": body,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
        }
        if additional_headers is not None:
            request_kwargs["additional_headers"] = additional_headers
        payload = client.request(method, path, **request_kwargs)
    except AppApiError as exc:
        envelope = {
            "ok": False,
            "tool": tool,
            "error": _authoring_error(exc.payload, exc.status_code)
            if tool in _AUTHORING_TOOLS
            else {
                "code": _error_code(exc.status_code),
                "message": _error_message(exc.payload, exc.status_code),
                "status_code": exc.status_code,
            },
        }
        if tool == "workflow_import":
            blocking = _blocking_dependencies(exc.payload)
            if blocking:
                envelope["error"]["blocking_dependencies"] = blocking
        raise ToolError(json.dumps(envelope, ensure_ascii=False, sort_keys=True)) from exc

    return json.dumps(
        {
            "ok": True,
            "tool": tool,
            "data": _payload_data(payload),
            "meta": {"schema_version": "v1", "correlation_id": correlation_id},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


__all__ = ["api_tool_response"]
