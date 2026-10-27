"""Unit tests for the project ToolProvider runtime builder.

Retired inline HTTP definitions are not an executable tool source.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: empty project ToolProvider bindings
# ---------------------------------------------------------------------------


class TestBuildProviderToolsEmpty:
    """Empty bindings and inert legacy values produce no provider tools."""

    def test_empty_config_returns_empty_list(self):
        """project_config with no extra_tools → empty list."""
        from core.application.orchestration.tool_registry import build_project_provider_tools

        result = build_project_provider_tools({})
        assert result == []

    def test_none_extra_tools_returns_empty_list(self):
        """project_config with extra_tools=None → empty list."""
        from core.application.orchestration.tool_registry import build_project_provider_tools

        result = build_project_provider_tools({"extra_tools": None})
        assert result == []

    def test_empty_extra_tools_list_returns_empty(self):
        """project_config with extra_tools=[] → empty list."""
        from core.application.orchestration.tool_registry import build_project_provider_tools

        result = build_project_provider_tools({"extra_tools": []})
        assert result == []


class TestBuildProviderTools:
    def test_tool_provider_extension_tool_created_and_invoked(self):
        from core.application.orchestration.tool_registry import build_project_provider_tools

        config = {
            "project_providers": [
                {
                    "provider_key": "example_tool_provider",
                    "provider_type_key": "tool_provider",
                    "enabled": True,
                    "provider_enabled": True,
                    "implementation_kind": "extension",
                    "provider_config": {"extension_key": "example_tool_provider"},
                    "binding_config": {
                        "tools": [
                            {
                                "name": "echo_text",
                                "description": "Echo text through ToolProvider",
                                "tool_name": "echo_text",
                            }
                        ]
                    },
                }
            ]
        }

        with (
            patch(
                "core.application.orchestration.tools.tool_resolver._resolve_extension_record",
                return_value=(
                    {
                        "extension_key": "example_tool_provider",
                        "enabled": True,
                        "kind": "tool_provider",
                    },
                    MagicMock(),
                ),
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation",
                return_value={
                    "status": "succeeded",
                    "response": {"ok": True, "result": {"text": "hello"}},
                },
            ),
        ):
            tools = build_project_provider_tools(config)
            assert len(tools) == 1
            result = tools[0].invoke({"arguments_json": json.dumps({"text": "hello"})})

        assert "hello" in result

    def test_tool_provider_extension_failure_returns_error_text(self):
        from core.application.orchestration.tool_registry import build_project_provider_tools

        config = {
            "project_providers": [
                {
                    "provider_key": "example_tool_provider",
                    "provider_type_key": "tool_provider",
                    "enabled": True,
                    "provider_enabled": True,
                    "implementation_kind": "extension",
                    "provider_config": {"extension_key": "example_tool_provider"},
                    "binding_config": {
                        "tools": [{"name": "echo_text", "description": "Echo text"}]
                    },
                }
            ]
        }

        with (
            patch(
                "core.application.orchestration.tools.tool_resolver._resolve_extension_record",
                return_value=(
                    {
                        "extension_key": "example_tool_provider",
                        "enabled": True,
                        "kind": "tool_provider",
                    },
                    MagicMock(),
                ),
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation",
                return_value={"status": "failed", "detail": "provider crashed"},
            ),
        ):
            tools = build_project_provider_tools(config)
            result = tools[0].invoke({"arguments_json": json.dumps({"text": "hello"})})

        assert "[ERROR]" in result
        assert "provider crashed" in result
