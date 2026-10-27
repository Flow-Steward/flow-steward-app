"""Selector grammar and traversal for transform steps."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MISSING = object()


@dataclass(frozen=True)
class SelectorToken:
    kind: str
    value: str | int | None = None


def parse_transform_selector(selector: str, *, allow_wildcard: bool = True) -> list[SelectorToken]:
    text = str(selector or "").strip()
    if not text:
        raise ValueError("selector is required")
    tokens: list[SelectorToken] = []
    wildcard_count = 0
    i = 0
    expect_segment = True
    while i < len(text):
        char = text[i]
        if char == ".":
            if expect_segment:
                raise ValueError("empty selector segment")
            expect_segment = True
            i += 1
            continue
        if char == "[":
            close = _find_bracket_close(text, i)
            if close < 0:
                raise ValueError("unclosed selector bracket")
            raw = text[i + 1 : close].strip()
            if raw == "*":
                if not allow_wildcard:
                    raise ValueError("wildcard is not allowed here")
                wildcard_count += 1
                if wildcard_count > 1:
                    raise ValueError("nested wildcards are not supported in v1")
                tokens.append(SelectorToken("wildcard"))
            elif raw.startswith(("'", '"')):
                tokens.append(SelectorToken("key", _parse_quoted_key(raw)))
            else:
                if raw.startswith("-"):
                    raise ValueError("negative indexes are not supported")
                if not raw.isdigit():
                    raise ValueError("array index must be a non-negative integer")
                tokens.append(SelectorToken("index", int(raw)))
            expect_segment = False
            i = close + 1
            continue
        if char == "]":
            raise ValueError("unexpected selector bracket")
        start = i
        while i < len(text) and text[i] not in ".[":
            i += 1
        key = text[start:i].strip()
        if not key:
            raise ValueError("empty selector segment")
        if any(item in key for item in ("?", "(", ")", "{", "}")):
            raise ValueError("filters, functions, and interpolation are not supported")
        tokens.append(SelectorToken("key", key))
        expect_segment = False
    if expect_segment:
        raise ValueError("selector must not end with a dot")
    return tokens


def selector_has_wildcard(selector: str) -> bool:
    try:
        return any(token.kind == "wildcard" for token in parse_transform_selector(selector))
    except ValueError:
        return False


def select_value(root: Any, selector: str) -> tuple[Any, bool]:
    tokens = parse_transform_selector(selector)

    def _walk(cursor: Any, remaining: list[SelectorToken]) -> Any:
        if not remaining:
            return cursor
        token = remaining[0]
        rest = remaining[1:]
        if token.kind == "key":
            if not isinstance(cursor, dict) or token.value not in cursor:
                return MISSING
            return _walk(cursor.get(str(token.value)), rest)
        if token.kind == "index":
            if not isinstance(cursor, list):
                return MISSING
            index = int(token.value or 0)
            if index >= len(cursor):
                return MISSING
            return _walk(cursor[index], rest)
        if token.kind == "wildcard":
            if not isinstance(cursor, list):
                return MISSING
            values: list[Any] = []
            for item in cursor:
                selected = _walk(item, rest)
                if selected is MISSING:
                    continue
                values.append(selected)
            return values
        return MISSING

    value = _walk(root, tokens)
    return value, value is MISSING


def json_get(value: Any, path: str, default: Any = MISSING) -> Any:
    selected, missing = select_value(value, path)
    if missing:
        if default is not MISSING:
            return default
        raise ValueError(f"path '{path}' is missing")
    return selected


def transform_state_selector_error(config: dict[str, Any]) -> dict[str, Any] | None:
    from core.application.orchestration.workflow_state_selector_contract import (
        RUNTIME_ERROR_CODE,
        validate_step_state_selectors,
    )

    issues = validate_step_state_selectors(
        {"step_kind": "transform", "override": {"transform": config}}
    )
    if not issues:
        return None
    issue = issues[0]
    return {
        "ok": False,
        "errors": [
            {
                "reason": RUNTIME_ERROR_CODE,
                "selector": issue["selector"],
                "path": issue["path"].removeprefix("step."),
            }
        ],
        "state_patch": {},
        "detail": RUNTIME_ERROR_CODE,
    }


def _find_bracket_close(text: str, start: int) -> int:
    quote = ""
    escaped = False
    for idx in range(start + 1, len(text)):
        char = text[idx]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "]":
            return idx
    return -1


def _parse_quoted_key(raw: str) -> str:
    if len(raw) < 2 or raw[0] not in {"'", '"'} or raw[-1] != raw[0]:
        raise ValueError("quoted object key must use matching quotes")
    try:
        parsed = json.loads(raw) if raw[0] == '"' else raw[1:-1].replace("\\'", "'")
    except Exception as exc:
        raise ValueError("invalid quoted object key") from exc
    if not isinstance(parsed, str) or not parsed:
        raise ValueError("quoted object key must be non-empty text")
    return parsed


__all__ = [
    "MISSING",
    "SelectorToken",
    "json_get",
    "parse_transform_selector",
    "select_value",
    "selector_has_wildcard",
    "transform_state_selector_error",
]
