"""
BaseTool — the abstract base class for all Flow Steward custom tools.

Extend this to create new tools. The tool is registered via a manifest
in the DB registry and dynamically loaded at runtime by the ToolResolver.

Usage:
    from core.application.orchestration.tools.base import BaseTool
    from langchain_core.tools import tool

    class MyTool(BaseTool):
        tool_id = "my_custom_tool"

        def get_tools(self) -> list:
            timeout = self.config.get("timeout_seconds", 30)

            @tool
            def my_function(arg: str) -> str:
                '''Does something useful.'''
                return do_work(arg)

            return [my_function]

Then create a matching tool manifest in the DB via the admin UI,
with implementation.class = "MyTool" and implementation.module = "core.application.orchestration.tools.custom.my_tool".

And add the tool_id to any agent's `tools` list in the admin UI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """Base class for Flow Steward tools that produce LangChain-compatible callables.

    Subclasses must implement ``get_tools()`` which returns a list of
    @tool-decorated functions (or LangChain BaseTool instances) that can be
    passed to ``llm.bind_tools()``.

    For single-function tools, return a list with one item.
    For multi-function bundles, return multiple.
    """

    tool_id: str = ""  # Must match the YAML manifest tool_id

    def __init__(self, config: dict[str, Any] | None = None):
        """
        Args:
            config: Tool configuration from the YAML manifest's ``config``
                    section. Contains settings like timeout, rate_limit,
                    cost_tier, etc.
        """
        self.config = config or {}

    @abstractmethod
    def get_tools(self) -> list:
        """Return a list of LangChain-compatible tool callables.

        Each item must be a ``@tool``-decorated function or a LangChain
        ``BaseTool`` instance that can be passed to ``llm.bind_tools()``.
        """
        ...

    def validate(self) -> list[str]:
        """Optional: validate that required config/env vars are present.

        Returns a list of error messages. Empty list means valid.
        Called at startup to surface misconfigured tools early.
        Override in subclasses that need runtime dependencies (API keys, etc.).
        """
        return []
