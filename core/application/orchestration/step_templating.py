"""Shared state path and templating helpers for deterministic workflow steps."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from core.application.orchestration.state_path_guards import (
    RESERVED_STATE_ROOTS,
    canonical_state_write_parts,
)
from core.application.orchestration.state_paths import (
    resolve_state_path_with_workflow_data,
)

STATE_PREFIX = "state."

_TEMPLATE_RE = re.compile(r"\{\{\s*+([^{}]++)\}\}")


def _resolve_path(state: dict[str, Any], raw_path: str) -> Any:
    path = str(raw_path or "").strip()
    if not path:
        return None
    if path.startswith(STATE_PREFIX):
        path = path[len(STATE_PREFIX) :]
    if not path:
        return state

    # The shared selector language also owns the secondary workflow_data mirror
    # policy, including the distinction between a present null and a missing path.
    resolved, found = resolve_state_path_with_workflow_data(state, path)
    return resolved if found else None


def _is_source_reference(value: Any) -> bool:
    if not isinstance(value, dict) or set(value.keys()) != {"source"}:
        return False
    source = value.get("source")
    if not isinstance(source, str):
        return False
    normalized = source.strip()
    return normalized == "state" or normalized.startswith(STATE_PREFIX)


def _parse_literal(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        return text.strip("'\"")


def _split_default_filter(selector: str) -> tuple[str, Any, bool]:
    text = str(selector or "").strip()
    if "|" not in text:
        return text, None, False
    path, _, filter_text = text.partition("|")
    filter_text = filter_text.strip()
    if not filter_text.startswith("default"):
        return text, None, False
    _, sep, raw_default = filter_text.partition(":")
    if not sep:
        start = filter_text.find("(")
        end = filter_text.rfind(")")
        raw_default = filter_text[start + 1 : end] if start >= 0 and end > start else ""
    return path.strip(), _parse_literal(raw_default), True


def _render_template_string(template: str, state: dict[str, Any]) -> str:
    text = str(template or "")

    def _replace(match: re.Match[str]) -> str:
        selector, default_value, has_default = _split_default_filter(
            str(match.group(1) or "").strip()
        )
        value = _resolve_path(state, selector)
        if value in (None, "") and has_default:
            value = default_value
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    return _TEMPLATE_RE.sub(_replace, text)


def _render_value(value: Any, state: dict[str, Any]) -> Any:
    if _is_source_reference(value):
        return _resolve_path(state, str(value.get("source") or "").strip())
    if isinstance(value, str):
        return _render_template_string(value, state)
    if isinstance(value, list):
        return [_render_value(item, state) for item in value]
    if isinstance(value, dict):
        return {str(k): _render_value(v, state) for k, v in value.items()}
    return value


def set_state_path(
    update: dict[str, Any],
    source_state: dict[str, Any],
    target: str,
    value: Any,
    *,
    mirror_workflow_data: bool = True,
) -> None:
    canonical_parts = canonical_state_write_parts(target)
    if (
        canonical_parts is None
        or not canonical_parts
        or canonical_parts[0].strip() in RESERVED_STATE_ROOTS
    ):
        return
    parts = list(canonical_parts)

    def _set_workflow_data(value_to_store: Any) -> None:
        if not mirror_workflow_data or parts[0] == "workflow_data":
            return
        workflow_data = update.get("workflow_data")
        if not isinstance(workflow_data, dict):
            base = source_state.get("workflow_data")
            workflow_data = copy.deepcopy(base) if isinstance(base, dict) else {}
        cursor = workflow_data
        for part in parts[:-1]:
            next_cursor = cursor.get(part)
            if not isinstance(next_cursor, dict):
                next_cursor = {}
                cursor[part] = next_cursor
            cursor = next_cursor
        cursor[parts[-1]] = value_to_store
        update["workflow_data"] = workflow_data

    root = parts[0]
    if len(parts) == 1:
        update[root] = value
        _set_workflow_data(value)
        return

    existing = update.get(root)
    if not isinstance(existing, dict):
        base = source_state.get(root)
        existing = copy.deepcopy(base) if isinstance(base, dict) else {}
    cursor = existing
    for part in parts[1:-1]:
        next_cursor = cursor.get(part)
        if not isinstance(next_cursor, dict):
            next_cursor = {}
            cursor[part] = next_cursor
        cursor = next_cursor
    cursor[parts[-1]] = value
    update[root] = existing
    _set_workflow_data(value)
