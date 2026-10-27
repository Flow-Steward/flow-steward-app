"""LangChain-compatible wrappers that proxy to the Flow Steward MCP server."""

from __future__ import annotations

import logging
import time
from typing import Annotated

import httpx
from langchain_core.tools import tool
from pydantic import Field

from core.application.orchestration.tools import mcp_tools_client as _client
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)

_MCP_PRINCIPAL = _client._MCP_PRINCIPAL
_MCP_URL = _client._MCP_URL
_HEADERS = _client._HEADERS
_APP_BASE_URL = _client._APP_BASE_URL
_VERIFY_SSL = _client._VERIFY_SSL
_MCP_BEARER_TOKEN_CACHE = _client._MCP_BEARER_TOKEN_CACHE
_CLIENT_BOOTSTRAP_SESSION_TOKEN = _client._bootstrap_fs_session_token
_CLIENT_SEED_BOOTSTRAP_USER = _client._seed_local_auth_bootstrap_user
_CLIENT_BOOTSTRAP_SESSION_TOKEN_HTTP = _client._bootstrap_fs_session_token_via_http
_CLIENT_FS_MCP_BEARER_TOKEN = _client._fs_mcp_bearer_token
_CLIENT_MODERN_MCP_CALL = _client._call_modern_mcp_endpoint

_CALLER_AUTH_REQUIRED_MCP_TOOLS = {
    "project_list",
    "project_jobs",
    "workflow_list",
    "workflow_validate",
    "workflow_suggestions",
    "workflow_trigger",
    "job_status",
    "job_result",
    "job_runtime_journal",
    "job_events",
    "job_messages",
    "job_logs",
    "job_cost_breakdown",
    "job_approval",
    "job_cancel",
    "job_approval_decide",
    "report_generate",
    "report_get",
    "project_notification_routes",
    "project_notification_deliveries",
    "dispatch_project_notification",
}


def _sync_client_state() -> None:
    _client._MCP_URL = _MCP_URL
    _client._HEADERS = _HEADERS
    _client._APP_BASE_URL = _APP_BASE_URL
    _client._VERIFY_SSL = _VERIFY_SSL
    _client._MCP_BEARER_TOKEN_CACHE = _MCP_BEARER_TOKEN_CACHE
    _client.httpx = httpx
    _client._get_platform_auth_logger = _get_platform_auth_logger
    _client._resolve_backend_admin_creds = _resolve_backend_admin_creds
    _client._bootstrap_fs_session_token = _bootstrap_fs_session_token
    _client._seed_local_auth_bootstrap_user = _seed_local_auth_bootstrap_user
    _client._bootstrap_fs_session_token_via_http = _bootstrap_fs_session_token_via_http
    _client._headers_for_url = _headers_for_url
    _client._fs_mcp_bearer_token = _fs_mcp_bearer_token
    _client._reset_fs_mcp_bearer_token_cache = _reset_fs_mcp_bearer_token_cache
    _client._call_modern_mcp_endpoint = _CLIENT_MODERN_MCP_CALL


def _sync_module_state() -> None:
    global _MCP_BEARER_TOKEN_CACHE
    _MCP_BEARER_TOKEN_CACHE = _client._MCP_BEARER_TOKEN_CACHE


def set_mcp_principal(principal_id: str) -> None:
    """Bind the current principal id to the active execution context."""
    _client.set_mcp_principal(principal_id)


_build_local_url = _client._build_local_url
_safe_json_len = _client._safe_json_len
_redact_mcp_observability_text = _client._redact_mcp_observability_text
_normalize_mcp_principal_job_id = _client._normalize_mcp_principal_job_id
_resolve_backend_admin_creds = _client._resolve_backend_admin_creds
_normalize_runtime_base_url = _client._normalize_runtime_base_url
_get_platform_auth_logger = _client._get_platform_auth_logger
_log_mcp_tool_observability = _client._log_mcp_tool_observability
_json_compact = _client._json_compact
_unwrap_platform_mcp_result = _client._unwrap_platform_mcp_result


def _candidate_platform_base_urls() -> list[str]:
    _sync_client_state()
    return _client._candidate_platform_base_urls()


