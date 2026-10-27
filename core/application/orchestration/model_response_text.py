"""Provider-neutral text extraction for chat-model responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _text_fragments(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        return [value.decode("utf-8", errors="replace")]
    if isinstance(value, (list, tuple)):
        fragments: list[str] = []
        for item in value:
            fragments.extend(_text_fragments(item))
        return fragments
    if isinstance(value, Mapping):
        for key in ("text", "content", "output_text", "value"):
            if key in value:
                return _text_fragments(value.get(key))
        if value.get("type"):
            return []
        return [str(dict(value))]
    for attribute in ("text", "content", "output_text", "value"):
        if hasattr(value, attribute):
            candidate = getattr(value, attribute)
            if candidate is not None:
                return _text_fragments(candidate)
    return [str(value)]


def model_response_text(response: object) -> str:
    """Flatten string or block-style provider content into parser-safe text.

    LangChain chat responses may expose ``content`` as a string or as a list of
    typed blocks. Reasoning/tool blocks without textual content are ignored;
    textual blocks are concatenated in provider order.
    """

    content = getattr(response, "content", response)
    return "".join(_text_fragments(content))


__all__ = ["model_response_text"]
