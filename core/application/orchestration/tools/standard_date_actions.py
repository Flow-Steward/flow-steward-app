"""Connector date tools share the application dispatcher and its action schemas."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import ConfigDict, create_model

from core.application.orchestration.date_actions import build_date_actions, execute_date_action
from core.application.orchestration.transform_action_contract import TransformAction


def _connector_tool(action: TransformAction) -> StructuredTool:
    # Keep inputs uncoerced so the common date adapter owns named validation errors.
    # Dynamic fields also preserve the public `from` parameter of date.diff.
    fields: dict[str, Any] = {"value": (Any, ...)}
    for name, schema in action.params_schema["properties"].items():
        default = ... if name in action.required_params else schema.get("default")
        fields[name] = (Any, default)
    args_schema = create_model(
        f"StandardDate_{action.tool_action_id}_Args",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )

    def _execute(value: Any, **params: Any) -> dict[str, Any]:
        return {"result": execute_date_action(action.action_id, value, params)}

    return StructuredTool.from_function(
        func=_execute,
        name=action.tool_action_id,
        description=f"{action.display_name} using deterministic date operations.",
        args_schema=args_schema,
        infer_schema=False,
    )


for _action in build_date_actions().values():
    globals()[_action.tool_action_id] = _connector_tool(_action)