def _bootstrap_fs_session_token(username: str, password: str) -> str:
    _sync_client_state()
    return _CLIENT_BOOTSTRAP_SESSION_TOKEN(username, password)


def _seed_local_auth_bootstrap_user(
    username: str, password: str, *, display_name: str = ""
) -> bool:
    _sync_client_state()
    return _CLIENT_SEED_BOOTSTRAP_USER(username, password, display_name=display_name)


def _bootstrap_fs_session_token_via_http(username: str, password: str) -> str:
    _sync_client_state()
    return _CLIENT_BOOTSTRAP_SESSION_TOKEN_HTTP(username, password)


def _candidate_platform_mcp_urls() -> list[str]:
    _sync_client_state()
    return _client._candidate_platform_mcp_urls()


def _clear_fs_mcp_bearer_token_cache() -> None:
    global _MCP_BEARER_TOKEN_CACHE
    _client._clear_fs_mcp_bearer_token_cache()
    _MCP_BEARER_TOKEN_CACHE = _client._MCP_BEARER_TOKEN_CACHE


def _reset_fs_mcp_bearer_token_cache() -> None:
    _clear_fs_mcp_bearer_token_cache()


def _fs_mcp_bearer_token(*, force_refresh: bool = False) -> str:
    _sync_client_state()
    try:
        return _CLIENT_FS_MCP_BEARER_TOKEN(force_refresh=force_refresh)
    finally:
        _sync_module_state()


def _explicit_fs_mcp_bearer_token() -> str:
    return str(
        _runtime_env.get("MCP_BEARER_TOKEN") or _runtime_env.get("FS_SESSION_TOKEN") or ""
    ).strip()


def _headers_for_url(
    url: str,
    *,
    force_refresh: bool = False,
    include_platform_auth: bool | None = None,
) -> dict[str, str]:
    headers = dict(_HEADERS)
    should_include_auth = (
        (url == _MCP_URL) if include_platform_auth is None else bool(include_platform_auth)
    )
    if should_include_auth:
        bearer = _fs_mcp_bearer_token(force_refresh=force_refresh)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _call_mcp_endpoint(
    url: str,
    tool_name: str,
    arguments: dict,
    *,
    include_platform_auth: bool | None = None,
) -> str:
    """Send a tools/call JSON-RPC request to an MCP endpoint and return the text result."""
    _sync_client_state()
    try:
        return _client._call_mcp_endpoint(
            url,
            tool_name,
            arguments,
            include_platform_auth=include_platform_auth,
        )
    finally:
        _sync_module_state()


def _call_platform_mcp(tool_name: str, arguments: dict) -> str:
    # Rate-limit guard — check before dispatching; record on success.
    principal = _MCP_PRINCIPAL.get()
    try:
        from core.application.orchestration.tools.mcp_rate_limit import (
            check_mcp_rate_limit,
            record_mcp_call,
        )

        allowed, retry_after = check_mcp_rate_limit(principal, tool_name)
        if not allowed:
            raise RuntimeError(
                f"MCP rate limit exceeded for principal '{principal}' tool '{tool_name}'. "
                f"Retry after {retry_after}s."
            )
    except ImportError:
        pass

    candidates = _candidate_platform_mcp_urls()
    if not candidates:
        raise RuntimeError("Flow Steward MCP endpoint is not configured")
    if tool_name in _CALLER_AUTH_REQUIRED_MCP_TOOLS and not _explicit_fs_mcp_bearer_token():
        raise RuntimeError(
            f"Explicit MCP bearer token is required for tool '{tool_name}'. "
            "Set MCP_BEARER_TOKEN or FS_SESSION_TOKEN for caller-scoped platform actions."
        )

    # Every entry below is a JSON-RPC tools/call. A transport failure does not
    # prove whether the remote tool ran, so trying a second endpoint could
    # duplicate an external effect. Endpoint selection is therefore one-shot;
    # discovery/list lifecycle retries live in the MCP protocol client instead.
    result = _call_mcp_endpoint(candidates[0], tool_name, arguments, include_platform_auth=True)

    try:
        from core.application.orchestration.tools.mcp_rate_limit import record_mcp_call

        record_mcp_call(principal, tool_name)
    except ImportError:
        pass

    return result


