"""Action descriptors shared by transform execution and its UI catalog."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_TABULAR_FEED_ACTION_IDS = {
    "date.parse",
    "date.to_utc",
    "date.to_timezone",
    "date.format",
    "date.add",
    "date.part",
    "date.start_of",
    "date.end_of",
    "text.html_to_text",
    "text.trim",
    "text.upper",
    "text.lower",
    "text.title_case",
    "text.replace",
    "text.append",
    "text.length",
    "text.pad",
    "number.coerce",
    "number.add",
    "number.subtract",
    "number.multiply",
    "number.divide",
    "number.round",
    "number.floor",
    "number.ceil",
    "money.coerce",
    "money.add",
    "money.subtract",
    "money.multiply",
    "money.round",
    "boolean.coerce",
    "map.values",
}


@dataclass(frozen=True)
class TransformAction:
    action_id: str
    category: str
    display_name: str
    value_scope: str
    input_type: str
    output_type: str
    params_schema: dict[str, Any]
    required_params: tuple[str, ...]
    param_sources: dict[str, dict[str, Any]]
    func: Callable[[Any, dict[str, Any]], Any]
    source: str = "standard_tool"
    tool_id: str = ""
    tool_action_id: str = ""
    transform_exposure: str = "auto"
    pure: bool = True
    deterministic: bool = True
    io_allowed: bool = False
    transform_safe: bool = True

    def contract(self) -> dict[str, Any]:
        params_schema = copy.deepcopy(self.params_schema)
        params_schema["required"] = list(self.required_params)
        return {
            "action_id": self.action_id,
            "category": self.category,
            "display_name": self.display_name,
            "source": self.source,
            "tool_id": self.tool_id,
            "tool_action_id": self.tool_action_id,
            "transform_exposure": self.transform_exposure,
            "value_scope": self.value_scope,
            "input_type": self.input_type,
            "output_type": self.output_type,
            "params_schema": params_schema,
            "param_sources": copy.deepcopy(self.param_sources),
            "pure": self.pure,
            "deterministic": self.deterministic,
            "io_allowed": self.io_allowed,
            "transform_safe": self.transform_safe,
            "backends": {
                "scalar": True,
                "polars": self.action_id in _TABULAR_FEED_ACTION_IDS,
            },
        }
