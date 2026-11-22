"""The idempotency fingerprint of an approval decision (ADR 0075, item 9).

``values`` joins the fingerprint only when a decision carries it, so a decision
recorded before typed review fields existed, retried with the same key after an
update, still replays instead of conflicting.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from core.application.management import approval_decision_service as decision_module
from core.application.management.approval_decision_service import ApprovalDecisionService
from core.application.management.command_models_api_inputs import ApprovalDecisionInput
from core.application.management.errors import ServiceConflictError
from core.application.management.scope import ScopeContext

#: Computed from the fingerprint code as released before ``values`` existed:
#: sha256 of the canonical JSON of approval_id, approved, action, comment,
#: override_content and external_actor. It must never change.
LEGACY_FINGERPRINT = "7ffdcd845fdcf21329abdc72802c90fb80d377049dfe0febf8bc4b08e6b0bf87"


def _captured_fingerprint(monkeypatch: pytest.MonkeyPatch, decision: ApprovalDecisionInput) -> str:
    captured: dict[str, Any] = {}

    class _Service:
        def __init__(self, _repository: Any) -> None:
            pass

        def claim(self, *, request_fingerprint: str, **_kwargs: Any) -> Any:
            captured["fingerprint"] = request_fingerprint
            return SimpleNamespace(outcome="mismatch", claimed=False, response=None)

    @contextmanager
    def _transaction(_conn: Any):
        yield object()

    monkeypatch.setattr(decision_module, "RequestIdempotencyService", _Service)
    monkeypatch.setattr(decision_module, "RequestIdempotencyRepository", lambda conn: conn)
    monkeypatch.setattr(decision_module, "transaction_connection", _transaction)
    service = ApprovalDecisionService(
        lock_job_row=lambda *_args: None,
        current_interrupt_payload=lambda _thread: {},
        persist_decision=lambda **_kwargs: None,
    )
    with pytest.raises(ServiceConflictError):
        service.claim_and_record(
            bl=SimpleNamespace(conn=object()),
            thread_id="thread-1",
            scope=ScopeContext(account_id="account-1", actor_id="alice"),
            decision=decision,
            token_principal=False,
            idempotency_key="key-1",
        )
    return str(captured["fingerprint"])


def test_decision_without_values_keeps_the_released_fingerprint(monkeypatch) -> None:
    decision = ApprovalDecisionInput(approved=True, comment="Looks right", approval_id="approval-1")

    assert _captured_fingerprint(monkeypatch, decision) == LEGACY_FINGERPRINT