def verify_worker_mcp_grounding(
    *,
    retries: int = 1,
    delay_seconds: float = 0.0,
    retry_rejected: bool = False,
) -> None:
    """Fail worker startup unless MCP is reachable with project-read authority."""
    token = str(_runtime_env.get("MCP_BEARER_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "An explicit worker MCP machine token is required; configure "
            "FS_WORKER_MCP_BEARER_TOKEN with projects.read only."
        )
    last_error: Exception | None = None
    rejected = False
    for attempt in range(max(1, int(retries))):
        try:
            _call_platform_mcp("project_list", {})
            return
        except Exception as exc:
            last_error = exc
            message = str(exc or "").lower()
            if any(marker in message for marker in ("401", "403", "unauthorized", "forbidden")):
                rejected = True
                if not retry_rejected:
                    break
            if attempt + 1 < max(1, int(retries)) and delay_seconds > 0:
                time.sleep(delay_seconds)
    # The loop already knows whether the token was rejected or the server never
    # answered. Reporting a credential problem either way sends the operator to
    # look at scopes and revocation when the real cause can be that fs-mcp is not
    # serving at all — for example when it refuses to start because its image
    # revision does not match the deployment. Say which one happened, and never
    # swallow the underlying error.
    if rejected:
        raise RuntimeError(
            f"Worker MCP project-read readiness failed: {_MCP_URL} rejected the worker "
            "machine token. It is revoked, invalid, or lacks projects.read. "
            f"Underlying error: {last_error}"
        ) from last_error
    raise RuntimeError(
        f"Worker MCP project-read readiness failed: {_MCP_URL} did not answer. The token "
        "was NOT rejected, so this is not a credential problem — check that the MCP "
        "service is running and that its build revision matches this deployment. "
        f"Underlying error: {last_error}"
    ) from last_error


# ---------------------------------------------------------------------------
# Project query tools — @tool (for LLM bind_tools / .invoke())
# ---------------------------------------------------------------------------


@tool
def project_list() -> str:
    """List all Flow Steward projects with names, IDs, and job counts."""
    return _call_platform_mcp("project_list", {})


@tool
def project_jobs(project_name: str, limit: int = 10) -> str:
    """List recent orchestrator jobs for a project."""
    return _call_platform_mcp(
        "project_jobs",
        {
            "project_name": project_name,
            "limit": limit,
        },
    )


@tool
def workflow_list(
    project_name: str,
    enabled_only: bool = True,
    include_validation: bool = False,
) -> str:
    """List workflow bindings available to the current project scope."""
    return _call_platform_mcp(
        "workflow_list",
        {
            "project_name": project_name,
            "enabled_only": enabled_only,
            "include_validation": include_validation,
        },
    )


@tool
def workflow_validate(workflow_id: str, project_name: str = "") -> str:
    """Validate a workflow contract and return normalized issues."""
    return _call_platform_mcp(
        "workflow_validate",
        {"workflow_id": workflow_id, "project_name": project_name},
    )


@tool
def workflow_suggestions(workflow_id: str, project_name: str = "") -> str:
    """Get actionable repair suggestions for a workflow contract."""
    return _call_platform_mcp(
        "workflow_suggestions",
        {"workflow_id": workflow_id, "project_name": project_name},
    )


@tool
def workflow_trigger(
    project_name: str,
    workflow_id: str,
    payload: dict | None = None,
    client_context: dict | None = None,
    test_mode: bool = False,
    files: list[dict[str, object]] | None = None,
) -> str:
    """Trigger a workflow through the MCP workflow-as-a-tool surface."""
    return _call_platform_mcp(
        "workflow_trigger",
        {
            "project_name": project_name,
            "workflow_id": workflow_id,
            "payload": dict(payload or {}),
            "client_context": dict(client_context or {}),
            "test_mode": test_mode,
            "files": list(files or []),
        },
    )


@tool
def job_status(job_id: str) -> str:
    """Get the current status and runtime metadata for a job."""
    return _call_platform_mcp("job_status", {"job_id": job_id})


@tool
def job_result(
    job_id: str,
    include_messages_limit: Annotated[int, Field(ge=1, le=2000)] = 100,
    include_logs_limit: Annotated[int, Field(ge=1, le=2000)] = 50,
) -> str:
    """Get a normalized final result snapshot for a job."""
    return _call_platform_mcp(
        "job_result",
        {
            "job_id": job_id,
            "include_messages_limit": include_messages_limit,
            "include_logs_limit": include_logs_limit,
        },
    )


@tool
def job_runtime_journal(job_id: str) -> str:
    """Get runtime journal entries for a job."""
    return _call_platform_mcp("job_runtime_journal", {"job_id": job_id})


@tool
def job_events(job_id: str, since_id: str = "", limit: int = 200) -> str:
    """List event rows for a job."""
    return _call_platform_mcp(
        "job_events",
        {
            "job_id": job_id,
            "since_id": since_id,
            "limit": limit,
        },
    )


@tool
def job_messages(job_id: str, since_id: str = "", before_id: str = "", limit: int = 200) -> str:
    """List message rows for a job."""
    return _call_platform_mcp(
        "job_messages",
        {
            "job_id": job_id,
            "since_id": since_id,
            "before_id": before_id,
            "limit": limit,
        },
    )


@tool
def job_logs(
    job_id: str, since_id: str = "", before_id: str = "", newest: bool = False, limit: int = 25
) -> str:
    """List logs for a job."""
    return _call_platform_mcp(
        "job_logs",
        {
            "job_id": job_id,
            "since_id": since_id,
            "before_id": before_id,
            "newest": newest,
            "limit": limit,
        },
    )


@tool
def job_cost_breakdown(job_id: str) -> str:
    """Get cost breakdown for a job."""
    return _call_platform_mcp("job_cost_breakdown", {"job_id": job_id})


@tool
def job_approval(job_id: str) -> str:
    """Get the current approval gate payload for a job."""
    return _call_platform_mcp("job_approval", {"job_id": job_id})


@tool
def job_cancel(job_id: str, comment: str = "Cancelled via MCP") -> str:
    """Cancel a job through the MCP control plane."""
    return _call_platform_mcp("job_cancel", {"job_id": job_id, "comment": comment})


@tool
def job_approval_decide(
    thread_id: str,
    approval_id: str,
    approved: bool,
    idempotency_key: str,
    comment: str = "",
    action: str = "",
) -> str:
    """Approve or reject an awaiting-approval thread through MCP."""
    return _call_platform_mcp(
        "job_approval_decide",
        {
            "thread_id": thread_id,
            "approval_id": approval_id,
            "approved": approved,
            "idempotency_key": idempotency_key,
            "comment": comment,
            "action": action,
        },
    )


@tool
def report_generate(
    report_type: str,
    params: dict | None = None,
    persist: bool = True,
    session_id: str = "",
) -> str:
    """Generate a report through the Flow Steward control plane."""
    return _call_platform_mcp(
        "report_generate",
        {
            "report_type": report_type,
            "params": dict(params or {}),
            "persist": persist,
            "session_id": session_id,
        },
    )


@tool
def report_get(report_id: str) -> str:
    """Fetch a previously generated report."""
    return _call_platform_mcp("report_get", {"report_id": report_id})


@tool
def project_notification_routes(project_name: str, enabled_only: bool = False) -> str:
    """List notification routes for a project."""
    return _call_platform_mcp(
        "project_notification_routes",
        {
            "project_name": project_name,
            "enabled_only": enabled_only,
        },
    )


@tool
def project_notification_deliveries(project_name: str, job_id: str = "", limit: int = 200) -> str:
    """List notification delivery logs for a project."""
    return _call_platform_mcp(
        "project_notification_deliveries",
        {
            "project_name": project_name,
            "job_id": job_id,
            "limit": limit,
        },
    )


@tool
def dispatch_project_notification(
    project_name: str,
    event_type: str,
    severity: str = "info",
    workflow_id: str = "",
    job_id: str = "",
    payload: dict | None = None,
) -> str:
    """Dispatch a project notification event through the control plane."""
    return _call_platform_mcp(
        "dispatch_project_notification",
        {
            "project_name": project_name,
            "event_type": event_type,
            "severity": severity,
            "workflow_id": workflow_id,
            "job_id": job_id,
            "payload": dict(payload or {}),
        },
    )
