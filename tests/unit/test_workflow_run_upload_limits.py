"""Browser runs take real photos under the attachment policy (ADR 0075)."""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from core.application.management.command_models_api_inputs import (
    BuilderWorkflowRunInput,
    WorkflowRunInput,
)
from core.infrastructure.config.workflow_file_limits import browser_run_upload_limits

PROJECT_ID = "00000000-0000-0000-0000-000000000007"
_MIB = 1024 * 1024


def _file(name: str, size: int, *, filename: str = "receipt.jpg") -> dict:
    return {
        "input_name": name,
        "filename": filename,
        "mime_type": "image/jpeg",
        "content_base64": base64.b64encode(b"\xff" * size).decode(),
    }


def _browser(files: list[dict]) -> BuilderWorkflowRunInput:
    return BuilderWorkflowRunInput.model_validate(
        {
            "project_id": PROJECT_ID,
            "expected_revision": 3,
            "acknowledged": True,
            "summary": "Receipts",
            "files": files,
        }
    )


@pytest.fixture
def policy(monkeypatch):
    def set_policy(*, max_bytes: int, max_files: int = 10) -> None:
        monkeypatch.setenv("FS_ATTACHMENT_MAX_SIZE_BYTES", str(max_bytes))
        monkeypatch.setenv("FS_ATTACHMENT_MAX_FILES", str(max_files))

    return set_policy


def test_limits_come_from_the_attachment_policy(policy) -> None:
    policy(max_bytes=5 * _MIB, max_files=4)

    assert browser_run_upload_limits() == {
        "max_file_bytes": 5 * _MIB,
        "max_total_bytes": 20 * _MIB,
        "max_files": 4,
    }


def test_a_photo_larger_than_one_mib_under_the_policy_is_accepted(policy) -> None:
    policy(max_bytes=5 * _MIB)

    request = _browser([_file("receipt", 3 * _MIB)])

    assert len(request.files[0].decoded_content()) == 3 * _MIB


def test_a_file_over_the_policy_is_refused(policy) -> None:
    policy(max_bytes=2 * _MIB)

    with pytest.raises(ValidationError, match="larger than"):
        _browser([_file("receipt", 2 * _MIB + 1)])


def test_several_pages_each_under_the_policy_are_accepted(policy) -> None:
    """Four pages of one contract: each file counts, not their sum (ADR 0075)."""
    policy(max_bytes=2 * _MIB, max_files=4)

    request = _browser([_file(f"page{index}", _MIB + 10) for index in range(4)])

    assert len(request.files) == 4


def test_the_total_follows_from_size_and_count(policy) -> None:
    policy(max_bytes=2 * _MIB, max_files=2)
    total = browser_run_upload_limits()["max_total_bytes"]

    assert total == 4 * _MIB


def test_more_files_than_the_policy_allows_are_refused(policy) -> None:
    policy(max_bytes=5 * _MIB, max_files=2)

    with pytest.raises(ValidationError, match="at most 2 files"):
        _browser([_file("a", 10), _file("b", 10), _file("c", 10)])


def test_zero_keeps_its_meaning_of_no_limit(policy) -> None:
    policy(max_bytes=0, max_files=0)

    request = _browser([_file(f"f{index}", 2 * _MIB) for index in range(12)])

    assert len(request.files) == 12


def test_an_empty_file_is_refused(policy) -> None:
    policy(max_bytes=5 * _MIB)

    with pytest.raises(ValidationError):
        _browser([_file("receipt", 0)])


def test_several_files_may_share_one_input_name_in_order(policy) -> None:
    """A list-of-files input takes one entry per page; the contract decides (ADR 0075)."""
    policy(max_bytes=5 * _MIB, max_files=4)

    request = _browser([_file("pages", 10, filename=f"p{index}.jpg") for index in range(4)])

    assert [row.filename for row in request.files] == ["p0.jpg", "p1.jpg", "p2.jpg", "p3.jpg"]


def test_the_count_covers_several_files_of_one_input(policy) -> None:
    policy(max_bytes=5 * _MIB, max_files=3)

    with pytest.raises(ValidationError, match="at most 3 files"):
        _browser([_file("pages", 10) for _ in range(4)])


def test_one_oversized_page_refuses_the_run(policy) -> None:
    policy(max_bytes=2 * _MIB)

    with pytest.raises(ValidationError, match="larger than"):
        _browser([_file("pages", 10), _file("pages", 2 * _MIB + 1, filename="big.jpg")])


def test_the_api_launch_keeps_its_own_fixed_budget(policy) -> None:
    """The MCP/API contract is unchanged: a larger policy does not widen it."""
    policy(max_bytes=50 * _MIB)

    with pytest.raises(ValidationError):
        WorkflowRunInput.model_validate(
            {
                "project_id": PROJECT_ID,
                "workflow_id": "wf",
                "files": [_file("receipt", 2 * _MIB)],
            }
        )
