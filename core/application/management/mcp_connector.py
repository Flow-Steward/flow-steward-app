"""MCP connector model for registered external MCP servers."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _generate_connector_id() -> str:
    return f"mcp_{uuid.uuid4().hex[:16]}"


class McpConnectorAuthMode(StrEnum):
    none = "none"
    bearer = "bearer"
    api_key = "api_key"  # pragma: allowlist secret
    oauth2 = "oauth2"


class McpConnectorStatus(StrEnum):
    active = "active"
    degraded = "degraded"
    offline = "offline"
    unchecked = "unchecked"


class McpConnectorAuthStatus(StrEnum):
    not_configured = "not_configured"
    discovery_required = "discovery_required"
    auth_required = "auth_required"
    connected = "connected"
    refresh_failed = "refresh_failed"


class McpConnector(BaseModel):
    """First-class registered external MCP server."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(default_factory=_generate_connector_id)
    account_id: str
    project_id: str
    tenant_id: str
    display_name: str
    description: str = ""
    transport: Literal["streamable_http"] = "streamable_http"
    base_url: str
    auth_mode: McpConnectorAuthMode = McpConnectorAuthMode.none
    headers: dict[str, str] = Field(default_factory=dict)
    allowlisted_tools: list[str] = Field(default_factory=list)
    namespace_prefix: str = ""
    status: McpConnectorStatus = McpConnectorStatus.unchecked
    auth_status: McpConnectorAuthStatus = McpConnectorAuthStatus.not_configured
    oauth_metadata_jsonb: dict[str, object] = Field(default_factory=dict)
    oauth_secret_refs_jsonb: dict[str, object] = Field(default_factory=dict)
    oauth_credential_revision: int = 1
    oauth_connected_identity: str = ""
    oauth_last_error: str = ""
    discovered_tools_jsonb: list[dict[str, object]] = Field(default_factory=list)
    last_synced_at: str | None = None
    last_sync_status: Literal["never", "success", "failed"] = "never"
    last_sync_error: str = ""
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None
