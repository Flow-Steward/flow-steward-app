"""How many rows one `dataset_query` run may materialise.

A `dataset_query` step materialises its whole result set into workflow state, so
the ceiling is a deployment decision the operator owns, not a per-workflow one.
`FS_DATASET_QUERY_MAX_RUNTIME_ROWS` is that single ceiling, read by the one
getter in `core.infrastructure.config.workflow_collection_limits` that also
bounds a For each pass; a workflow's `row_limit` is the request, and it is
approved only when it fits underneath.

Raising the maximum increases load on the source database, the worker's memory
and checkpoint storage in equal measure, because the rows are held in state for
the rest of the run. For volumes that do not fit comfortably, prepare an export
on the source side (URL, FTP/SFTP, upload or email) and read the file instead.

Preview limits are separate and deliberately untouched.
"""

from __future__ import annotations

from typing import Any

from core.infrastructure.config.workflow_collection_limits import (
    COLLECTION_ITEM_LIMIT_ENV,
    InvalidCollectionLimitError,
    positive_int,
    resolve_collection_item_limit,
)

DATASET_QUERY_MAX_RUNTIME_ROWS_ENV = COLLECTION_ITEM_LIMIT_ENV
DEFAULT_DATASET_QUERY_ROW_LIMIT = 100


class InvalidDatasetQueryLimitError(InvalidCollectionLimitError):
    """A dataset query row limit that cannot be honoured as configured."""


def resolve_dataset_query_row_limit(requested: Any) -> int:
    """Approve a step's requested `row_limit` against the deployment maximum.

    Only an absent value takes the step default. Zero is a request for no rows, so
    answering it with 100 would return a result the author did not ask for; it is
    refused like any other unusable value.
    """
    try:
        maximum = resolve_collection_item_limit()
    except InvalidCollectionLimitError as exc:
        raise InvalidDatasetQueryLimitError(str(exc)) from exc
    if requested is None or (isinstance(requested, str) and not requested.strip()):
        return min(DEFAULT_DATASET_QUERY_ROW_LIMIT, maximum)
    resolved = positive_int(requested)
    if resolved is None:
        raise InvalidDatasetQueryLimitError(
            f"dataset_query row_limit must be a positive whole number of rows, "
            f"got {str(requested)!r}"
        )
    if resolved > maximum:
        raise InvalidDatasetQueryLimitError(
            f"dataset_query row_limit {resolved} exceeds the deployment maximum of {maximum} "
            f"rows. Lower row_limit, or raise {DATASET_QUERY_MAX_RUNTIME_ROWS_ENV} — the whole "
            f"result set is held in workflow state, so a higher maximum costs source database "
            f"load, worker memory and checkpoint size."
        )
    return resolved


__all__ = [
    "DATASET_QUERY_MAX_RUNTIME_ROWS_ENV",
    "DEFAULT_DATASET_QUERY_ROW_LIMIT",
    "InvalidDatasetQueryLimitError",
    "resolve_dataset_query_row_limit",
]
