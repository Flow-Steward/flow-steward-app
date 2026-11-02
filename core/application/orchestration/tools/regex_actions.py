"""Bounded local regex actions; never return partial results after a limit error."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import regex
from langchain_core.tools import tool

_MAX_TEXT = 1_048_576
_MAX_PATTERN = 4096
_MAX_MATCHES = 1000
_MAX_FLAGS = 5
_TIMEOUT_SECONDS = 0.1
_REPLACEMENT_TOKENS = re.compile(r"\\(?:g<([^>]+)>|[0-7]{3}|([1-9](?a:\d)?)|.)", re.DOTALL)
_COUNTED_REPEAT = re.compile(r"((?a:\d+))(?:,((?a:\d*)))?")
_MAX_COMPILE_EXPANSION = 65_536
_FLAGS = {"i": regex.I, "m": regex.M, "s": regex.S, "x": regex.X, "a": regex.A}


def _check_compile_budget(pattern: str) -> None:
    # Compilation has no engine timeout. Count even sequential/literal repeats
    # conservatively, avoiding large eager expansions and nested multipliers.
    budget = max(1, len(pattern))
    index = 0
    while index < len(pattern):
        if pattern[index] == "\\":
            index += 2
            continue
        if pattern[index] == "{":
            end = pattern.find("}", index + 1)
            if end < 0:
                break
            content = pattern[index + 1 : end]
            # Comments can contain closing braces: do not guess where a verbose
            # counted repeat ends. Require ordinary counts in that case.
            if "#" in content:
                raise ValueError("regex_compile_limit")
            count = _COUNTED_REPEAT.fullmatch("".join(content.split()))
            if count:
                bounds = [part for part in count.groups() if part]
                if any(len(part.lstrip("0")) > 5 for part in bounds):
                    raise ValueError("regex_compile_limit")
                budget *= max(1, *(int(part) for part in bounds))
                if budget > _MAX_COMPILE_EXPANSION:
                    raise ValueError("regex_compile_limit")
            elif any(char.isdigit() for char in content):
                raise ValueError("regex_compile_limit")
            index = end
        index += 1


def _compile(value: str, pattern: str, flags: str) -> Any:
    if len(value) > _MAX_TEXT:
        raise ValueError("regex_input_limit")
    if len(pattern) > _MAX_PATTERN:
        raise ValueError("regex_pattern_limit")
    if len(flags) > _MAX_FLAGS:
        raise ValueError("regex_flags_limit")
    if len(set(flags)) != len(flags) or any(flag not in _FLAGS for flag in flags):
        raise ValueError("regex_invalid_flags")
    _check_compile_budget(pattern)
    options = regex.VERSION0
    for flag in flags:
        options |= _FLAGS[flag]
    try:
        compiled = regex.compile(pattern, options)
    except (regex.error, RecursionError, OverflowError, ValueError):
        raise ValueError("regex_invalid_pattern") from None
    if compiled.flags & regex.REVERSE:
        raise ValueError("regex_reverse_unsupported")
    return compiled


def _matches(compiled: Any, value: str, *, first: bool) -> Iterator[Any]:
    previous_end = 0
    try:
        for count, match in enumerate(compiled.finditer(value, timeout=_TIMEOUT_SECONDS), 1):
            if count > _MAX_MATCHES:
                raise ValueError("regex_match_limit")
            start, end = match.span()
            if not previous_end <= start <= end <= len(value):
                raise ValueError("regex_unsupported_span")
            previous_end = end
            yield match
            if first:
                return
    except TimeoutError:
        raise ValueError("regex_timeout") from None


def _find(value: str, pattern: str, flags: str, *, first: bool) -> str | list[str] | None:
    compiled = _compile(value, pattern, flags)
    found = []
    size = 0
    for match in _matches(compiled, value, first=first):
        text = match.group(0)
        size += len(text)
        if size > _MAX_TEXT:
            raise ValueError("regex_output_limit")
        found.append(text)
    if first:
        return found[0] if found else None
    return found


def _validate_replacement(compiled: Any, replacement: str) -> None:
    def remove_reference(token: re.Match[str]) -> str:
        group = token.group(1) or token.group(2)
        if group is None:
            return token.group(0)
        if group.isdecimal():
            if int(group) > compiled.groups:
                raise ValueError("regex_invalid_replacement")
        elif group not in compiled.groupindex:
            raise ValueError("regex_invalid_replacement")
        return ""

    # Remove verified references, then let the engine validate all remaining
    # escapes against a guaranteed empty match. No-match inputs still fail for
    # malformed templates, without compiling a second user-controlled pattern.
    escapes = _REPLACEMENT_TOKENS.sub(remove_reference, replacement)
    try:
        regex.fullmatch("", "").expand(escapes)
    except (regex.error, IndexError, KeyError, OverflowError, ValueError):
        raise ValueError("regex_invalid_replacement") from None


def _expand(match: Any, replacement: str) -> str:
    # Tokenize only trusted replacement syntax, measuring captures by spans so
    # neither the bound nor a repeated backreference allocates huge strings.
    size = len(replacement)
    try:
        for token in _REPLACEMENT_TOKENS.finditer(replacement):
            group = token.group(1) or token.group(2)
            if group is None:
                continue
            start, end = match.span(int(group) if group.isdecimal() else group)
            size += max(0, end - start) - len(token.group(0))
            if size > _MAX_TEXT:
                raise ValueError("regex_output_limit")
        return str(match.expand(replacement))
    except (regex.error, IndexError, KeyError, OverflowError):
        raise ValueError("regex_invalid_replacement") from None


def _replace(value: str, pattern: str, replacement: str, flags: str, *, first: bool) -> str:
    compiled = _compile(value, pattern, flags)
    if len(replacement) > _MAX_PATTERN:
        raise ValueError("regex_replacement_limit")
    _validate_replacement(compiled, replacement)
    chunks = []
    end = size = 0
    for match in _matches(compiled, value, first=first):
        prefix = value[end : match.start()]
        expanded = _expand(match, replacement)
        size += len(prefix) + len(expanded)
        if size > _MAX_TEXT:
            raise ValueError("regex_output_limit")
        chunks.extend((prefix, expanded))
        end = match.end()
    if size + len(value) - end > _MAX_TEXT:
        raise ValueError("regex_output_limit")
    chunks.append(value[end:])
    return "".join(chunks)


@tool
def regex_find(value: str, pattern: str, flags: str = "") -> dict[str, Any]:
    """Find the first full regex match, returning result as text or null."""
    return {"result": _find(value, pattern, flags, first=True)}


@tool
def regex_find_all(value: str, pattern: str, flags: str = "") -> dict[str, Any]:
    """Find ordered full regex matches, returning result as a string array."""
    return {"result": _find(value, pattern, flags, first=False)}


@tool
def regex_replace(value: str, pattern: str, replacement: str, flags: str = "") -> dict[str, Any]:
    """Replace the first regex match, with Python group references in replacement."""
    return {"result": _replace(value, pattern, replacement, flags, first=True)}


@tool
def regex_replace_all(
    value: str, pattern: str, replacement: str, flags: str = ""
) -> dict[str, Any]:
    """Replace all regex matches, with Python group references in replacement."""
    return {"result": _replace(value, pattern, replacement, flags, first=False)}
