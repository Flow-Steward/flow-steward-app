"""Regression tests for batch order-import idempotency.

These tests document and verify the fix for the within-batch duplicate
``external_order_id`` defect in ``import_order_batch``.

Defect summary
--------------
Without the ``seen_in_batch`` guard, submitting a batch with two entries
sharing the same ``external_order_id`` would:
  - call ``import_order`` twice for the same ID,
  - count *both* as "imported" in the summary even though only the first
    write reached the store, and
  - mislead callers doing reconciliation on the returned counts.

Every test that directly exercises duplicate-prevention is annotated with
``# Regression`` so future readers can identify the coverage scope.
"""

from __future__ import annotations

from core.application.extensions.order_import_batch import import_order_batch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store() -> dict:
    return {}


def _order(external_order_id: str, **kwargs) -> dict:
    defaults: dict = {
        "external_order_id": external_order_id,
        "customer_id": "cust_1",
        "amount": 10.0,
        "currency": "usd",
        "status": "pending",
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# Happy path: fresh batch with unique IDs
# ---------------------------------------------------------------------------


def test_empty_batch_returns_zero_counts() -> None:
    result = import_order_batch(order_store=_store(), orders=[])
    assert result == {"imported": 0, "duplicates": 0, "errors": []}


def test_single_order_is_imported() -> None:
    store = _store()
    result = import_order_batch(order_store=store, orders=[_order("ord_001")])
    assert result["imported"] == 1
    assert result["duplicates"] == 0
    assert result["errors"] == []
    assert "ord_001" in store


def test_unique_batch_imports_all() -> None:
    store = _store()
    batch = [_order("ord_a", amount=1.0), _order("ord_b", amount=2.0), _order("ord_c", amount=3.0)]
    result = import_order_batch(order_store=store, orders=batch)
    assert result["imported"] == 3
    assert result["duplicates"] == 0
    assert len(store) == 3


# ---------------------------------------------------------------------------
# Regression: within-batch duplicate IDs must be deduplicated
# ---------------------------------------------------------------------------


def test_duplicate_within_batch_counted_as_duplicate() -> None:
    """Regression: second occurrence of same ID in one batch must not count as imported."""
    store = _store()
    batch = [
        _order("ord_dup", amount=10.0),
        _order("ord_dup", amount=99.0),  # duplicate within batch
    ]
    result = import_order_batch(order_store=store, orders=batch)
    assert result["imported"] == 1  # Regression: was 2 without the fix
    assert result["duplicates"] == 1
    assert len(store) == 1


def test_first_occurrence_wins_in_batch() -> None:
    """Regression: first occurrence of a duplicate ID determines the stored record."""
    store = _store()
    batch = [
        _order("ord_first", amount=10.0),
        _order("ord_first", amount=999.0),  # must be ignored
    ]
    import_order_batch(order_store=store, orders=batch)
    assert store["ord_first"]["amount"] == 10.0  # Regression: first wins


def test_many_duplicates_within_batch_collapse_to_one() -> None:
    """Regression: N occurrences of the same ID produce exactly one store entry."""
    store = _store()
    batch = [_order("ord_many")] * 5  # same dict repeated 5 times
    result = import_order_batch(order_store=store, orders=batch)
    assert result["imported"] == 1  # Regression: was 5 without the fix
    assert result["duplicates"] == 4
    assert len(store) == 1


def test_mixed_unique_and_duplicate_ids() -> None:
    """Regression: unique IDs are all imported; duplicate occurrences are counted correctly."""
    store = _store()
    batch = [
        _order("ord_x", amount=1.0),
        _order("ord_y", amount=2.0),
        _order("ord_x", amount=100.0),  # duplicate of ord_x
        _order("ord_z", amount=3.0),
        _order("ord_y", amount=200.0),  # duplicate of ord_y
    ]
    result = import_order_batch(order_store=store, orders=batch)
    assert result["imported"] == 3  # ord_x, ord_y, ord_z
    assert result["duplicates"] == 2  # second ord_x, second ord_y
    assert len(store) == 3


# ---------------------------------------------------------------------------
# Regression: cross-call idempotency (retrying the same batch)
# ---------------------------------------------------------------------------


def test_retrying_full_batch_counts_all_as_duplicates() -> None:
    """Regression: retrying a batch must not re-import already-recorded orders."""
    store = _store()
    batch = [_order("ord_r1"), _order("ord_r2")]
    import_order_batch(order_store=store, orders=batch)
    result = import_order_batch(order_store=store, orders=batch)
    assert result["imported"] == 0  # Regression: all already present
    assert result["duplicates"] == 2
    assert len(store) == 2  # store unchanged after retry


def test_retrying_batch_with_whitespace_padded_id_counts_as_duplicate() -> None:
    """Regression: retry IDs are normalized before checking existing imports."""
    store = _store()
    import_order_batch(order_store=store, orders=[_order("ord_retry_normalized", amount=42.0)])

    result = import_order_batch(
        order_store=store,
        orders=[_order("  ord_retry_normalized  ", amount=1.0)],
    )

    assert result["imported"] == 0
    assert result["duplicates"] == 1
    assert len(store) == 1
    assert store["ord_retry_normalized"]["amount"] == 42.0


def test_partial_retry_imports_only_new_orders() -> None:
    """Regression: a partial retry must import new orders and skip already-known ones."""
    store = _store()
    import_order_batch(order_store=store, orders=[_order("ord_existing")])
    result = import_order_batch(
        order_store=store,
        orders=[_order("ord_existing"), _order("ord_new")],
    )
    assert result["imported"] == 1  # only ord_new
    assert result["duplicates"] == 1  # ord_existing already present
    assert len(store) == 2


def test_stored_record_not_mutated_on_cross_call_retry() -> None:
    """Regression: retry must not overwrite fields of an already-imported order."""
    store = _store()
    import_order_batch(order_store=store, orders=[_order("ord_immutable", amount=42.0)])
    import_order_batch(order_store=store, orders=[_order("ord_immutable", amount=1.0)])
    assert store["ord_immutable"]["amount"] == 42.0


# ---------------------------------------------------------------------------
# Validation: entries with missing external_order_id go to errors
# ---------------------------------------------------------------------------


def test_order_without_id_goes_to_errors() -> None:
    store = _store()
    bad_order = {"customer_id": "cust_x", "amount": 5.0}
    result = import_order_batch(order_store=store, orders=[bad_order])
    assert result["imported"] == 0
    assert result["errors"] == [{"error": "missing external_order_id", "order": bad_order}]
    assert len(store) == 0


def test_order_with_blank_id_goes_to_errors() -> None:
    store = _store()
    result = import_order_batch(
        order_store=store,
        orders=[{"external_order_id": "   ", "amount": 5.0}],
    )
    assert len(result["errors"]) == 1
    assert len(store) == 0


def test_mixed_valid_and_invalid_orders() -> None:
    store = _store()
    batch = [
        _order("ord_valid"),
        {"customer_id": "no_id"},  # missing external_order_id
        _order("ord_also_valid"),
    ]
    result = import_order_batch(order_store=store, orders=batch)
    assert result["imported"] == 2
    assert len(result["errors"]) == 1
    assert len(store) == 2


# ---------------------------------------------------------------------------
# Return-value isolation: summary dict is independent of the store
# ---------------------------------------------------------------------------


def test_summary_dict_is_standalone() -> None:
    """Returned summary must not hold references back into the store."""
    store = _store()
    result = import_order_batch(order_store=store, orders=[_order("ord_iso")])
    # The result only contains counts and error lists — no store references.
    assert set(result.keys()) == {"imported", "duplicates", "errors"}
