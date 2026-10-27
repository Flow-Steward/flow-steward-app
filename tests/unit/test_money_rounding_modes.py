"""A declared rounding mode must actually be applied, in exact decimal arithmetic.

Regression for the workflow E2E ledger finding F6. `number.round` and `money.round`
both accept a `mode` parameter, it is declared in the action contracts and it is
normalised into the rule payload — and both engines then ignored it. `tabular_feed`
always rounded `half_away_from_zero`, `transform` always used Python's `round()`
(which is half-to-even, so it did not even honour the declared `half_up`) and
`ROUND_HALF_UP`.

With no way to round money up, the supplier price rule "cost x 1.5, rounded up to
cents" had to be composed out of float operations — x1.5, x100, `number.ceil`,
÷100 — and that demonstrably produced wrong money in a live import:
`3.20 x 1.5` is `4.800000000000001` in binary floating point, so the ceiling
returned €4.81 instead of €4.80, and `45.10 x 1.5` returned €67.66 instead of
€67.65.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.transform_actions import (
    _action_money_multiply,
    _action_money_round,
    _action_number_round,
)
from core.infrastructure.rounding import (
    SUPPORTED_ROUNDING_MODES,
    UnsupportedRoundingModeError,
    quantize_decimal,
)

# ---------------------------------------------------------------------------
# The exact cases from the live import
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cost, expected",
    [
        ("3.20", "4.80"),
        ("45.10", "67.65"),
        ("9.99", "14.99"),  # 14.985 -> ceiling at 2dp
        ("0.01", "0.02"),  # 0.015 -> ceiling at 2dp
    ],
)
def test_supplier_price_rule_is_exact(cost, expected):
    """cost x 1.5 rounded up to cents, with no floating point drift."""
    multiplied = _action_money_multiply(cost, {"factor": "1.5"})
    assert _action_money_round(multiplied, {"decimals": 2, "mode": "ceil"}) == expected


def test_the_float_composition_that_produced_wrong_money_is_no_longer_needed():
    """The values that broke are exactly representable once the mode is honoured."""
    assert float("3.20") * 1.5 != 4.80, "the float path really is off by a bit"
    assert (
        _action_money_round(
            _action_money_multiply("3.20", {"factor": "1.5"}), {"decimals": 2, "mode": "ceil"}
        )
        == "4.80"
    )


# ---------------------------------------------------------------------------
# Every declared mode is applied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, mode, expected",
    [
        ("1.235", "half_up", "1.24"),
        ("1.235", "ceil", "1.24"),
        ("1.235", "floor", "1.23"),
        # 1.245 is where half_up and half_even part company: half_even keeps the
        # even last digit, half_up always goes away from zero.
        ("1.245", "half_up", "1.25"),
        ("1.245", "half_even", "1.24"),
    ],
)
def test_money_round_applies_the_declared_mode(value, mode, expected):
    assert _action_money_round(value, {"decimals": 2, "mode": mode}) == expected


def test_money_round_defaults_to_half_up():
    assert _action_money_round("1.235", {"decimals": 2}) == "1.24"


@pytest.mark.parametrize(
    "value, mode, expected",
    [
        (1.235, "half_up", 1.24),
        (1.235, "ceil", 1.24),
        (1.235, "floor", 1.23),
        (1.245, "half_up", 1.25),
        (1.245, "half_even", 1.24),
    ],
)
def test_number_round_applies_the_declared_mode(value, mode, expected):
    assert _action_number_round(value, {"decimals": 2, "mode": mode}) == expected


def test_number_round_honours_half_up_rather_than_pythons_banker_rounding():
    """Python's `round()` is half-to-even, so the declared `half_up` was not applied."""
    assert round(0.125, 2) == 0.12
    assert _action_number_round(0.125, {"decimals": 2}) == 0.13
    assert _action_number_round("2.675", {"decimals": 2}) == 2.68


# ---------------------------------------------------------------------------
# Boundaries and negatives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, mode, expected",
    [
        # Exactly on the quantum: nothing to round.
        ("4.80", "ceil", "4.80"),
        ("4.80", "floor", "4.80"),
        ("4.80", "half_up", "4.80"),
        ("-4.80", "ceil", "-4.80"),
        ("-4.80", "floor", "-4.80"),
        # Just above and just below.
        ("4.801", "ceil", "4.81"),
        ("4.809", "floor", "4.80"),
        # Negatives: ceiling is towards positive infinity, floor towards negative.
        ("-4.801", "ceil", "-4.80"),
        ("-4.801", "floor", "-4.81"),
        ("-1.235", "half_up", "-1.24"),
        ("-1.245", "half_even", "-1.24"),
        # Zero decimals.
        ("4.2", "ceil", "5"),
        ("-4.2", "ceil", "-4"),
        ("-4.2", "floor", "-5"),
    ],
)
def test_money_round_boundaries_and_negatives(value, mode, expected):
    decimals = 0 if "." not in expected else len(expected.split(".")[1])
    assert _action_money_round(value, {"decimals": decimals, "mode": mode}) == expected


def test_zero_is_stable_in_every_mode():
    for mode in SUPPORTED_ROUNDING_MODES:
        assert _action_money_round("0.00", {"decimals": 2, "mode": mode}) == "0.00"


# ---------------------------------------------------------------------------
# An unsupported mode is refused, not silently downgraded
# ---------------------------------------------------------------------------


