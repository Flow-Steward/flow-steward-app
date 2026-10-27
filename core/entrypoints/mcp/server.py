"""Flow Steward MCP transport facade backed only by the application API."""

from __future__ import annotations

import core.entrypoints.mcp.server_job_tools
import core.entrypoints.mcp.server_project_workflow_tools
import core.entrypoints.mcp.server_report_notification_tools
import core.entrypoints.mcp.server_workflow_authoring_tools
import core.entrypoints.mcp.server_workflow_hook_tools
import core.entrypoints.mcp.server_workflow_portability_tools  # noqa: F401
from core.entrypoints.mcp.server_main import main
from core.entrypoints.mcp.server_runtime import mcp

__all__ = ["main", "mcp"]


if __name__ == "__main__":
    main()
