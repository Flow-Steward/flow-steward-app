from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_runtime_uses_official_mcp_v2_server_and_exact_inventory(monkeypatch) -> None:
    monkeypatch.setenv("FS_MCP_AUTH_DISABLED", "1")
    from mcp.server import MCPServer

    from core.entrypoints.mcp.server import mcp
    from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES

    assert importlib.metadata.version("mcp") == "2.2.0"
    assert isinstance(mcp, MCPServer)
    assert set(mcp._tool_manager._tools) == set(MCP_TOOL_SCOPES)


def test_auth_settings_decide_token_resource_validation_explicitly() -> None:
    """mcp 2.2 warns when resource_server_url is set without validate_token_resource.

    App API tokens are introspected by AppApiTokenVerifier and carry no RFC 8707
    resource, so the server opts out explicitly; 3.0 would otherwise refuse every token.
    Runs in a subprocess because the runtime builds its auth settings at import time.
    """
    script = (
        "import warnings\n"
        "from mcp import MCPDeprecationWarning\n"
        "warnings.simplefilter('error', MCPDeprecationWarning)\n"
        "from core.entrypoints.mcp import server_runtime\n"
        "assert server_runtime.MCP_AUTH_ENABLED\n"
        "assert server_runtime.auth_settings.validate_token_resource is False\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "FS_MCP_AUTH_DISABLED"}
    env["FS_MCP_AUTH_ENABLED"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_caller_identity_wraps_runtime_maintenance_middleware(monkeypatch) -> None:
    monkeypatch.setenv("FS_MCP_AUTH_DISABLED", "1")
    from core.entrypoints.mcp.server import mcp
    from core.entrypoints.mcp.server_caller_identity import McpCallerIdentityMiddleware
    from core.entrypoints.mcp.server_maintenance import McpMaintenanceMiddleware

    identity_index = next(
        index
        for index, middleware in enumerate(mcp.middleware)
        if isinstance(middleware, McpCallerIdentityMiddleware)
    )
    maintenance_index = next(
        index
        for index, middleware in enumerate(mcp.middleware)
        if isinstance(middleware, McpMaintenanceMiddleware)
    )

    assert identity_index < maintenance_index


def test_server_exposes_only_streamable_http_and_stdio(monkeypatch) -> None:
    monkeypatch.setenv("FS_MCP_AUTH_DISABLED", "1")
    from core.entrypoints.mcp.server_main import _build_argument_parser

    parser = _build_argument_parser()
    assert parser.parse_args(["--transport", "stdio"]).transport == "stdio"
    assert parser.parse_args(["--transport", "streamable-http"]).transport == "streamable-http"


def test_mcp_allowed_hosts_accept_the_host_header_clients_actually_send() -> None:
    """AC17-R55: 421 on every public MCP call.

    The installer writes FS_PUBLIC_BASE_URL with an explicit default port
    (https://host:443), so the derived authority was `host:443`. Standard clients omit the
    default port from the Host header and send bare `host`, the two never matched, and the
    transport rejected the entire public surface. Measured on the owner's server: identical
    requests, `Host: flow.example.com` -> 421, `Host: flow.example.com:443` -> a correct initialize.
    """
    from core.entrypoints.mcp.server_main import _allowed_hosts

    hosts = _allowed_hosts({"FS_PUBLIC_BASE_URL": "https://fs.example.test:443"})

    assert "fs.example.test" in hosts
    assert "fs.example.test:443" in hosts


def test_mcp_allowed_hosts_do_not_widen_a_non_default_port() -> None:
    """An install on 8443 must keep matching 8443 and must not start accepting bare host."""
    from core.entrypoints.mcp.server_main import _allowed_hosts

    hosts = _allowed_hosts({"FS_PUBLIC_BASE_URL": "https://fs.example.test:8443"})

    assert "fs.example.test:8443" in hosts
    assert "fs.example.test" not in hosts


def test_mcp_allowed_hosts_compare_the_port_against_its_own_scheme() -> None:
    """443 is not http's default, so a plain-http origin on :443 keeps the explicit port.

    A client speaking http to :443 sends `host:443` in the Host header and would never send a
    bare host, so admitting one would widen the allow-list for nothing.
    """
    from core.entrypoints.mcp.server_main import _allowed_hosts

    hosts = _allowed_hosts({"FS_PUBLIC_BASE_URL": "http://fs.example.test:443"})

    assert "fs.example.test:443" in hosts
    assert "fs.example.test" not in hosts

    plain = _allowed_hosts({"FS_PUBLIC_BASE_URL": "http://fs.example.test:80"})
    assert "fs.example.test" in plain


def test_mcp_allowed_hosts_normalize_an_operator_supplied_default_port() -> None:
    """The explicit override deserves the same treatment as the derived value."""
    from core.entrypoints.mcp.server_main import _allowed_hosts

    hosts = _allowed_hosts({"FS_MCP_ALLOWED_HOSTS": "fs.example.test:443"})

    assert "fs.example.test" in hosts
    assert "fs.example.test:443" in hosts


def test_mcp_allowed_hosts_adds_only_validated_public_authority() -> None:
    from core.entrypoints.mcp.server_main import _allowed_hosts

    assert "flow.example.test:8443" in _allowed_hosts(
        {"FS_PUBLIC_BASE_URL": "https://flow.example.test:8443"}
    )
    with pytest.raises(ValueError, match="exact authority"):
        _allowed_hosts({"FS_MCP_ALLOWED_HOSTS": "*"})


def test_portability_tools_are_in_official_registry_with_exact_public_signatures(monkeypatch):
    import inspect

    monkeypatch.setenv("FS_MCP_AUTH_DISABLED", "1")
    from core.entrypoints.mcp import server_workflow_portability_tools as tools
    from core.entrypoints.mcp.server import mcp

    expected = {
        "workflow_export": ["project_name", "workflow_id", "include_children", "reference_only"],
        "workflow_import": ["project_name", "bundle_yaml", "target_workflow_id", "resolutions"],
        "workflow_stats": ["project_name", "workflow_id"],
    }
    for name, parameters in expected.items():
        assert name in mcp._tool_manager._tools
        assert list(inspect.signature(getattr(tools, name)).parameters) == parameters
    from core.entrypoints.mcp.server_maintenance import mcp_tool_is_mutation

    assert mcp_tool_is_mutation("workflow_export") is False
    assert mcp_tool_is_mutation("workflow_stats") is False
    assert mcp_tool_is_mutation("workflow_import") is True


def test_authoring_tools_are_in_official_registry_with_exact_public_signatures(monkeypatch):
    import inspect

    monkeypatch.setenv("FS_MCP_AUTH_DISABLED", "1")
    from core.entrypoints.mcp import server_workflow_authoring_tools as tools
    from core.entrypoints.mcp.server import mcp

    expected = {
        "capability_search": ["project_name", "query", "kinds", "limit"],
        "capability_describe": ["project_name", "ref", "operation_id"],
        "workflow_examples_search": ["project_name", "query", "limit"],
        "workflow_authoring_schema": ["step_kind"],
        "workflow_draft_validate": ["project_name", "bundle_yaml", "resolutions"],
        "workflow_draft_save": ["project_name", "bundle_yaml", "resolutions"],
        "workflow_convert_group": [
            "workflow_id",
            "group_id",
            "child_workflow_id",
            "display_name",
            "expected_revision",
            "dry_run",
            "preview_digest",
        ],
        "workflow_apply_changes": ["workflow_id", "expected_revision", "operations", "dry_run"],
        "workflow_definition": ["workflow_id"],
    }
    for name, parameters in expected.items():
        assert name in mcp._tool_manager._tools
        assert list(inspect.signature(getattr(tools, name)).parameters) == parameters
