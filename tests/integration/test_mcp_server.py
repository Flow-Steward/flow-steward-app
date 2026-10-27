"""
Integration tests for the MCP server.

Requires: MCP server running (fs-mcp container).

Run:
    docker compose exec fs-app python -m pytest tests/integration/test_mcp_server.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

pytestmark = pytest.mark.integration

MCP_BASE_URL_CANDIDATES = [
    os.getenv("MCP_BASE_URL", "").strip(),
    "http://fs-mcp:8100",
    "http://localhost:8100",
]
MCP_TEST_PROJECT_NAME = (
    str(
        os.getenv("MCP_TEST_PROJECT_NAME")
        or os.getenv("TEST_PROJECT_BACKEND_NAME")
        or "Test Project Backend"
    ).strip()
    or "Test Project Backend"
)
CONTROL_PLANE_REQUIRED_TOOLS = {
    "project_list",
    "project_jobs",
    "workflow_list",
    "job_result",
    "job_status",
    "job_events",
    "job_messages",
    "job_logs",
    "job_cost_breakdown",
    "job_approval",
    "report_generate",
    "report_get",
    "project_notification_routes",
    "project_notification_deliveries",
    "dispatch_project_notification",
}
MCP_MODERN_VERSION = "2026-07-28"
MCP_LEGACY_VERSION = "2025-11-25"


def _modern_meta() -> dict[str, object]:
    return {
        "io.modelcontextprotocol/protocolVersion": MCP_MODERN_VERSION,
        "io.modelcontextprotocol/clientInfo": {
            "name": "flow-steward-integration",
            "version": "1.0.0",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def _modern_headers(*, method: str, name: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_MODERN_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _modern_params(params: dict[str, object] | None = None) -> dict[str, object]:
    return {**(params or {}), "_meta": _modern_meta()}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _normalize_app_base_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return value
    try:
        parsed = urlsplit(value)
    except Exception:
        return value

    host = str(parsed.hostname or "").strip().lower()
    if (
        parsed.scheme == "https"
        and host in {"localhost", "127.0.0.1", "::1", "fs-app"}
        and str(os.getenv("FS_SERVE_SSL", "0") or "").strip().lower()
        not in {"1", "true", "yes", "on"}
    ):
        return urlunsplit(("http", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return value


def _candidate_app_base_urls() -> list[str]:
    try:
        from core.application.tooling.testing.runtime_env import resolve_base_url

        resolved = resolve_base_url(os.environ, include_dotenv=True)
    except Exception:
        resolved = os.getenv("APP_BASE_URL", "").strip()

    urls: list[str] = []
    for candidate in (
        _normalize_app_base_url(resolved),
        _normalize_app_base_url(os.getenv("FS_INTERNAL_BASE_URL", "").strip()),
        "http://localhost:8099",
        "http://fs-app:8099",
    ):
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


def _mcp_auth_headers() -> tuple[dict[str, str], str | None]:
    import requests

    try:
        from core.application.tooling.testing.runtime_env import resolve_test_env

        creds = resolve_test_env(os.environ, lane="backend", role="admin", include_dotenv=True)
    except Exception as e:
        return {}, f"Could not resolve backend test credentials: {e}"

    for app_base_url in _candidate_app_base_urls():
        session = requests.Session()
        try:
            resp = session.post(
                f"{app_base_url}/api/auth/login",
                json={"email": creds.username, "password": creds.password},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=10,
                verify=False,
            )
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        token = str(session.cookies.get("fs_session") or "").strip()
        if token:
            return {"Authorization": f"Bearer {token}"}, None
    return {}, "Could not obtain Flow Steward session token for MCP integration tests"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def http_session():
    """requests.Session for MCP calls."""
    try:
        import requests

        session = requests.Session()
        auth_headers, auth_error = _mcp_auth_headers()
        if auth_headers:
            session.headers.update(auth_headers)
        reachable_statuses: list[tuple[str, int]] = []
        contract_failures: list[tuple[str, str]] = []
        for base_url in [url for url in MCP_BASE_URL_CANDIDATES if url]:
            try:
                resp = session.post(
                    f"{base_url}/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/list",
                        "params": _modern_params(),
                    },
                    headers=_modern_headers(method="tools/list"),
                    timeout=5,
                )
            except Exception:
                continue
            if resp.status_code in (401, 403):
                reachable_statuses.append((base_url, resp.status_code))
            if resp.status_code == 200:
                try:
                    payload = _require_jsonrpc_ok(resp, tool_name="tools/list")
                    _tools_list_names(payload)
                except AssertionError as exc:
                    contract_failures.append((base_url, str(exc)))
                    continue
                yield session, base_url
                return
        if contract_failures:
            detail = "; ".join(f"{url} -> {message}" for url, message in contract_failures)
            pytest.fail(f"MCP tools/list contract preflight failed: {detail}")
        if reachable_statuses:
            detail = ", ".join(f"{url} -> {status}" for url, status in reachable_statuses)
            if auth_error:
                pytest.skip(
                    f"MCP server reachable but authenticated tools/list failed: {auth_error}; seen {detail}"
                )
            pytest.skip(f"MCP server reachable but authenticated tools/list failed: {detail}")
        pytest.skip("MCP server not available on any configured base URL")
    except Exception as e:
        pytest.skip(f"MCP server not available: {e}")


def call_mcp_tool(session, base_url: str, tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool via HTTP POST."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
            "_meta": _modern_meta(),
        },
    }
    return session.post(
        f"{base_url}/mcp",
        json=payload,
        headers=_modern_headers(method="tools/call", name=tool_name),
        timeout=30,
    )


def _require_jsonrpc_ok(resp, *, tool_name: str) -> dict:
    """Require strict JSON-RPC HTTP contract for registered MCP tools."""
    assert resp.status_code == 200, (
        f"{tool_name} expected HTTP 200, got {resp.status_code}: {resp.text[:300]}"
    )
    payload = resp.json()
    assert payload.get("jsonrpc") == "2.0", f"{tool_name} returned non-JSON-RPC payload: {payload}"
    assert payload.get("id") == 1, f"{tool_name} response id mismatch: {payload}"
    assert "result" in payload, f"{tool_name} missing result payload: {payload}"
    assert "error" not in payload, f"{tool_name} returned JSON-RPC error: {payload.get('error')}"
    return payload


def _tool_result_text(payload: dict) -> str:
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "result" in structured:
        return str(structured.get("result") or "")
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "text":
                return str(item.get("text") or "")
    return ""


def _tools_list_names(payload: dict) -> set[str]:
    result = payload.get("result")
    assert isinstance(result, dict), f"tools/list returned malformed result payload: {payload}"

    candidates: list[object] = []
    tools = result.get("tools")
    if isinstance(tools, list):
        candidates.append(tools)

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        structured_tools = structured.get("tools")
        if isinstance(structured_tools, list):
            candidates.append(structured_tools)

    text = _tool_result_text(payload)
    if text:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("tools"), list):
            candidates.append(parsed.get("tools"))
        elif isinstance(parsed, list):
            candidates.append(parsed)

    names: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        for item in candidate:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    names.add(name)

    assert names, f"tools/list did not expose any MCP tools: {payload}"
    return names


def _tool_result_envelope(payload: dict, *, tool_name: str) -> dict:
    text = _tool_result_text(payload)
    assert text, f"{tool_name} returned empty tool result text"
    try:
        data = json.loads(text)
    except Exception as exc:
        raise AssertionError(f"{tool_name} returned non-JSON result payload: {text[:300]}") from exc
    assert isinstance(data, dict), f"{tool_name} result should be a JSON object: {data!r}"
    return data


def _tool_result_json(payload: dict, *, tool_name: str) -> dict:
    envelope = _tool_result_envelope(payload, tool_name=tool_name)
    assert "ok" in envelope, f"{tool_name} did not return MCP envelope: {envelope}"
    assert "data" in envelope, f"{tool_name} did not return MCP envelope: {envelope}"
    assert "error" in envelope, f"{tool_name} did not return MCP envelope: {envelope}"
    assert "meta" in envelope, f"{tool_name} did not return MCP envelope: {envelope}"
    assert envelope.get("ok") is True, f"{tool_name} returned MCP error envelope: {envelope}"
    data = envelope.get("data")
    assert isinstance(data, dict), f"{tool_name} result data should be a JSON object: {data!r}"
    return data


def _tool_result_is_error(payload: dict) -> bool:
    result = payload.get("result")
    if not isinstance(result, dict):
        return False
    if bool(result.get("isError")):
        return True
    try:
        envelope = _tool_result_envelope(payload, tool_name="mcp_tool")
    except Exception:
        return False
    return envelope.get("ok") is False


def _require_registered_tool_round_trip(payload: dict, *, tool_name: str) -> str:
    text = _tool_result_text(payload)
    assert text, f"{tool_name} returned empty tool result text"
    lowered = text.lower()
    assert "unknown tool" not in lowered, f"{tool_name} is not registered: {text}"
    assert "invalid arguments" not in lowered, f"{tool_name} argument schema mismatch: {text}"
    return text


def _project_name_from_project_list(body: dict) -> str:
    projects = list(body.get("projects") or [])
    if projects:
        exact = next(
            (
                str((row or {}).get("name") or "").strip()
                for row in projects
                if str((row or {}).get("name") or "").strip() == MCP_TEST_PROJECT_NAME
            ),
            "",
        )
        if exact:
            return exact
        fallback = str((projects[0] or {}).get("name") or "").strip()
        if fallback:
            return fallback
    return MCP_TEST_PROJECT_NAME


def _latest_job_id_from_project_jobs(body: dict) -> str:
    jobs = list(body.get("jobs") or [])
    if jobs:
        value = str((jobs[0] or {}).get("id") or (jobs[0] or {}).get("job_id") or "").strip()
        if value:
            return value
    return "1"


def _resolve_mcp_project_name(session, base_url: str) -> str:
    resp = call_mcp_tool(session, base_url, "project_list", {})
    payload = _require_jsonrpc_ok(resp, tool_name="project_list")
    body = _tool_result_json(payload, tool_name="project_list")
    return _project_name_from_project_list(body)


def _resolve_latest_job_id(session, base_url: str, *, project_name: str) -> str:
    resp = call_mcp_tool(
        session,
        base_url,
        "project_jobs",
        {"project_name": project_name, "limit": 20},
    )
    payload = _require_jsonrpc_ok(resp, tool_name="project_jobs")
    body = _tool_result_json(payload, tool_name="project_jobs")
    return _latest_job_id_from_project_jobs(body)


def _require_control_plane_preflight(session, base_url: str) -> dict[str, object]:
    tools_resp = session.post(
        f"{base_url}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": _modern_params(),
        },
        headers=_modern_headers(method="tools/list"),
        timeout=5,
    )
    tools_payload = _require_jsonrpc_ok(tools_resp, tool_name="tools/list")
    available_tools = _tools_list_names(tools_payload)
    missing_tools = sorted(CONTROL_PLANE_REQUIRED_TOOLS - available_tools)
    assert not missing_tools, "Control-plane MCP preflight missing required tools: " + ", ".join(
        missing_tools
    )

    project_name = _resolve_mcp_project_name(session, base_url)
    job_id = _resolve_latest_job_id(session, base_url, project_name=project_name)

    return {
        "available_tools": sorted(available_tools),
        "project_name": project_name,
        "job_id": job_id,
    }


@pytest.fixture(scope="module")
def control_plane_context(http_session):
    session, base_url = http_session
    preflight = _require_control_plane_preflight(session, base_url)
    return {
        "session": session,
        "base_url": base_url,
        "project_name": str(preflight["project_name"]),
        "job_id": str(preflight["job_id"]),
        "available_tools": list(preflight["available_tools"]),
    }


# ---------------------------------------------------------------------------
# MCP server reachability
# ---------------------------------------------------------------------------


class TestMCPServerReachability:
    def test_mcp_server_is_reachable(self, http_session):
        """MCP server should respond to a JSON tools/list call."""
        session, base_url = http_session
        resp = session.post(
            f"{base_url}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": _modern_params(),
            },
            headers=_modern_headers(method="tools/list"),
            timeout=5,
        )
        assert resp.status_code == 200

    def test_modern_discovery_is_stateless_and_advertises_2026(self, http_session):
        session, base_url = http_session
        resp = session.post(
            f"{base_url}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": _modern_params(),
            },
            headers=_modern_headers(method="server/discover"),
            timeout=5,
        )

        payload = _require_jsonrpc_ok(resp, tool_name="server/discover")
        assert MCP_MODERN_VERSION in payload["result"]["supportedVersions"]
        assert "capabilities" in payload["result"]
        assert "mcp-session-id" not in {key.lower() for key in resp.headers}

    def test_supported_legacy_client_initializes_and_lists_tools(self, http_session):
        session, base_url = http_session
        initialize = session.post(
            f"{base_url}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_LEGACY_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-test", "version": "1.0.0"},
                },
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=5,
        )
        initialized_payload = _require_jsonrpc_ok(initialize, tool_name="initialize")
        negotiated = initialized_payload["result"]["protocolVersion"]
        session_id = initialize.headers.get("Mcp-Session-Id")
        assert session_id

        session.post(
            f"{base_url}/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": negotiated,
                "Mcp-Session-Id": session_id,
            },
            timeout=5,
        )
        listed = session.post(
            f"{base_url}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": negotiated,
                "Mcp-Session-Id": session_id,
            },
            timeout=5,
        )
        names = set(_tools_list_names(_require_jsonrpc_ok(listed, tool_name="tools/list")))
        assert len(names) == 33
        assert {
            "kb_search",
            "kb_write",
            "project_profile_get",
            "project_profile_update",
        }.isdisjoint(names)
        assert {"project_list", "project_jobs"} <= names

    def test_mcp_server_health(self):
        """MCP server should respond to basic health check."""
        import requests

        resp = None
        for base_url in [url for url in MCP_BASE_URL_CANDIDATES if url]:
            try:
                resp = requests.get(f"{base_url}/", timeout=5)
                break
            except Exception:
                continue
        if resp is None:
            pytest.skip("MCP server not available")
        assert resp.status_code in (200, 404)


class TestControlPlaneViaMCP:
    @pytest.mark.parametrize(
        ("tool_name", "arg_builder"),
        [
            ("workflow_list", lambda ctx: {"project_name": str(ctx["project_name"])}),
            ("job_result", lambda ctx: {"job_id": str(ctx["job_id"])}),
        ],
    )
    def test_workflow_surface_tools_round_trip(self, control_plane_context, tool_name, arg_builder):
        resp = call_mcp_tool(
            control_plane_context["session"],
            control_plane_context["base_url"],
            tool_name,
            dict(arg_builder(control_plane_context)),
        )
        payload = _require_jsonrpc_ok(resp, tool_name=tool_name)
        _require_registered_tool_round_trip(payload, tool_name=tool_name)
        envelope = _tool_result_envelope(payload, tool_name=tool_name)
        assert "meta" in envelope
        if envelope.get("ok") is False:
            assert isinstance(envelope.get("error"), dict)
            return
        assert isinstance(envelope.get("data"), dict)

    def test_workflow_trigger_tool_is_reachable_via_mcp(self, control_plane_context):
        resp = call_mcp_tool(
            control_plane_context["session"],
            control_plane_context["base_url"],
            "workflow_trigger",
            {
                "project_name": str(control_plane_context["project_name"]),
                "test_mode": True,
                "workflow_id": "weekly_report",
            },
        )
        payload = _require_jsonrpc_ok(resp, tool_name="workflow_trigger")
        text = _require_registered_tool_round_trip(payload, tool_name="workflow_trigger")
        if _tool_result_is_error(payload):
            assert "task enqueue is unavailable" in text.lower()
            return

        body = _tool_result_json(payload, tool_name="workflow_trigger")
        assert str(body.get("job_id") or "").strip(), (
            f"workflow_trigger did not return job_id: {body}"
        )

    @pytest.mark.parametrize(
        ("tool_name", "arg_builder"),
        [
            ("job_status", lambda ctx: {"job_id": str(ctx["job_id"])}),
            ("job_events", lambda ctx: {"job_id": str(ctx["job_id"]), "limit": 25}),
            ("job_messages", lambda ctx: {"job_id": str(ctx["job_id"]), "limit": 25}),
            ("job_logs", lambda ctx: {"job_id": str(ctx["job_id"]), "limit": 10}),
            ("job_cost_breakdown", lambda ctx: {"job_id": str(ctx["job_id"])}),
            ("job_approval", lambda ctx: {"job_id": str(ctx["job_id"])}),
        ],
    )
    def test_job_control_plane_tools_round_trip(
        self, control_plane_context, tool_name, arg_builder
    ):
        resp = call_mcp_tool(
            control_plane_context["session"],
            control_plane_context["base_url"],
            tool_name,
            dict(arg_builder(control_plane_context)),
        )
        payload = _require_jsonrpc_ok(resp, tool_name=tool_name)
        text = _require_registered_tool_round_trip(payload, tool_name=tool_name)
        if tool_name == "job_approval" and _tool_result_is_error(payload):
            assert "not awaiting approval" in text.lower()
            return
        assert not _tool_result_is_error(payload), f"{tool_name} returned tool error: {text}"

    def test_report_generate_and_get_round_trip(self, control_plane_context):
        session = control_plane_context["session"]
        base_url = control_plane_context["base_url"]
        resp = call_mcp_tool(
            session,
            base_url,
            "report_generate",
            {
                "report_type": "job_summary",
                "params": {"job_id": str(control_plane_context["job_id"])},
                "persist": True,
            },
        )
        payload = _require_jsonrpc_ok(resp, tool_name="report_generate")
        _require_registered_tool_round_trip(payload, tool_name="report_generate")
        assert not _tool_result_is_error(payload), (
            f"report_generate returned tool error: {_tool_result_text(payload)}"
        )
        report = _tool_result_json(payload, tool_name="report_generate")
        report_id = str(report.get("id") or "").strip()
        assert report_id, f"report_generate did not return report id: {report}"

        get_resp = call_mcp_tool(session, base_url, "report_get", {"report_id": report_id})
        get_payload = _require_jsonrpc_ok(get_resp, tool_name="report_get")
        _require_registered_tool_round_trip(get_payload, tool_name="report_get")
        assert not _tool_result_is_error(get_payload), (
            f"report_get returned tool error: {_tool_result_text(get_payload)}"
        )
        get_body = _tool_result_json(get_payload, tool_name="report_get")
        assert str(get_body.get("id") or "").strip() == report_id

    def test_project_notification_control_plane_tools_round_trip(self, control_plane_context):
        session = control_plane_context["session"]
        base_url = control_plane_context["base_url"]
        project_name = str(control_plane_context["project_name"] or "").strip()
        job_id = str(control_plane_context["job_id"])

        routes_resp = call_mcp_tool(
            session,
            base_url,
            "project_notification_routes",
            {"project_name": project_name},
        )
        routes_payload = _require_jsonrpc_ok(routes_resp, tool_name="project_notification_routes")
        _require_registered_tool_round_trip(routes_payload, tool_name="project_notification_routes")
        assert not _tool_result_is_error(routes_payload), (
            f"project_notification_routes returned tool error: {_tool_result_text(routes_payload)}"
        )

        deliveries_resp = call_mcp_tool(
            session,
            base_url,
            "project_notification_deliveries",
            {"project_name": project_name, "job_id": job_id, "limit": 25},
        )
        deliveries_payload = _require_jsonrpc_ok(
            deliveries_resp, tool_name="project_notification_deliveries"
        )
        _require_registered_tool_round_trip(
            deliveries_payload, tool_name="project_notification_deliveries"
        )
        assert not _tool_result_is_error(deliveries_payload), (
            f"project_notification_deliveries returned tool error: {_tool_result_text(deliveries_payload)}"
        )

        dispatch_resp = call_mcp_tool(
            session,
            base_url,
            "dispatch_project_notification",
            {
                "project_name": project_name,
                "event_type": "job.failed",
                "severity": "error",
                "job_id": job_id,
                "payload": {"source": "integration-test"},
            },
        )
        dispatch_payload = _require_jsonrpc_ok(
            dispatch_resp, tool_name="dispatch_project_notification"
        )
        _require_registered_tool_round_trip(
            dispatch_payload, tool_name="dispatch_project_notification"
        )
        assert not _tool_result_is_error(dispatch_payload), (
            f"dispatch_project_notification returned tool error: {_tool_result_text(dispatch_payload)}"
        )
