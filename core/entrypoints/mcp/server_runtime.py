"""Minimal MCP protocol runtime; product capabilities live behind ``fs-app``."""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

from core.entrypoints.mcp.server_api_auth import AppApiTokenVerifier
from core.entrypoints.mcp.server_caller_identity import McpCallerIdentityMiddleware
from core.entrypoints.mcp.server_discovery import McpToolDiscoveryMiddleware
from core.entrypoints.mcp.server_maintenance import McpMaintenanceMiddleware
from core.entrypoints.mcp.server_public_identity import mcp_auth_urls
from core.infrastructure.runtime_env import runtime_env
from mcp.server import MCPServer

logger = logging.getLogger("platform.mcp_server")


def _env_flag(name: str, default: bool = False) -> bool:
    value = str(runtime_env.get(name, "1" if default else "0")).strip().lower()
    return value in {"1", "true", "yes", "on"}


_auth_enabled_value = str(runtime_env.get("FS_MCP_AUTH_ENABLED", "")).strip()
if _auth_enabled_value:
    MCP_AUTH_ENABLED = _env_flag("FS_MCP_AUTH_ENABLED")
else:
    MCP_AUTH_ENABLED = not _env_flag("FS_MCP_AUTH_DISABLED")

token_verifier: Any = None
auth_settings: Any = None
#: The issuer and protected resource this server advertises (empty with auth off).
MCP_ISSUER_URL = ""
MCP_RESOURCE_URL = ""
if MCP_AUTH_ENABLED:
    try:
        from mcp.server.auth.settings import AuthSettings

        # P1-5: derived from the public origin when not set explicitly, so a public
        # client is never sent to a development name to discover how to authenticate.
        # Read by name through the canonical runtime environment, so each variable
        # stays on the audited, registered surface.
        issuer_url, resource_url = mcp_auth_urls(
            {
                "FS_PUBLIC_BASE_URL": str(runtime_env.get("FS_PUBLIC_BASE_URL") or ""),
                "FS_MCP_ISSUER_URL": str(runtime_env.get("FS_MCP_ISSUER_URL") or ""),
                "FS_MCP_RESOURCE_URL": str(runtime_env.get("FS_MCP_RESOURCE_URL") or ""),
            }
        )
        MCP_ISSUER_URL, MCP_RESOURCE_URL = issuer_url, resource_url
        token_verifier = AppApiTokenVerifier(
            resource_server_url=resource_url,
            issuer_url=issuer_url,
        )
        auth_settings = AuthSettings(
            issuer_url=cast(Any, issuer_url),
            resource_server_url=cast(Any, resource_url),
            # ADR 0047 decision 14: an OAuth access token carries the resource it was
            # issued for and the SDK refuses any other; PATs, machine tokens and
            # sessions are vouched for by the verifier as valid for this resource.
            # Ships together with the verifier change, or every PAT would be refused.
            validate_token_resource=True,
        )
    except Exception:
        logger.exception("MCP auth initialization failed")
        sys.exit(1)
else:
    logger.warning("MCP auth disabled by explicit local-development configuration")


mcp = MCPServer(
    name="Flow Steward",
    version="2.0.0",
    instructions=(
        "Flow Steward project, workflow, job, report and notification "
        "tools. Every tool delegates the caller's bearer to the canonical application API. "
        "For workflow authoring, search local examples and reuse a suitable workflow first. "
        "Obtain live bundle/step schemas and operation contracts, validate and repair, "
        "then save a disabled draft. Project-name resolution additionally requires projects.read "
        "or current broader project-read authority. Draft creation requires an admin user "
        "with admin:all on a token; machine tokens retain their current operator role. "
        "Missing agents and skills are created with agent_create/skill_create from the "
        "agent_authoring_schema/skill_authoring_schema bodies, read with agent_get/skill_get "
        "(disabled ones included) and changed with targeted agent_update/skill_update. "
        "agent_set_enabled, skill_set_enabled and workflow_set_enabled set the desired "
        "enabled state explicitly and require the same admin authority. A 409 conflict is "
        "reported, never resolved by renaming. Use Test Run and result interfaces for "
        "execution evidence; validation does not verify execution or external effects, "
        "and Test Run may execute external operations."
    ),
    middleware=[
        McpCallerIdentityMiddleware(auth_enabled=MCP_AUTH_ENABLED),
        McpMaintenanceMiddleware(),
        McpToolDiscoveryMiddleware(auth_enabled=MCP_AUTH_ENABLED),
    ],
    token_verifier=token_verifier,
    auth=auth_settings,
)


__all__ = ["MCP_AUTH_ENABLED", "MCP_ISSUER_URL", "MCP_RESOURCE_URL", "logger", "mcp"]
