"""One interpretation of model token usage for accounting, cost and telemetry.

Providers report usage in three shapes, and only the shape says whether cache
tokens are already part of the input count:

- LangChain ``usage_metadata``, OpenAI ``prompt_tokens`` and Gemini counts
  include cached tokens in the input; the cache buckets are a subset.
- Raw Anthropic ``usage`` (``cache_*_input_tokens`` next to ``input_tokens``)
  excludes them; the buckets are added to get the input.

A missing count stays ``None`` and is never replaced by zero; a reported zero
stays zero. Every count records whether the provider reported it or it was
derived from reported components.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

REPORTED = "reported"
DERIVED = "derived"

# The flow_steward-owned key a model adapter uses to hand normalized usage to
# the callback when LangChain's usage_metadata cannot carry an unknown count.
LLM_OUTPUT_USAGE_KEY = "flow_steward_usage"

_INPUT_KEYS = ("prompt_tokens", "input_tokens", "prompt_token_count")
_OUTPUT_KEYS = ("completion_tokens", "output_tokens", "candidates_token_count")
_TOTAL_KEYS = ("total_tokens", "total_token_count")
_DETAIL_KEYS = ("input_token_details", "prompt_tokens_details", "input_tokens_details")
_DETAIL_READ_KEYS = ("cache_read", "cached_tokens", "priority_cache_read", "flex_cache_read")
_DETAIL_WRITE_KEYS = ("cache_creation", "cache_write_tokens")
_EPHEMERAL_WRITE_KEYS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
_RAW_READ_KEYS = ("cache_read_input_tokens", "cached_content_token_count")
_RAW_WRITE_KEYS = ("cache_creation_input_tokens",)
_ANTHROPIC_RAW_KEYS = ("cache_creation_input_tokens", "cache_read_input_tokens")

_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


@dataclass(frozen=True)
class TokenUsage:
    """Usage of one model call; ``input_tokens`` always includes cache tokens."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    provenance: Mapping[str, str] = field(default_factory=dict)

    @property
    def uncached_input_tokens(self) -> int | None:
        """Input billed at the base rate: the input minus both cache buckets."""
        if self.input_tokens is None:
            return None
        cached = (self.cache_read_tokens or 0) + (self.cache_write_tokens or 0)
        return max(self.input_tokens - cached, 0)

    def as_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {name: getattr(self, name) for name in _FIELDS}
        values["provenance"] = dict(self.provenance)
        return values

    def to_langchain_usage_metadata(self) -> dict[str, Any] | None:
        """LangChain ``UsageMetadata``, which needs known input and output counts."""
        if self.input_tokens is None or self.output_tokens is None:
            return None
        metadata: dict[str, Any] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": (
                self.total_tokens
                if self.total_tokens is not None
                else self.input_tokens + self.output_tokens
            ),
        }
        details = {
            key: value
            for key, value in (
                ("cache_read", self.cache_read_tokens),
                ("cache_creation", self.cache_write_tokens),
            )
            if value is not None
        }
        if details:
            metadata["input_token_details"] = details
        return metadata


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:  # NaN or negative
        return None
    return int(value)


def _first_count(source: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _count(source.get(key))
        if value is not None:
            return value
    return None


def _details(usage: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in _DETAIL_KEYS:
        value = usage.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _detail_write(details: Mapping[str, Any]) -> int | None:
    ephemeral = [_count(details.get(key)) for key in _EPHEMERAL_WRITE_KEYS]
    known = [value for value in ephemeral if value is not None]
    if known and sum(known) > 0:
        # langchain-anthropic zeroes the generic bucket when it splits by TTL.
        return sum(known)
    return _first_count(details, _DETAIL_WRITE_KEYS)


def _build(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    cache_read: int | None,
    cache_write: int | None,
    cache_in_input: bool,
) -> TokenUsage | None:
    if all(v is None for v in (input_tokens, output_tokens, total_tokens, cache_read, cache_write)):
        return None
    provenance: dict[str, str] = {}
    if not cache_in_input and input_tokens is not None and (cache_read or cache_write):
        input_tokens += (cache_read or 0) + (cache_write or 0)
        provenance["input_tokens"] = DERIVED
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
        provenance["total_tokens"] = DERIVED
    for name, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("total_tokens", total_tokens),
        ("cache_read_tokens", cache_read),
        ("cache_write_tokens", cache_write),
    ):
        if value is not None:
            provenance.setdefault(name, REPORTED)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        provenance=provenance,
    )


