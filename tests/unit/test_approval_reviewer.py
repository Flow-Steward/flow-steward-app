from __future__ import annotations

from tests.e2e.approval_reviewer import _min_content_len_for_stage


def test_stage_override_is_case_insensitive() -> None:
    assert _min_content_len_for_stage("Analysis", {"analysis": 60}) == 60


def test_stage_override_exact_key_still_supported() -> None:
    assert _min_content_len_for_stage("Analysis", {"Analysis": 70}) == 70
