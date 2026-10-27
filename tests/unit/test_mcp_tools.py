from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _mcp_payload(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": text}],
        },
    }


def _mock_flow_steward_endpoint(monkeypatch, mcp_tools, fake_post) -> None:
    monkeypatch.setattr(mcp_tools, "_MCP_URL", "http://flow-steward.example/mcp")
    monkeypatch.setattr(mcp_tools, "_MCP_BEARER_TOKEN_CACHE", "test-mcp-token")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-mcp-token")

    def fake_call(url, tool_name, arguments, **_kwargs):
        response = fake_post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        return response.json()["result"]["content"][0]["text"]

    monkeypatch.setattr(mcp_tools, "_call_mcp_endpoint", fake_call)


def test_workflow_trigger_uses_flow_steward_endpoint(monkeypatch):
    try:
        import core.application.orchestration.tools.mcp_tools as mcp_tools
    except Exception as e:
        pytest.skip(f"core.application.orchestration.tools.mcp_tools not importable: {e}")

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResponse(_mcp_payload('{"job_id":77}'))

    _mock_flow_steward_endpoint(monkeypatch, mcp_tools, fake_post)

    result = mcp_tools.workflow_trigger.invoke(
        {
            "project_name": "demo",
            "workflow_id": "stock-import",
            "payload": {"city": "Vilnius"},
        }
    )

    assert '"job_id":77' in result
    assert calls[0]["json"]["params"]["name"] == "workflow_trigger"
    assert calls[0]["json"]["params"]["arguments"]["payload"] == {"city": "Vilnius"}


def test_workflow_list_uses_flow_steward_endpoint(monkeypatch):
    try:
        import core.application.orchestration.tools.mcp_tools as mcp_tools
    except Exception as e:
        pytest.skip(f"core.application.orchestration.tools.mcp_tools not importable: {e}")

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResponse(_mcp_payload('{"project_id":11,"workflows":[]}'))

    _mock_flow_steward_endpoint(monkeypatch, mcp_tools, fake_post)

    result = mcp_tools.workflow_list.invoke(
        {
            "project_name": "demo",
            "enabled_only": True,
            "include_validation": False,
        }
    )

    assert '"project_id":11' in result
    assert calls[0]["json"]["params"]["name"] == "workflow_list"


def test_job_result_uses_flow_steward_endpoint(monkeypatch):
    try:
        import core.application.orchestration.tools.mcp_tools as mcp_tools
    except Exception as e:
        pytest.skip(f"core.application.orchestration.tools.mcp_tools not importable: {e}")

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResponse(_mcp_payload('{"job":{"id":77},"completed":true}'))

    _mock_flow_steward_endpoint(monkeypatch, mcp_tools, fake_post)

    result = mcp_tools.job_result.invoke({"job_id": "33333333-3333-3333-3333-333333333333"})

    assert '"completed":true' in result
    assert calls[0]["json"]["params"]["name"] == "job_result"


def test_job_cancel_uses_flow_steward_endpoint(monkeypatch):
    try:
        import core.application.orchestration.tools.mcp_tools as mcp_tools
    except Exception as e:
        pytest.skip(f"core.application.orchestration.tools.mcp_tools not importable: {e}")

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResponse(_mcp_payload('{"job_id":77,"status":"cancelled"}'))

    _mock_flow_steward_endpoint(monkeypatch, mcp_tools, fake_post)

    result = mcp_tools.job_cancel.invoke(
        {"job_id": "33333333-3333-3333-3333-333333333333", "comment": "stop"}
    )

    assert '"status":"cancelled"' in result
    assert calls[0]["json"]["params"]["name"] == "job_cancel"


def test_report_generate_uses_flow_steward_endpoint(monkeypatch):
    try:
        import core.application.orchestration.tools.mcp_tools as mcp_tools
    except Exception as e:
        pytest.skip(f"core.application.orchestration.tools.mcp_tools not importable: {e}")

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResponse(_mcp_payload('{"report_type":"job_summary"}'))

    _mock_flow_steward_endpoint(monkeypatch, mcp_tools, fake_post)

    result = mcp_tools.report_generate.invoke(
        {
            "report_type": "job_summary",
            "params": {"project_id": 11},
            "persist": True,
        }
    )

    assert '"report_type":"job_summary"' in result
    assert calls[0]["json"]["params"]["name"] == "report_generate"


def test_project_notification_routes_uses_flow_steward_endpoint(monkeypatch):
    try:
        import core.application.orchestration.tools.mcp_tools as mcp_tools
    except Exception as e:
        pytest.skip(f"core.application.orchestration.tools.mcp_tools not importable: {e}")

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResponse(_mcp_payload('{"project_id":11}'))

    _mock_flow_steward_endpoint(monkeypatch, mcp_tools, fake_post)

    result = mcp_tools.project_notification_routes.invoke(
        {
            "project_name": "demo",
            "enabled_only": False,
        }
    )

    assert '"project_id":11' in result
    assert calls[0]["json"]["params"]["name"] == "project_notification_routes"


@pytest.mark.parametrize("name", ["include_messages_limit", "include_logs_limit"])
@pytest.mark.parametrize("value", [0, -1, 2001])
def test_job_result_rejects_invalid_limits_before_transport(monkeypatch, name, value):
    from pydantic import ValidationError

    from core.application.orchestration.tools import mcp_tools

    calls = []
    monkeypatch.setattr(mcp_tools, "_call_platform_mcp", lambda *args: calls.append(args))

    with pytest.raises(ValidationError) as error:
        mcp_tools.job_result.invoke({"job_id": "job-1", name: value})

    assert error.value.errors()[0]["loc"] == (name,)
    assert calls == []


@pytest.mark.parametrize("limits", [{}, {"include_messages_limit": 1, "include_logs_limit": 2000}])
def test_job_result_keeps_default_and_boundary_limits(monkeypatch, limits):
    from core.application.orchestration.tools import mcp_tools

    calls = []

    def transport(name, arguments):
        calls.append((name, arguments))
        return '{"job":{"id":"job-1"},"completed":true}'

    monkeypatch.setattr(mcp_tools, "_call_platform_mcp", transport)

    result = mcp_tools.job_result.invoke({"job_id": "job-1", **limits})

    assert result == '{"job":{"id":"job-1"},"completed":true}'
    assert calls == [
        (
            "job_result",
            {
                "job_id": "job-1",
                "include_messages_limit": limits.get("include_messages_limit", 100),
                "include_logs_limit": limits.get("include_logs_limit", 50),
            },
        )
    ]
