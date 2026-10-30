"""Strict scalar primitives retain transform coercion and arithmetic behavior."""

from decimal import Decimal, InvalidOperation

import pytest

from core.application.orchestration import transform_actions, transform_values
from core.infrastructure import standard_boolean, standard_money, standard_numbers
from core.infrastructure.rounding import UnsupportedRoundingModeError


@pytest.mark.parametrize("value, expected", [(" 2 ", 2), ("1e2", 100), ("1.25", 1.25), (-0.0, 0)])
def test_number_coercion_preserves_value_and_type(value, expected):
    result = standard_numbers.coerce(value)
    assert result == expected == transform_values._coerce_number(value)
    assert type(result) is type(expected)


@pytest.mark.parametrize(
    "value, message",
    [
        (True, "boolean is not a number"),
        (None, "NoneType is not a number"),
        (Decimal("2"), "Decimal is not a number"),
        (" ", "empty string is not a number"),
        (float("inf"), "number must be finite"),
        ("NaN", "number must be finite"),
    ],
)
def test_number_coercion_rejections(value, message):
    for coerce in (standard_numbers.coerce, transform_values._coerce_number):
        with pytest.raises(ValueError, match=message):
            coerce(value)


@pytest.mark.parametrize(
    "operation, parameter, value, operand, expected",
    [
        ("add", "operand", 2, 3.0, 5.0),
        ("subtract", "operand", 2, 3.0, -1.0),
        ("multiply", "factor", 3.2, 1.5, 4.800000000000001),
        ("divide", "divisor", 3, 2.0, 1.5),
    ],
)
def test_normalized_number_arithmetic(operation, parameter, value, operand, expected):
    result = getattr(standard_numbers, operation)(value, operand)
    assert result == expected
    assert result == getattr(transform_actions, f"_action_number_{operation}")(
        value, {parameter: operand}
    )
    assert isinstance(result, float)


def test_divide_rejects_both_signed_zeros():
    for divisor in (0.0, -0.0):
        with pytest.raises(ValueError, match="divisor must not be zero"):
            standard_numbers.divide(1, divisor)


@pytest.mark.parametrize("operation, expected", [("floor", -2), ("ceil", -1)])
def test_number_directional_integer_results(operation, expected):
    result = getattr(standard_numbers, operation)(-1.2)
    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize(
    "value, mode, places, expected",
    [
        (1.245, "half_up", 2, 1.25),
        (1.245, "half_even", 2, 1.24),
        (-1.235, "ceil", 2, -1.23),
        (-1.235, "floor", 2, -1.24),
        (1.5, None, 0, 2.0),
        (15.5, None, -1, 16.0),
    ],
)
def test_number_round_reuses_declared_decimal_modes(value, mode, places, expected):
    assert standard_numbers.round_number(value, places, mode) == expected
    assert standard_numbers.round_number(
        value, places, mode
    ) == transform_actions._action_number_round(value, {"decimals": places, "mode": mode})


@pytest.mark.parametrize(
    "value, expected",
    [(" 1.2300 ", "1.2300"), ("1e3", "1000"), (Decimal("-0.00"), "-0.00"), (2, "2")],
)
def test_money_coercion_retains_scale_and_plain_format(value, expected):
    assert standard_money.to_decimal(value) == transform_values._money_decimal(value)
    assert standard_money.to_text(value) == expected == transform_values._money_text(value)


@pytest.mark.parametrize(
    "value, message",
    [
        (True, "value is not a money amount"),
        (None, "value is not a money amount"),
        ("", "value is not a money amount"),
        ("Infinity", "money amount must be finite"),
        (Decimal("NaN"), "money amount must be finite"),
    ],
)
def test_money_rejections(value, message):
    for coerce in (standard_money.to_decimal, transform_values._money_decimal):
        with pytest.raises(ValueError, match=message):
            coerce(value)


def test_invalid_money_preserves_exception_cause():
    with pytest.raises(ValueError) as error:
        standard_money.to_decimal("invalid")
    assert isinstance(error.value.__cause__, InvalidOperation)


@pytest.mark.parametrize(
    "operation, parameter, expected",
    [("add", "operand", "4.70"), ("subtract", "operand", "1.70"), ("multiply", "factor", "4.800")],
)
def test_normalized_money_arithmetic_retains_decimal_scale(operation, parameter, expected):
    assert getattr(standard_money, operation)(Decimal("3.20"), Decimal("1.5")) == expected
    assert (
        getattr(transform_actions, f"_action_money_{operation}")("3.20", {parameter: "1.5"})
        == expected
    )


