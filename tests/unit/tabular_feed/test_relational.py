from __future__ import annotations

import polars as pl
import pytest

from core.infrastructure.tabular_feed.relational import _apply_relational


def test_explode_then_unnest_prefix_preserves_product_and_variant_fields() -> None:
    frame = pl.DataFrame(
        [
            {
                "id": 1,
                "title": "Product",
                "variants": [
                    {"id": 2, "title": "Blue", "sku": "A", "price": "10", "inventory_item_id": 3}
                ],
            }
        ]
    )

    result = _apply_relational(
        frame,
        [
            {"op": "explode", "column": "variants"},
            {"op": "unnest", "column": "variants", "prefix": "variant_"},
        ],
    )

    assert result.to_dicts() == [
        {
            "id": 1,
            "title": "Product",
            "variant_id": 2,
            "variant_title": "Blue",
            "variant_sku": "A",
            "variant_price": "10",
            "variant_inventory_item_id": 3,
        }
    ]
    assert frame.to_dicts()[0]["variants"][0]["id"] == 2


@pytest.mark.parametrize("prefix", [None, ""])
def test_unnest_without_prefix_preserves_nonconflicting_names(prefix: str | None) -> None:
    frame = pl.DataFrame([{"id": 1, "details": {"sku": "A", "price": 10}}])
    operation = {"op": "unnest", "column": "details"}
    if prefix is not None:
        operation["prefix"] = prefix

    assert _apply_relational(frame, [operation]).to_dicts() == [{"id": 1, "sku": "A", "price": 10}]


def test_unnest_without_prefix_reports_conflicting_names_and_prefix_remedy() -> None:
    frame = pl.DataFrame([{"id": 1, "title": "Product", "details": {"id": 2, "title": "Variant"}}])

    with pytest.raises(ValueError) as raised:
        _apply_relational(frame, [{"op": "unnest", "column": "details"}])

    assert "id" in str(raised.value)
    assert "title" in str(raised.value)
    assert "prefix" in str(raised.value).lower()


def test_unnest_prefix_reports_final_name_collision_without_overwriting() -> None:
    frame = pl.DataFrame([{"variant_id": 1, "details": {"id": 2}}])

    with pytest.raises(ValueError, match="variant_id"):
        _apply_relational(frame, [{"op": "unnest", "column": "details", "prefix": "variant_"}])

    assert frame.to_dicts() == [{"variant_id": 1, "details": {"id": 2}}]


def test_unnest_prefix_does_not_rename_fields_from_previous_operations() -> None:
    frame = pl.DataFrame([{"product": {"id": 1}, "variant": {"sku": "A"}}])

    result = _apply_relational(
        frame,
        [
            {"op": "unnest", "column": "product"},
            {"op": "unnest", "column": "variant", "prefix": "variant_"},
        ],
    )

    assert result.to_dicts() == [{"id": 1, "variant_sku": "A"}]


def test_unnest_prefix_supports_lazy_frames() -> None:
    frame = pl.DataFrame([{"id": 1, "details": {"id": 2}}]).lazy()

    result = _apply_relational(frame, [{"op": "unnest", "column": "details", "prefix": "detail_"}])

    assert result.collect().to_dicts() == [{"id": 1, "detail_id": 2}]
