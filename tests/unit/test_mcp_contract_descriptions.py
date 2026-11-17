"""MCP tool descriptions state the rules the server enforces.

An AI client reads only the tool description and its input schema. When the
server refuses something the description does not mention, the client learns
the rule by failing a call (MCP probe 2026-11-17, F2 and F6).
"""

from __future__ import annotations

from typing import Any


def _tool(name: str) -> Any:
    from core.entrypoints.mcp.server import mcp

    return mcp._tool_manager._tools[name]


def test_trigger_and_contract_say_inputs_with_a_default_are_fixed() -> None:
    contract = str(_tool("workflow_contract").description)
    trigger = str(_tool("workflow_trigger").description)

    assert "caller_may_supply" in contract
    assert "input_schema" in contract
    assert "configured default" in trigger
    assert "override_forbidden" in trigger


def test_approval_decide_states_when_a_comment_is_required() -> None:
    tool = _tool("job_approval_decide")
    properties = tool.parameters["properties"]

    comment = properties["comment"]["description"]
    assert "Required" in comment
    assert "abort" in comment
    assert "abort" in properties["action"]["description"]
    assert "cancelled" in properties["action"]["description"]
    assert "comment is required when rejecting an approval" in str(tool.description)
