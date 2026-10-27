"""Tests for the shared fail-safe byte-size-limit env resolver."""

from __future__ import annotations

import pytest

from core.infrastructure.config.size_limits import clamp_size_limit_bytes

_DEFAULT = 1_000_000
_MIN = 1_000
_MAX = 10_000_000


def _resolve(raw):
    return clamp_size_limit_bytes(raw, default=_DEFAULT, minimum=_MIN, maximum=_MAX)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_returns_default(raw):
    assert _resolve(raw) == _DEFAULT


def test_valid_in_range_override_is_used():
    assert _resolve("2500000") == 2_500_000
    assert _resolve("  2500000  ") == 2_500_000


@pytest.mark.parametrize("raw", ["not-a-number", "1.5", "12kb", "0x10", "5mb"])
def test_invalid_value_falls_back_to_default(raw):
    assert _resolve(raw) == _DEFAULT


def test_below_minimum_clamps_to_minimum():
    assert _resolve("500") == _MIN
    assert _resolve("0") == _MIN


def test_negative_clamps_to_minimum():
    assert _resolve("-100") == _MIN


def test_above_maximum_clamps_to_maximum():
    assert _resolve("99999999") == _MAX


def test_exact_boundaries_are_allowed():
    assert _resolve(str(_MIN)) == _MIN
    assert _resolve(str(_MAX)) == _MAX


def test_default_outside_range_is_itself_clamped():
    # A misconfigured default must still be returned within the documented range.
    assert clamp_size_limit_bytes(None, default=50, minimum=_MIN, maximum=_MAX) == _MIN
    assert clamp_size_limit_bytes(None, default=99_999_999, minimum=_MIN, maximum=_MAX) == _MAX


def test_inverted_range_is_normalized():
    # minimum/maximum passed in the wrong order must not break clamping.
    assert clamp_size_limit_bytes("5000", default=_DEFAULT, minimum=_MAX, maximum=_MIN) == 5000
