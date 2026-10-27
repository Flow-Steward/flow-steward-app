"""Order-import helpers for Extension Platform v2.

Provides idempotent order ingestion so that retrying an import for an
external_order_id that was already recorded returns the existing record
instead of creating a duplicate.
"""

from __future__ import annotations

import time
from typing import Any

_VALID_IMPORT_STATUSES: set[str] = {"pending", "processing", "completed", "failed"}


def import_order(
    *,
    order_store: dict[str, dict[str, Any]],
    external_order_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Import an order, guarding against duplicate ingestion on retry.

    If *external_order_id* is already present in *order_store* the existing
    record is returned unchanged.  This makes the operation safe to call
    multiple times (e.g. after a transient network failure) without creating
    duplicate orders.

    Args:
        order_store: Mutable mapping used as the backing store (keyed by
            normalised external_order_id).
        external_order_id: Caller-supplied identifier that uniquely identifies
            the order in the upstream system.
        payload: Order data to persist on first import.

    Returns:
        The stored order record (existing on duplicate, newly created otherwise).

    Raises:
        ValueError: If *external_order_id* is blank.
    """
    normalised_id = str(external_order_id or "").strip()
    if not normalised_id:
        raise ValueError("external_order_id is required")

    # Idempotency guard: return existing record without modification.
    existing = order_store.get(normalised_id)
    if isinstance(existing, dict):
        return dict(existing)

    raw_status = str(payload.get("status") or "pending").strip().lower()
    status = raw_status if raw_status in _VALID_IMPORT_STATUSES else "pending"

    record: dict[str, Any] = {
        "external_order_id": normalised_id,
        "customer_id": str(payload.get("customer_id") or "").strip(),
        "amount": float(payload.get("amount") or 0.0),
        "currency": str(payload.get("currency") or "usd").strip().lower(),
        "status": status,
        "imported_at": float(payload.get("imported_at") or time.time()),
        "metadata": dict(payload.get("metadata") or {}),
    }
    order_store[normalised_id] = record
    return dict(record)


def get_import_status(
    *,
    order_store: dict[str, dict[str, Any]],
    external_order_id: str,
) -> dict[str, Any] | None:
    """Return the stored record for *external_order_id*, or ``None``."""
    normalised_id = str(external_order_id or "").strip()
    if not normalised_id:
        return None
    record = order_store.get(normalised_id)
    return dict(record) if isinstance(record, dict) else None
