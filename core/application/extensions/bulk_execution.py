"""Bulk execution cursor and partial-failure semantics."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

EXECUTION_MODES: set[str] = {"sync", "async_job", "streamed_batch"}


PARTIAL_FAILURE_MODES: set[str] = {"fail_fast", "report_failed_items", "best_effort"}


def execute_bulk_operation(
    *,
    items: list[Any],
    handler: Callable[[Any], Any],
    execution_mode: str = "sync",
    batch_size: int = 100,
    cursor: int = 0,
    partial_failure_mode: str = "fail_fast",
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Run bulk operations with explicit mode/cursor/partial-failure semantics."""
    mode = str(execution_mode or "sync").strip().lower()
    if mode not in EXECUTION_MODES:
        raise ValueError(f"Unsupported execution_mode '{execution_mode}'")

    failure_mode = str(partial_failure_mode or "fail_fast").strip().lower()
    if failure_mode not in PARTIAL_FAILURE_MODES:
        raise ValueError(f"Unsupported partial_failure_mode '{partial_failure_mode}'")

    total = len(items)
    start = max(0, int(cursor or 0))
    size = max(1, int(batch_size or 1))
    processed = 0
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    chunk_reports: list[dict[str, Any]] = []

    position = start
    while position < total:
        chunk = items[position : position + size]
        chunk_success = 0
        chunk_failure = 0
        for offset, item in enumerate(chunk):
            index = position + offset
            try:
                value = handler(item)
                successes.append({"index": index, "result": value})
                chunk_success += 1
            except Exception as exc:
                failures.append({"index": index, "item": item, "error": str(exc)})
                chunk_failure += 1
                if failure_mode == "fail_fast":
                    processed = index + 1 - start
                    chunk_reports.append(
                        {
                            "chunk_start": position,
                            "chunk_end": index + 1,
                            "success_count": chunk_success,
                            "failure_count": chunk_failure,
                        }
                    )
                    return {
                        "status": "failed",
                        "execution_mode": mode,
                        "idempotency_key": str(idempotency_key or "").strip(),
                        "processed_count": processed,
                        "total_count": total - start,
                        "next_cursor": index + 1,
                        "successes": successes,
                        "failures": failures,
                        "chunk_reports": chunk_reports,
                    }

        processed += len(chunk)
        position += len(chunk)
        chunk_reports.append(
            {
                "chunk_start": position - len(chunk),
                "chunk_end": position,
                "success_count": chunk_success,
                "failure_count": chunk_failure,
            }
        )
        if mode == "async_job":
            # Async path preserves progress checkpoints and can be resumed.
            continue
        if mode == "streamed_batch":
            # In streamed mode we still return full report, but chunk metadata is primary.
            continue

    status = "completed_with_failures" if failures else "succeeded"
    return {
        "status": status,
        "execution_mode": mode,
        "idempotency_key": str(idempotency_key or "").strip(),
        "processed_count": processed,
        "total_count": total - start,
        "next_cursor": start + processed if (start + processed) < total else None,
        "successes": successes,
        "failures": failures,
        "chunk_reports": chunk_reports,
    }
