"""NDJSON column types come from the whole file, not from its first rows."""

import json

import pytest

from core.infrastructure.tabular_feed.readers import _dataframe_from_artifact_path


def _shopify_like_export(path, *, rows: int, first_filled: int) -> None:
    """A Shopify product export: template_suffix is null until *first_filled*, then ""."""
    with path.open("w", encoding="utf-8") as handle:
        for index in range(rows):
            suffix = None if index < first_filled else ""
            handle.write(json.dumps({"id": index, "template_suffix": suffix}) + "\n")


@pytest.mark.parametrize("lazy", [False, True])
def test_ndjson_reads_a_column_first_filled_after_the_first_hundred_rows(tmp_path, lazy):
    # Real export: 1,163 products with templateSuffix null, then one with "".
    path = tmp_path / "shopify-products.jsonl"
    _shopify_like_export(path, rows=130, first_filled=110)

    df, info = _dataframe_from_artifact_path(str(path), {}, {"format": "ndjson"}, lazy=lazy)
    frame = df.collect() if lazy else df

    assert info["format"] == "ndjson"
    assert frame.height == 130
    assert str(frame.schema["template_suffix"]) == "String"
    assert frame["template_suffix"][129] == ""
