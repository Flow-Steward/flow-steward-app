"""Date configuration failures must be found before a workflow is launched."""

from __future__ import annotations

import pytest

from core.application.orchestration.transform_actions import _ACTIONS
from core.application.orchestration.transform_config_validation import _validate_action_params


def test_unknown_date_format_token_is_rejected_during_configuration():
    errors = _validate_action_params(
        _ACTIONS["date.format"], {"output_format": "YYYY-QQ-DD"}, path="actions[0].params"
    )
    assert any(error["code"] == "unknown_format_token" for error in errors)
    assert any(error["path"] == "actions[0].params.output_format" for error in errors)


@pytest.mark.parametrize("amount", [True, False, 1.5, "2", None])
def test_date_amount_requires_a_literal_integer(amount):
    errors = _validate_action_params(
        _ACTIONS["date.add"], {"amount": amount, "unit": "days"}, path="actions[0].params"
    )
    assert any(error["path"] == "actions[0].params.amount" for error in errors)


@pytest.mark.parametrize("amount", [-1, 0, 2])
def test_date_signed_integer_amount_is_valid(amount):
    assert not _validate_action_params(
        _ACTIONS["date.add"], {"amount": amount, "unit": "days"}, path="actions[0].params"
    )