def usage_from_mapping(
    usage: Mapping[str, Any] | None,
    *,
    cache_details: Mapping[str, Any] | None = None,
) -> TokenUsage | None:
    """Normalize one provider or LangChain usage mapping.

    ``cache_details`` are raw Anthropic counts found next to a mapping that has
    no cache breakdown of its own (older langchain-anthropic messages).
    """
    if not isinstance(usage, Mapping) or not usage:
        return None
    details = _details(usage)
    cache_read = _first_count(details, _DETAIL_READ_KEYS)
    cache_write = _detail_write(details)
    cache_in_input = True
    if cache_read is None and cache_write is None:
        raw = usage
        if not any(key in usage for key in _ANTHROPIC_RAW_KEYS) and isinstance(
            cache_details, Mapping
        ):
            raw = cache_details
        cache_read = _first_count(raw, _RAW_READ_KEYS)
        cache_write = _first_count(raw, _RAW_WRITE_KEYS)
        cache_in_input = not any(key in raw for key in _ANTHROPIC_RAW_KEYS)
    return _build(
        input_tokens=_first_count(usage, _INPUT_KEYS),
        output_tokens=_first_count(usage, _OUTPUT_KEYS),
        total_tokens=_first_count(usage, _TOTAL_KEYS),
        cache_read=cache_read,
        cache_write=cache_write,
        cache_in_input=cache_in_input,
    )


def usage_from_extension_payload(usage: Any) -> TokenUsage | None:
    """Normalize the usage an LLM provider extension returns.

    The extension contract follows OpenAI: ``input_tokens`` includes cached
    tokens, so the cache counts are a subset however they are named.
    """
    if not isinstance(usage, Mapping) or not usage:
        return None
    return _build(
        input_tokens=_count(usage.get("input_tokens")),
        output_tokens=_count(usage.get("output_tokens")),
        total_tokens=_count(usage.get("total_tokens")),
        cache_read=_first_count(usage, ("cache_read_input_tokens", "cache_read_tokens")),
        cache_write=_first_count(usage, ("cache_creation_input_tokens", "cache_creation_tokens")),
        cache_in_input=True,
    )


def _from_normalized(value: Any) -> TokenUsage | None:
    if not isinstance(value, Mapping):
        return None
    counts = {name: _count(value.get(name)) for name in _FIELDS}
    if all(count is None for count in counts.values()):
        return None
    provenance = value.get("provenance")
    return TokenUsage(
        **counts,
        provenance={
            name: str(provenance[name])
            for name in _FIELDS
            if counts[name] is not None
            and isinstance(provenance, Mapping)
            and provenance.get(name) in (REPORTED, DERIVED)
        },
    )


def usage_from_message(message: Any) -> TokenUsage | None:
    """Normalize the usage carried by one LangChain chat message."""
    usage_metadata = getattr(message, "usage_metadata", None)
    response_metadata = getattr(message, "response_metadata", None)
    if not isinstance(response_metadata, Mapping):
        response_metadata = {}
    raw_usage = response_metadata.get("usage")
    raw_usage = raw_usage if isinstance(raw_usage, Mapping) else None
    if isinstance(usage_metadata, Mapping) and usage_metadata:
        return usage_from_mapping(usage_metadata, cache_details=raw_usage)
    if raw_usage:
        return usage_from_mapping(raw_usage)
    return usage_from_mapping(response_metadata)


def _generation_messages(response: Any) -> list[Any]:
    messages: list[Any] = []
    for generations in getattr(response, "generations", None) or []:
        for generation in generations if isinstance(generations, list) else []:
            message = getattr(generation, "message", None)
            if message is not None:
                messages.append(message)
    return messages


def usage_from_llm_result(response: Any) -> TokenUsage | None:
    """Normalize the usage of one LangChain ``LLMResult`` (one model call)."""
    llm_output = getattr(response, "llm_output", None)
    llm_output = llm_output if isinstance(llm_output, Mapping) else {}
    normalized = _from_normalized(llm_output.get(LLM_OUTPUT_USAGE_KEY))
    if normalized is not None:
        return normalized
    for key in ("token_usage", "usage"):
        usage = usage_from_mapping(llm_output.get(key))
        if usage is not None:
            return usage
    for message in _generation_messages(response):
        usage = usage_from_message(message)
        if usage is not None:
            return usage
    return None
