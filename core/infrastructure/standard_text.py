"""Pure scalar text operations on normalized strings.

Adapters own coercion, parameter defaults, and validation. Title casing preserves
whitespace-delimited tokens; punctuation within a token does not start a new word.
Tabular feeds retain their native Polars expressions and their existing semantics.
"""

from __future__ import annotations

import re


def trim(value: str) -> str:
    return value.strip()


def upper(value: str) -> str:
    return value.upper()


def lower(value: str) -> str:
    return value.lower()


def title_case(value: str) -> str:
    """Capitalize alphabetic token starts without changing whitespace or punctuation."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if not token or not token[0].isalpha():
            return token
        return token[0].upper() + token[1:].lower()

    return re.sub(r"\S+", _replace, value)


def replace(value: str, find: str, replace_with: str) -> str:
    return value.replace(find, replace_with)


def append(value: str, suffix: str) -> str:
    return value + suffix


def length(value: str) -> int:
    return len(value)


def pad(value: str, width: int, fill_char: str, side: str) -> str:
    """Pad using an already validated single character and left/right side."""
    padding = max(0, width - len(value))
    return f"{fill_char * padding}{value}" if side == "left" else f"{value}{fill_char * padding}"
