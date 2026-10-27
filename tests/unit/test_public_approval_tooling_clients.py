from __future__ import annotations

from unittest.mock import MagicMock

from core.application.tooling.testing import cutover_smoke


def test_cutover_smoke_uses_canonical_approval_contract(monkeypatch) -> None:
    response = MagicMock(status_code=202)
    post = MagicMock(return_value=response)
    session = MagicMock()
    session.cookies = {}
    session.post = post

    assert (
        cutover_smoke._approve(
            session,
            "https://example.test",
            True,
            "thread-42",
            "gate-42",
            "looks good",
        )
        == 202
    )

    _, kwargs = post.call_args
    assert "params" not in kwargs
    assert kwargs["json"] == {
        "approval_id": "gate-42",
        "approved": True,
        "comment": "looks good",
    }
    assert kwargs["headers"] == {"Idempotency-Key": "cutover-approval:gate-42:approve"}
