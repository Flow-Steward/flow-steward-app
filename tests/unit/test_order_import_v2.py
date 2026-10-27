"""Regression tests for order-import idempotency (external_order_id duplicate guard).

These tests document the fix for the bug where retrying an import could
insert the same external_order_id twice.  Every test that verifies
duplicate-prevention behaviour is marked with a comment so future readers
understand the history.
"""

from __future__ import annotations

import pytest

from core.application.extensions.order_import_v2 import get_import_status, import_order

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store() -> dict:
    return {}


def _payload(**kwargs):
    defaults = {
        "customer_id": "cust_1",
        "amount": 99.00,
        "currency": "usd",
        "status": "pending",
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# Happy-path: first import creates a record
# ---------------------------------------------------------------------------


def test_first_import_creates_record() -> None:
    store = _store()
    result = import_order(
        order_store=store,
        external_order_id="ord_001",
        payload=_payload(),
    )
    assert result["external_order_id"] == "ord_001"
    assert result["customer_id"] == "cust_1"
    assert result["amount"] == 99.0
    assert result["currency"] == "usd"
    assert result["status"] == "pending"
    assert len(store) == 1


# ---------------------------------------------------------------------------
# Regression: duplicate guard — retry MUST NOT create a second record
# ---------------------------------------------------------------------------


def test_duplicate_import_returns_existing_record() -> None:
    """Regression: retrying import with same external_order_id must be idempotent."""
    store = _store()
    first = import_order(
        order_store=store,
        external_order_id="ord_002",
        payload=_payload(amount=50.0),
    )
    # Simulate a retry with different payload fields — the original must win.
    second = import_order(
        order_store=store,
        external_order_id="ord_002",
        payload=_payload(amount=999.0, status="completed"),
    )
    # Both calls must return the same record.
    assert first == second
    # Only one entry must exist in the store.
    assert len(store) == 1


def test_duplicate_import_does_not_overwrite_amount() -> None:
    """Regression: second import attempt must not mutate the stored amount."""
    store = _store()
    import_order(
        order_store=store,
        external_order_id="ord_003",
        payload=_payload(amount=75.0),
    )
    import_order(
        order_store=store,
        external_order_id="ord_003",
        payload=_payload(amount=1.0),
    )
    assert store["ord_003"]["amount"] == 75.0


def test_duplicate_import_does_not_overwrite_status() -> None:
    """Regression: second import attempt must not mutate the stored status."""
    store = _store()
    import_order(
        order_store=store,
        external_order_id="ord_004",
        payload=_payload(status="processing"),
    )
    import_order(
        order_store=store,
        external_order_id="ord_004",
        payload=_payload(status="completed"),
    )
    assert store["ord_004"]["status"] == "processing"


def test_many_retries_leave_exactly_one_record() -> None:
    """Regression: N retries with the same external_order_id must not fan-out."""
    store = _store()
    for _ in range(10):
        import_order(
            order_store=store,
            external_order_id="ord_005",
            payload=_payload(),
        )
    assert len(store) == 1


# ---------------------------------------------------------------------------
# Isolation: different external_order_ids are independent
# ---------------------------------------------------------------------------


def test_different_external_order_ids_are_independent() -> None:
    store = _store()
    import_order(order_store=store, external_order_id="ord_a", payload=_payload(amount=10.0))
    import_order(order_store=store, external_order_id="ord_b", payload=_payload(amount=20.0))
    assert len(store) == 2
    assert store["ord_a"]["amount"] == 10.0
    assert store["ord_b"]["amount"] == 20.0


# ---------------------------------------------------------------------------
# Validation: blank external_order_id must be rejected
# ---------------------------------------------------------------------------


def test_blank_external_order_id_raises() -> None:
    _raises_input_144_1 = _store()
    _raises_input_144_2 = _payload()
    with pytest.raises(ValueError, match="external_order_id is required"):
        import_order(
            order_store=_raises_input_144_1, external_order_id="", payload=_raises_input_144_2
        )


def test_whitespace_external_order_id_raises() -> None:
    _raises_input_149_1 = _store()
    _raises_input_149_2 = _payload()
    with pytest.raises(ValueError, match="external_order_id is required"):
        import_order(
            order_store=_raises_input_149_1, external_order_id="   ", payload=_raises_input_149_2
        )


# ---------------------------------------------------------------------------
# Status normalisation
# ---------------------------------------------------------------------------


def test_unknown_status_is_normalised_to_pending() -> None:
    store = _store()
    result = import_order(
        order_store=store,
        external_order_id="ord_006",
        payload=_payload(status="unknown_state"),
    )
    assert result["status"] == "pending"


@pytest.mark.parametrize("status", ["pending", "processing", "completed", "failed"])
def test_valid_statuses_are_preserved(status: str) -> None:
    store = _store()
    result = import_order(
        order_store=store,
        external_order_id=f"ord_s_{status}",
        payload=_payload(status=status),
    )
    assert result["status"] == status


# ---------------------------------------------------------------------------
# get_import_status helper
# ---------------------------------------------------------------------------


def test_get_import_status_returns_none_for_missing() -> None:
    assert get_import_status(order_store=_store(), external_order_id="missing") is None


def test_get_import_status_returns_none_for_blank_id() -> None:
    assert get_import_status(order_store=_store(), external_order_id="") is None


def test_get_import_status_returns_record_after_import() -> None:
    store = _store()
    import_order(order_store=store, external_order_id="ord_007", payload=_payload())
    record = get_import_status(order_store=store, external_order_id="ord_007")
    assert record is not None
    assert record["external_order_id"] == "ord_007"


def test_get_import_status_returns_copy_not_reference() -> None:
    """Callers must not be able to mutate stored state via the returned dict."""
    store = _store()
    import_order(order_store=store, external_order_id="ord_008", payload=_payload(amount=5.0))
    record = get_import_status(order_store=store, external_order_id="ord_008")
    assert record is not None
    record["amount"] = 999.0  # mutate the copy
    # Stored value must be unchanged.
    assert store["ord_008"]["amount"] == 5.0


# ---------------------------------------------------------------------------
# Regression: import_order also returns a copy, not the stored reference
# ---------------------------------------------------------------------------


def test_import_order_returns_copy_not_reference() -> None:
    """Regression: mutating the dict returned by import_order must not affect stored state."""
    store = _store()
    result = import_order(
        order_store=store,
        external_order_id="ord_009",
        payload=_payload(amount=42.0),
    )
    result["amount"] = 0.0  # mutate the returned copy
    # Stored record must be unchanged.
    assert store["ord_009"]["amount"] == 42.0


def test_duplicate_import_returns_copy_not_reference() -> None:
    """Regression: the copy returned on a duplicate call must also be isolated."""
    store = _store()
    import_order(order_store=store, external_order_id="ord_010", payload=_payload(amount=10.0))
    second = import_order(
        order_store=store,
        external_order_id="ord_010",
        payload=_payload(amount=999.0),
    )
    second["amount"] = 0.0  # mutate the returned copy
    # Stored record must be unchanged after the duplicate-guard path.
    assert store["ord_010"]["amount"] == 10.0


# ---------------------------------------------------------------------------
# Regression: whitespace-normalised external_order_id hits the idempotency guard
# ---------------------------------------------------------------------------


def test_whitespace_padded_id_is_treated_as_duplicate() -> None:
    """Regression: '  ord_001  ' and 'ord_001' must resolve to the same record."""
    store = _store()
    first = import_order(
        order_store=store,
        external_order_id="ord_011",
        payload=_payload(amount=55.0),
    )
    # Re-import with leading/trailing whitespace — must be treated as the same order.
    second = import_order(
        order_store=store,
        external_order_id="  ord_011  ",
        payload=_payload(amount=1.0),
    )
    assert first == second
    assert len(store) == 1
    assert store["ord_011"]["amount"] == 55.0
