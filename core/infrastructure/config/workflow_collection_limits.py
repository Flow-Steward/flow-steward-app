"""The one operator limit on how many rows or items a workflow handles at once.

`FS_DATASET_QUERY_MAX_RUNTIME_ROWS` (default 1000) bounds both a `dataset_query`
result and the items one For each pass walks (ADR 0038). It is a deployment
decision: a dataset query asking for more is refused, and a For each over a
longer collection takes the first N items and reports the truncation.

The value must be a positive whole number. A typo is an error, never a silent
fall back to the default the operator did not choose.
"""

from __future__ import annotations

from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

COLLECTION_ITEM_LIMIT_ENV = "FS_DATASET_QUERY_MAX_RUNTIME_ROWS"
DEFAULT_COLLECTION_ITEM_LIMIT = 1000


class InvalidCollectionLimitError(ValueError):
    """The configured limit cannot be honoured as written."""


def positive_int(value: Any) -> int | None:
    """A positive whole number from an int or digit string; booleans never count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    raw = str(value or "").strip()
    if not raw.isdigit():
        return None
    parsed = int(raw)
    return parsed if parsed > 0 else None


def resolve_collection_item_limit() -> int:
    """The deployment's N, or the reason the configured one is unusable."""
    raw = _runtime_env.getenv(COLLECTION_ITEM_LIMIT_ENV)
    if raw is None:
        return DEFAULT_COLLECTION_ITEM_LIMIT
    resolved = positive_int(raw)
    if resolved is None:
        raise InvalidCollectionLimitError(
            f"{COLLECTION_ITEM_LIMIT_ENV} must be a positive whole number, got {str(raw)!r}"
        )
    return resolved


__all__ = [
    "COLLECTION_ITEM_LIMIT_ENV",
    "DEFAULT_COLLECTION_ITEM_LIMIT",
    "InvalidCollectionLimitError",
    "positive_int",
    "resolve_collection_item_limit",
]
