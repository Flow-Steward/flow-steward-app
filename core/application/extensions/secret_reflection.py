"""Provider-neutral bounded guard against reflected runtime credentials."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

_EMBEDDED_SECRET_MIN_LENGTH = 8
_MAX_SECRET_COUNT = 256
_MAX_SECRET_CODEPOINTS = 65_536
_MAX_CANDIDATE_STRINGS = 16_384
_MAX_COMPARISON_CODEPOINTS = 8 * 1024 * 1024
_MAX_TRAVERSAL_NODES = 65_536
_MAX_NESTING_DEPTH = 64
SECRET_EXPOSURE_CODE = "extension_secret_exposure_blocked"
SECRET_EXPOSURE_MESSAGE = (
    "The extension returned protected credential material. Contact the extension author."
)


def candidate_exposes_secret(candidate: str, secret: str) -> bool:
    if not secret:
        return False
    if candidate == secret or len(secret) >= _EMBEDDED_SECRET_MIN_LENGTH:
        return secret in candidate
    # Unicode ``\w`` is ``str.isalnum()`` plus underscore; subtracting underscore
    # preserves the existing boundary semantics while the regex search stays in C.
    alphanumeric = r"[^\W_]"
    return (
        re.search(
            rf"(?<!{alphanumeric}){re.escape(secret)}(?!{alphanumeric})",
            candidate,
        )
        is not None
    )


def _dict_nodes(
    value: dict[Any, Any],
    *,
    depth: int,
    string_keys_are_candidates: bool,
) -> Iterator[tuple[Any, int, bool, bool]]:
    for key, nested in value.items():
        yield key, depth, string_keys_are_candidates and isinstance(key, str), False
        yield nested, depth, True, True


def _sequence_nodes(
    value: list[Any] | tuple[Any, ...], *, depth: int
) -> Iterator[tuple[Any, int, bool, bool]]:
    for nested in value:
        yield nested, depth, True, True


def _nested_nodes(
    value: Any, *, string_keys_are_candidates: bool
) -> Iterator[tuple[Any, int, bool]]:
    pending: list[Iterator[tuple[Any, int, bool, bool]]] = [iter(((value, 0, True, True),))]
    while pending:
        try:
            node, depth, is_candidate, descend = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        yield node, depth, is_candidate
        if not descend:
            continue
        if isinstance(node, dict):
            pending.append(
                _dict_nodes(
                    node,
                    depth=depth + 1,
                    string_keys_are_candidates=string_keys_are_candidates,
                )
            )
        elif isinstance(node, (list, tuple)):
            pending.append(_sequence_nodes(node, depth=depth + 1))


def collect_secret_values(value: Any) -> tuple[frozenset[str], bool]:
    """Collect nested secret values within closed node, depth, and size budgets."""
    secrets: set[str] = set()
    codepoints = 0
    for node_count, (node, depth, is_candidate) in enumerate(
        _nested_nodes(value, string_keys_are_candidates=False), start=1
    ):
        if node_count > _MAX_TRAVERSAL_NODES or depth > _MAX_NESTING_DEPTH:
            return frozenset(), True
        if not is_candidate or not isinstance(node, str) or not node or node in secrets:
            continue
        secrets.add(node)
        codepoints += len(node)
        if len(secrets) > _MAX_SECRET_COUNT or codepoints > _MAX_SECRET_CODEPOINTS:
            return frozenset(), True
    return frozenset(secrets), False


def value_exposes_secret(value: Any, secrets: Iterable[str]) -> bool:
    normalized_secrets: list[str] = []
    secret_codepoints = 0
    for secret in secrets:
        if not isinstance(secret, str):
            return True
        if not secret:
            continue
        normalized_secrets.append(secret)
        secret_codepoints += len(secret)
        if (
            len(normalized_secrets) > _MAX_SECRET_COUNT
            or secret_codepoints > _MAX_SECRET_CODEPOINTS
        ):
            return True
    if not normalized_secrets:
        return False
    remaining_work = _MAX_COMPARISON_CODEPOINTS
    candidate_count = 0
    for node_count, (node, depth, is_candidate) in enumerate(
        _nested_nodes(value, string_keys_are_candidates=True), start=1
    ):
        if node_count > _MAX_TRAVERSAL_NODES or depth > _MAX_NESTING_DEPTH:
            return True
        if not is_candidate or not isinstance(node, str):
            continue
        candidate = node
        candidate_count += 1
        if candidate_count > _MAX_CANDIDATE_STRINGS:
            return True
        comparison_work = max(1, len(candidate)) * len(normalized_secrets)
        if comparison_work > remaining_work:
            return True
        remaining_work -= comparison_work
        if any(candidate_exposes_secret(candidate, secret) for secret in normalized_secrets):
            return True
    return False


def secret_exposure_invocation() -> dict[str, Any]:
    """Return the canonical fail-closed invocation for reflected credentials."""
    return {
        "status": "failed",
        "detail": SECRET_EXPOSURE_CODE,
        "response": {
            "ok": False,
            "error": SECRET_EXPOSURE_MESSAGE,
            "error_code": SECRET_EXPOSURE_CODE,
        },
        "errors": [{"code": SECRET_EXPOSURE_CODE}],
    }


__all__ = [
    "SECRET_EXPOSURE_CODE",
    "SECRET_EXPOSURE_MESSAGE",
    "candidate_exposes_secret",
    "collect_secret_values",
    "secret_exposure_invocation",
    "value_exposes_secret",
]
