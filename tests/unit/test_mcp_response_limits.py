"""Configured MCP HTTP bounds and safe operator diagnostics."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from core.application.management import mcp_connector_http


@pytest.fixture(autouse=True)
def public_mcp_dns(monkeypatch):
    monkeypatch.setattr(
        mcp_connector_http, "_resolve_hostname_ips", lambda *a, **k: ["93.184.216.34"]
    )


@pytest.mark.parametrize(
    "setting,expected",
    [
        (None, 4194304),
        ("8388608", 8388608),
        ("invalid", 4194304),
        ("0", 1024),
        ("1073741825", 1073741824),
    ],
)
def test_mcp_http_response_reads_configured_bound(monkeypatch, setting, expected):
    if setting is None:
        monkeypatch.delenv("FS_MCP_HTTP_RESPONSE_MAX_BYTES", raising=False)
    else:
        monkeypatch.setenv("FS_MCP_HTTP_RESPONSE_MAX_BYTES", setting)
    sizes = []

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}

        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)

    monkeypatch.setattr(
        mcp_connector_http.url_safety,
        "open_pinned_url",
        lambda *a, **k: Response(b'{"jsonrpc":"2.0","id":1,"result":{}}'),
    )
    mcp_connector_http._json_rpc_post_with_metadata(
        url="https://example.test/mcp",
        headers={},
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout_seconds=5,
    )
    assert sizes == [expected + 1]


def test_mcp_response_larger_than_old_default_passes_when_configured(monkeypatch):
    monkeypatch.setenv("FS_MCP_HTTP_RESPONSE_MAX_BYTES", "8388608")
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"text": "x" * (4 * 1024 * 1024)}}
    ).encode()

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}

    monkeypatch.setattr(
        mcp_connector_http.url_safety, "open_pinned_url", lambda *a, **k: Response(body)
    )
    decoded, _headers, _latency = mcp_connector_http._json_rpc_post_with_metadata(
        url="https://example.test/mcp",
        headers={},
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
        timeout_seconds=5,
    )
    assert len(decoded["result"]["text"]) == 4 * 1024 * 1024


def test_mcp_response_over_limit_reports_safe_limit(monkeypatch):
    monkeypatch.setenv("FS_MCP_HTTP_RESPONSE_MAX_BYTES", "1024")

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}

    marker = b"Authorization: Bearer private-response-token"
    monkeypatch.setattr(
        mcp_connector_http.url_safety, "open_pinned_url", lambda *a, **k: Response(marker * 100)
    )
    with pytest.raises(mcp_connector_http.McpResponseSizeLimitError) as raised:
        mcp_connector_http._json_rpc_post_with_metadata(
            url="https://example.test/mcp",
            headers={},
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
            timeout_seconds=5,
        )
    assert raised.value.max_bytes == 1024
    assert "1024" in str(raised.value)
    assert "private-response-token" not in str(raised.value)


def test_projected_mcp_tool_reports_limit_setting(monkeypatch):
    from core.application.auth import mcp_connector_oauth
    from core.application.orchestration.tools import tool_resolver

    monkeypatch.setattr(tool_resolver, "_load_mcp_connector_row", lambda *a, **k: {"enabled": True})
    monkeypatch.setattr(tool_resolver, "_mcp_tool_is_allowlisted", lambda *a, **k: True)

    def fail(*a, **k):
        raise mcp_connector_http.McpResponseSizeLimitError(
            max_bytes=1024, purpose="MCP server response"
        )

    monkeypatch.setattr(mcp_connector_oauth, "mcp_connector_rpc_post", fail)
    manifest = SimpleNamespace(
        tool_id="mcp.demo.read",
        implementation=SimpleNamespace(connector_id="demo"),
        config={"remote_tool_name": "read"},
    )
    with pytest.raises(RuntimeError, match="FS_MCP_HTTP_RESPONSE_MAX_BYTES") as raised:
        tool_resolver._invoke_remote_mcp_tool(
            manifest,
            function_name="invoke",
            arguments={},
            runtime_context={"logger": SimpleNamespace(cursor=object())},
        )
    assert "1024" in str(raised.value)


def test_mcp_limit_is_a_settable_host_setting():
    from core.application.lifecycle.operator_settings import operator_setting_specs

    spec = operator_setting_specs()["FS_MCP_HTTP_RESPONSE_MAX_BYTES"]
    assert spec.default == "4194304"
    assert not spec.secret


@pytest.mark.parametrize(
    "compose_file,services",
    [
        ("docker-compose.yaml", ("fs-app", "fs-worker")),
        ("docker-compose.prod.yaml", ("fs-app", "fs-worker")),
        ("docker-compose.compact.yaml", ("compact-runtime",)),
    ],
)
def test_mcp_limit_reaches_rendered_deployment(compose_file, services):
    root = Path(__file__).resolve().parents[2]
    image = "registry.invalid/synthetic@sha256:" + "a" * 64
    env = {
        **os.environ,
        # A host shell choice must not misidentify a Docker management process.
        "FS_INSTALLER_USE_HOST_PYTHON": "1",
        "FS_APP_IMAGE": image,
        "FS_WORKER_IMAGE": image,
        "FS_CONTROL_IMAGE": image,
        "FS_CLI_IMAGE": image,
        "MCP_IMAGE": image,
        "POSTGRES_IMAGE": image,
        "FS_COMPACT_IMAGE": image,
        "FS_APP_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "FS_WORKER_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "FS_CONTROL_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "MCP_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "FS_COMPACT_IMAGE_INDEX_DIGEST": "sha256:" + "a" * 64,
        "FS_COMPACT_IMAGE_MANIFEST_DIGEST": "sha256:" + "a" * 64,
        "FS_COMPACT_IMAGE_CONFIG_DIGEST": "sha256:" + "a" * 64,
        "FS_COMPACT_IMAGE_PLATFORM": "linux/amd64",
        "FS_COMPACT_IMAGE_REVISION": "a" * 40,
        "FS_BUILD_SHA": "a" * 40,
        "MCP_IMAGE_REVISION": "a" * 40,
        "FS_WORKER_PROCESSES": "1",
        "FS_CANDIDATE_STATE_DIR": "/tmp/synthetic-state",
        "FS_CANDIDATE_PROD_ENV": "/tmp/synthetic.env.prod",
        "FS_WORKER_MCP_BEARER_TOKEN": "synthetic-test-value",
        "FS_METRICS_TOKEN": "synthetic-test-value",
        "SECURITY_PII_SALT": "synthetic-test-value",
    }
    env.update(FS_MCP_HTTP_RESPONSE_MAX_BYTES="8388608", FS_STACK_ENV_FILE="/dev/null")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            os.devnull,
            "-f",
            str(root / "infra/docker" / compose_file),
            "config",
            "--no-env-resolution",
            "--format",
            "json",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    for service in services:
        assert (
            rendered["services"][service]["environment"]["FS_MCP_HTTP_RESPONSE_MAX_BYTES"]
            == "8388608"
        )


def test_mcp_limit_reaches_compact_child_processes():
    from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
    from core.infrastructure.runtime.compact.process_spec import build_process_specs

    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )
    specs = build_process_specs(
        settings,
        parent_environment={
            "FS_MCP_HTTP_RESPONSE_MAX_BYTES": "8388608",
            "FS_WORKER_MCP_BEARER_TOKEN": "synthetic-test-value",
        },
        public_port=8080,
        observation_fd=7,
    )
    by_role = {spec.role: spec for spec in specs}
    for role in ("app", "mcp", "worker-0"):
        assert by_role[role].environment["FS_MCP_HTTP_RESPONSE_MAX_BYTES"] == "8388608"
    assert "FS_MCP_HTTP_RESPONSE_MAX_BYTES" not in by_role["router"].environment
