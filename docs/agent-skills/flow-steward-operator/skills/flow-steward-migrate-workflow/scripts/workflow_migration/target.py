"""Target step/edge construction and strict source-option checks."""

from __future__ import annotations

from typing import Any

from .contracts import require


def step(step_id: str, kind: str, config: dict[str, Any]) -> dict[str, Any]:
    return {"step_id": step_id, "step_kind": kind, "override": {kind: config}}


def edge(source: str, outcome: str, target: str) -> dict[str, Any]:
    return {
        "source": {"kind": "step", "id": source},
        "outcome": {"name": outcome},
        "target": {"kind": "step", "id": target},
        "rework": False,
    }


def qualified_keys(
    data: dict[str, Any], allowed: set[str], path: str, *, code: str = "node_option_unsupported"
) -> None:
    require(not (set(data) - allowed), code, path, "An option has no qualified target equivalent.")
