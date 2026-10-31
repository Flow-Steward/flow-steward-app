"""The std.text pad action refuses a width that would exhaust a worker."""

from __future__ import annotations

import pytest

from core.application.orchestration.tools.standard_text_actions import MAX_PAD_WIDTH, pad


def test_pad_within_limit_pads() -> None:
    assert pad.invoke({"value": "7", "width": 3, "fill_char": "0"}) == {"result": "007"}


def test_pad_refuses_huge_width() -> None:
    with pytest.raises(ValueError, match="width must not exceed"):
        pad.invoke({"value": "x", "width": 1_073_741_824})
    assert len(pad.invoke({"value": "", "width": MAX_PAD_WIDTH})["result"]) == MAX_PAD_WIDTH