def test_an_unsupported_mode_is_rejected():
    with pytest.raises(ValueError, match="rounding mode"):
        _action_money_round("1.235", {"decimals": 2, "mode": "towards_the_moon"})
    with pytest.raises(ValueError, match="rounding mode"):
        _action_number_round(1.235, {"decimals": 2, "mode": "towards_the_moon"})


def test_the_shared_helper_rejects_an_unsupported_mode():
    _raises_input_167_1 = Decimal("1.235")
    with pytest.raises(UnsupportedRoundingModeError):
        quantize_decimal(_raises_input_167_1, decimals=2, mode="nope")


def test_the_supported_modes_are_the_documented_set():
    assert SUPPORTED_ROUNDING_MODES == ("half_up", "half_even", "ceil", "floor")


# ---------------------------------------------------------------------------
# The tabular_feed engine must apply the same declared mode
# ---------------------------------------------------------------------------


def _tabular_rows(rules, rows):
    from core.infrastructure.tabular_feed.actions import _apply_rule

    materialized = [dict(row) for row in rows]
    for row in materialized:
        for rule in rules:
            _apply_rule(row, rule)
    return materialized


def _money_rule(mode: str, decimals: int = 2) -> dict:
    return {"target": "price", "money_round": {"decimals": decimals, "mode": mode}}


@pytest.mark.parametrize(
    "mode, expected",
    [("half_up", "1.24"), ("ceil", "1.24"), ("floor", "1.23")],
)
def test_tabular_money_round_applies_the_declared_mode(mode, expected):
    rows = _tabular_rows([_money_rule(mode)], [{"price": "1.235"}])
    assert rows[0]["price"] == expected


def test_tabular_money_round_gives_the_exact_supplier_price():
    rules = [
        {"target": "price", "money_multiply": "1.5"},
        _money_rule("ceil"),
    ]
    rows = _tabular_rows(rules, [{"price": "3.20"}, {"price": "45.10"}])
    assert [row["price"] for row in rows] == ["4.80", "67.65"]


@pytest.mark.parametrize(
    "mode, expected",
    [("half_up", 1.24), ("ceil", 1.24), ("floor", 1.23)],
)
def test_tabular_number_round_applies_the_declared_mode(mode, expected):
    rows = _tabular_rows(
        [{"target": "qty", "round": {"decimals": 2, "mode": mode}}], [{"qty": 1.235}]
    )
    assert rows[0]["qty"] == expected


def test_tabular_round_rejects_an_unsupported_mode():
    _raises_input_224_1 = [_money_rule("towards_the_moon")]
    with pytest.raises(ValueError, match="rounding mode"):
        _tabular_rows(_raises_input_224_1, [{"price": "1.235"}])


def _tabular_frame(rules, rows):
    """Exercise the vectorised Polars path, not just the row-wise fallback."""
    import polars as pl

    from core.infrastructure.tabular_feed.actions import _apply_rule_expressions

    frame, _report = _apply_rule_expressions(pl.DataFrame(rows), rules)
    return frame.to_dicts()


def test_polars_money_round_gives_the_exact_supplier_price():
    rules = [
        {"target": "price", "money_multiply": "1.5"},
        _money_rule("ceil"),
    ]
    rows = _tabular_frame(rules, [{"price": "3.20"}, {"price": "45.10"}])
    assert [row["price"] for row in rows] == ["4.80", "67.65"]


@pytest.mark.parametrize(
    "mode, expected",
    [("half_up", "1.24"), ("ceil", "1.24"), ("floor", "1.23")],
)
def test_polars_money_round_applies_the_declared_mode(mode, expected):
    rows = _tabular_frame([_money_rule(mode)], [{"price": "1.235"}])
    assert rows[0]["price"] == expected


def test_polars_and_rowwise_paths_agree():
    rules = [{"target": "price", "money_multiply": "1.5"}, _money_rule("ceil")]
    source = [{"price": "3.20"}, {"price": "45.10"}, {"price": "0.01"}]
    assert [row["price"] for row in _tabular_frame(rules, source)] == [
        row["price"] for row in _tabular_rows(rules, source)
    ]


# ---------------------------------------------------------------------------
# An unusable mode is caught when the step is authored
# ---------------------------------------------------------------------------


def _validate_actions(mode: str):
    from core.application.tabular_feed.validation import validate_tabular_feed_config

    return validate_tabular_feed_config(
        {
            "operation": "transform",
            "input": {"dataset_handle": "dst_fixture"},
            "result_target": "state.transformed",
            "actions": [
                {
                    "action": "money.round",
                    "target": "price",
                    "params": {"decimals": 2, "mode": mode},
                }
            ],
        },
        path_prefix="step.override.tabular_feed",
    )


@pytest.mark.parametrize("mode", SUPPORTED_ROUNDING_MODES)
def test_a_supported_mode_passes_authoring_validation(mode):
    codes = {str(issue.get("code") or "") for issue in _validate_actions(mode)}
    assert "workflow.step_tabular_feed_action_param_value_invalid" not in codes


def test_an_unsupported_mode_is_rejected_at_authoring_time():
    issues = _validate_actions("towards_the_moon")
    codes = {str(issue.get("code") or "") for issue in issues}
    assert "workflow.step_tabular_feed_action_param_value_invalid" in codes
    message = " ".join(str(issue.get("message") or "") for issue in issues)
    assert "half_up" in message
    assert "ceil" in message
