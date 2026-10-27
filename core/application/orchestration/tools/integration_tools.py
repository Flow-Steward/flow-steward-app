"""Generic connector-facing integration tools (provider-agnostic)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool


def _parse_arguments_json(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except Exception as exc:
        return None, f"arguments_json must be valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "arguments_json must be a JSON object."
    return parsed, None


@tool
def mcp_connector_invoke(
    connector_id: str,
    function_name: str = "invoke",
    arguments_json: str = "{}",
) -> str:
    """Invoke any configured connector via the canonical connector runtime seam."""
    normalized_connector_id = str(connector_id or "").strip()
    if not normalized_connector_id:
        return "connector_id is required."

    input_payload, error = _parse_arguments_json(arguments_json)
    if error:
        return error

    _ = function_name, input_payload
    return (
        "Connector invocation requires workflow runtime scope; use a configured "
        "workflow connector step instead of the generic direct connector tool."
    )
