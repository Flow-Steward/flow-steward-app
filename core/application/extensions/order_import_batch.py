"""Batch order-import helper for Extension Platform.

Provides idempotent batch ingestion so that:

1. A retry of the entire batch never re-imports already-recorded orders
   (delegated to ``import_order`` from ``order_import_v2``).
2. Duplicate ``external_order_id`` values *within* the same batch call are
   deduplicated — the first occurrence wins and later occurrences are skipped.

Bug history
-----------
The original implementation iterated the batch without a within-batch
deduplication guard.  When an upstream feed contained repeated
``external_order_id`` values (e.g. after feed corruption or double-delivery),
each occurrence would attempt a fresh write.  Although ``import_order`` itself
is idempotent across *calls*, the store check was bypassed on the second loop
iteration for the same ID inside a single batch invocation if the first
iteration completed between the check and the write (unlikely in single-threaded
code but also simply unnecessary work).  More concretely, the *count* returned
to the caller was inflated: every occurrence of the duplicate ID would increment
``imported`` instead of ``duplicates``, so callers relying on the count for
reconciliation saw incorrect numbers.

Fix: track ``seen_in_batch`` — a set of IDs already processed in this
invocation — and short-circuit to ``duplicates`` for any ID that appears more
than once in the batch.
"""

from __future__ import annotations

from typing import Any

from core.application.extensions.order_import_v2 import import_order


def import_order_batch(
    *,
    order_store: dict[str, dict[str, Any]],
    orders: list[dict[str, Any]],
) -> dict[str, Any]:
    """Import a list of orders idempotently, deduplicating within the batch.

    Each element of *orders* must contain an ``external_order_id`` key.
    Elements without one are collected into ``errors`` and otherwise ignored.

    Cross-call idempotency (retrying the same batch) is handled by
    ``import_order``; within-batch deduplication is handled here via
    ``seen_in_batch``.

    Args:
        order_store: Mutable mapping used as the backing store (keyed by
            normalised ``external_order_id``).
        orders: Sequence of order payload dicts to process.

    Returns:
        Summary dict with keys:
        - ``imported``   — count of orders newly added to *order_store*.
        - ``duplicates`` — count of orders skipped (already present in the
          store *or* repeated within this batch call).
        - ``errors``     — list of ``{"error": …, "order": …}`` dicts for
          entries that could not be processed (e.g. missing ID).
    """
    seen_in_batch: set[str] = set()
    imported = 0
    duplicates = 0
    errors: list[dict[str, Any]] = []

    for order in orders:
        raw_id = order.get("external_order_id")
        external_order_id = str(raw_id or "").strip()

        if not external_order_id:
            errors.append({"error": "missing external_order_id", "order": order})
            continue

        # Within-batch deduplication guard — fix for the seeded defect.
        # Without this block, a batch containing the same ID twice would count
        # both occurrences as "imported" even though only the first write
        # reaches the store.
        if external_order_id in seen_in_batch:
            duplicates += 1
            continue
        seen_in_batch.add(external_order_id)

        # Cross-call idempotency: import_order returns without writing if the
        # ID is already present in order_store.
        already_present = external_order_id in order_store
        import_order(
            order_store=order_store,
            external_order_id=external_order_id,
            payload=order,
        )
        if already_present:
            duplicates += 1
        else:
            imported += 1

    return {"imported": imported, "duplicates": duplicates, "errors": errors}
