"""Startup provenance checks for the packaged MCP service (AUDIT-064)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.entrypoints.mcp.server_build_revision import (
    McpBuildRevisionMismatch,
    verify_embedded_build_revision,
)


def test_matching_embedded_and_deployment_revisions_are_accepted(monkeypatch):
    monkeypatch.setenv("FS_MCP_IMAGE_REVISION", "a" * 40)
    monkeypatch.setenv("FS_DEPLOYMENT_REVISION", "a" * 40)

    verify_embedded_build_revision()


def test_mismatched_deployment_revision_stops_mcp_before_serving(monkeypatch):
    monkeypatch.setenv("FS_MCP_IMAGE_REVISION", "a" * 40)
    monkeypatch.setenv("FS_DEPLOYMENT_REVISION", "b" * 40)

    with pytest.raises(McpBuildRevisionMismatch, match="does not match"):
        verify_embedded_build_revision()


def test_local_mcp_without_a_declared_deployment_revision_is_allowed(monkeypatch):
    monkeypatch.setenv("FS_MCP_IMAGE_REVISION", "a" * 40)
    monkeypatch.delenv("FS_DEPLOYMENT_REVISION", raising=False)

    verify_embedded_build_revision()


def test_mcp_main_checks_revision_before_bootstrapping_the_transport(monkeypatch):
    from core.entrypoints.mcp import server_main

    monkeypatch.setenv("FS_MCP_IMAGE_REVISION", "a" * 40)
    monkeypatch.setenv("FS_DEPLOYMENT_REVISION", "b" * 40)
    monkeypatch.setattr(
        server_main.argparse.ArgumentParser,
        "parse_args",
        lambda _parser: SimpleNamespace(transport="stdio", host="127.0.0.1", port=8100),
    )

    with pytest.raises(McpBuildRevisionMismatch, match="does not match"):
        server_main.main()
