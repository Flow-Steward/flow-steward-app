"""Exported CSV/XLSX cells never start a spreadsheet formula (CSV injection)."""

from __future__ import annotations

import polars as pl

from core.infrastructure.tabular_feed.common import _neutralize_spreadsheet_formulas


def test_formula_looking_text_gets_an_apostrophe() -> None:
    frame = pl.DataFrame(
        {
            "title": ["=1+1", "+cmd", "-2", "@SUM(A1)", "\tx", "plain", None],
            "count": [1, 2, 3, 4, 5, 6, 7],
        }
    )

    for result in (
        _neutralize_spreadsheet_formulas(frame),
        _neutralize_spreadsheet_formulas(frame.lazy()).collect(),
    ):
        assert result["title"].to_list() == [
            "'=1+1",
            "'+cmd",
            "'-2",
            "'@SUM(A1)",
            "'\tx",
            "plain",
            None,
        ]
        assert result["count"].to_list() == [1, 2, 3, 4, 5, 6, 7]