@pytest.mark.parametrize(
    "mode, expected", [(None, "1.25"), ("half_even", "1.24"), ("ceil", "1.25"), ("floor", "1.24")]
)
def test_money_round_reuses_existing_rounding(mode, expected):
    assert standard_money.round_money(Decimal("1.245"), 2, mode) == expected
    assert transform_actions._action_money_round("1.245", {"mode": mode}) == expected


def test_rounding_rejects_unsupported_mode():
    with pytest.raises(UnsupportedRoundingModeError):
        standard_numbers.round_number(2, 0, "unknown")
    amount = Decimal("2")
    with pytest.raises(UnsupportedRoundingModeError):
        standard_money.round_money(amount, 2, "unknown")


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (False, False),
        (" YES ", True),
        ("on", True),
        ("1", True),
        ("true", True),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
        (0, False),
        (-0.0, False),
        (-2, True),
        (float("nan"), True),
        (float("inf"), True),
    ],
)
def test_boolean_coercion_preserves_explicit_tokens_and_numeric_truth(value, expected):
    assert standard_boolean.coerce(value) is expected
    assert transform_values._coerce_boolean(value) is expected


@pytest.mark.parametrize("value", [None, [], {}, Decimal("1"), "maybe", "2"])
def test_boolean_rejects_other_truthy_objects(value):
    message = f"{type(value).__name__} is not a boolean"
    for coerce in (standard_boolean.coerce, transform_values._coerce_boolean):
        with pytest.raises(ValueError, match=message):
            coerce(value)


@pytest.mark.parametrize(
    "action, value, params, message",
    [
        ("number_add", None, {"operand": True}, "NoneType is not a number"),
        ("number_subtract", None, {"operand": True}, "NoneType is not a number"),
        ("number_multiply", None, {"factor": True}, "NoneType is not a number"),
        ("number_divide", None, {"divisor": 0}, "divisor must not be zero"),
        ("number_divide", None, {"divisor": True}, "boolean is not a number"),
        ("number_round", None, {"decimals": True}, "boolean is not a number"),
        ("money_round", None, {"decimals": -1}, "decimals must be non-negative"),
        ("money_round", None, {"decimals": True}, "boolean is not a number"),
    ],
)
def test_adapter_validation_order_is_preserved(action, value, params, message):
    adapter = getattr(transform_actions, f"_action_{action}")
    with pytest.raises(ValueError, match=message):
        adapter(value, params)


@pytest.mark.parametrize(
    "action, value, params, expected",
    [
        ("number_round", "1.5", {}, 2.0),
        ("money_round", "1.235", {}, "1.24"),
        ("number_round", "1.25", {"decimals": 1.9}, 1.3),
        ("money_round", "1.25", {"decimals": 1.9}, "1.3"),
        ("number_round", "15.5", {"decimals": -1}, 16.0),
        ("number_add", "2", {"operand": "3"}, 5.0),
        ("money_add", "2.00", {"operand": "3"}, "5.00"),
    ],
)
def test_adapter_defaults_and_parameter_coercions(action, value, params, expected):
    result = getattr(transform_actions, f"_action_{action}")(value, params)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize("category", ["number", "money"])
def test_explicit_null_rounding_precision_does_not_use_omission_default(category):
    adapter = getattr(transform_actions, f"_action_{category}_round")
    with pytest.raises(ValueError, match="NoneType is not a number"):
        adapter("1.25", {"decimals": None})


@pytest.mark.parametrize(
    "value_type, helper_module, helper_name, value, expected",
    [
        ("number", standard_numbers, "coerce", " 2.5 ", 2.5),
        ("money", standard_money, "to_text", " 2.50 ", "2.50"),
        ("boolean", standard_boolean, "coerce", "on", True),
    ],
)
def test_transform_value_coercion_uses_shared_owner(
    monkeypatch, value_type, helper_module, helper_name, value, expected
):
    original = getattr(helper_module, helper_name)
    calls = []

    def record(argument):
        calls.append(argument)
        return original(argument)

    monkeypatch.setattr(helper_module, helper_name, record)
    assert transform_values._coerce_value(value, value_type) == expected
    assert calls == [value]
