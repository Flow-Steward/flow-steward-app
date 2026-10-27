"""Atomic validation and idempotency coordination for approval decisions."""

from __future__ import annotations

import threading
import uuid
import weakref
from collections.abc import Callable, Mapping
from contextlib import nullcontext, suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.canonical_json import canonical_json_hash
from core.application.idempotency import (
    IdempotencyScope,
    RequestIdempotencyRepository,
    RequestIdempotencyService,
)
from core.infrastructure.db.db_pool import transaction_connection

from .approval_repository import decode_job_meta
from .command_models_api_inputs import ApprovalDecisionInput, ApprovalResumeAcceptedView
from .errors import (
    ServiceConflictError,
    ServiceError,
    ServiceNotFoundError,
    ServiceUnavailableError,
)
from .scope import ScopeContext

_lock_guard = threading.Lock()
_locks: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()


def approval_lock(thread_id: str) -> threading.RLock:
    """Serialize decisions on one approval thread inside this process."""
    key = str(thread_id or "").strip()
    with _lock_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


class ApprovalDecisionService:
    """Own first-decision-wins validation and the atomic idempotency transaction."""

    DEFAULT_ALLOWED_ACTIONS = ("approve", "reject")

    def __init__(
        self,
        *,
        lock_job_row: Callable[[Any, str], dict[str, Any] | None],
        current_interrupt_payload: Callable[[str], dict[str, Any]],
        persist_decision: Callable[..., None],
    ) -> None:
        self._lock_job_row = lock_job_row
        self._current_interrupt_payload = current_interrupt_payload
        self._persist_decision = persist_decision

    @staticmethod
    def current_approval_id(payload: Mapping[str, Any]) -> str:
        return str(
            payload.get("approval_id") or payload.get("interrupt_id") or payload.get("id") or ""
        ).strip()

    @classmethod
    def normalized_allowed_actions(cls, payload: Mapping[str, Any]) -> list[str]:
        """Return the explicit gate allow-list or the safe human-review default."""
        raw_actions = payload.get("allowed_actions")
        if not isinstance(raw_actions, list):
            return list(cls.DEFAULT_ALLOWED_ACTIONS)
        normalized: list[str] = []
        for item in raw_actions:
            action = str(item or "").strip().lower()
            if action and action not in normalized:
                normalized.append(action)
        return normalized or list(cls.DEFAULT_ALLOWED_ACTIONS)

    @staticmethod
    def state_based_approval_id(row: Mapping[str, Any] | None) -> str:
        """The durable identity of a gate that has no interrupt to identify it.

        A workflow Test Run parks at a phase completion gate by *state*: the
        ephemeral runner stops streaming the graph as soon as the state says
        `awaiting_approval`, so the graph never reaches a LangGraph `interrupt()`
        and the checkpoint carries no interrupt. `current_approval_id` therefore
        returns "" and every decision was rejected as a stale gate, which is the
        undecidable half of `AUDIT-038` symptom (b). `graph_resume_runtime`
        already supports resuming these runs from state, so only the identity was
        missing; it is written onto the anchor job when the run parks.

        Read only from the job row, never from the request, so this stays a
        server-side fact the caller cannot assert.
        """
        if row is None:
            return ""
        meta = decode_job_meta(row.get("meta"))
        if not isinstance(meta, Mapping):
            return ""
        if not meta.get("workflow_test_run"):
            return ""
        return str(meta.get("state_approval_id") or "").strip()

    def validate_current_gate(
        self,
        *,
        row: Mapping[str, Any] | None,
        payload: Mapping[str, Any],
        decision: ApprovalDecisionInput,
        token_principal: bool,
    ) -> str:
        del token_principal
        if row is not None and str(row.get("status") or "").strip() != "awaiting_approval":
            raise ServiceConflictError(
                "Job is not awaiting approval",
                payload={
                    "error": "Job is not awaiting approval",
                    "code": "approval_already_resolved",
                },
            )
        current_id = self.current_approval_id(payload)
        if not current_id:
            # No live interrupt. A state-based gate still has a durable identity
            # on its job row; anything else stays stale.
            current_id = self.state_based_approval_id(row)
        if not current_id or decision.approval_id != current_id:
            raise ServiceConflictError(
                "Approval gate is stale",
                payload={
                    "error": "Approval gate is stale",
                    "code": "stale_approval_gate",
                    "approval_id": current_id,
                },
            )
        allowed_set = set(self.normalized_allowed_actions(payload))
        requested = str(decision.action or ("approve" if decision.approved else "reject"))
        if requested.lower() not in allowed_set:
            raise ServiceConflictError(
                "Approval action is not allowed by the current gate",
                payload={
                    "error": "Approval action is not allowed by the current gate",
                    "code": "approval_action_not_allowed",
                    "allowed_actions": sorted(allowed_set),
                },
            )
        return current_id or decision.approval_id or uuid.uuid4().hex

    def claim_and_record(
        self,
        *,
        bl: Any,
        thread_id: str,
        scope: ScopeContext,
        decision: ApprovalDecisionInput,
        token_principal: bool,
        idempotency_key: str,
        before_new_decision: Callable[[], None] | None = None,
    ) -> tuple[dict[str, Any], str, RequestIdempotencyService | None, Any, bool, bool, Any]:
        lock = approval_lock(thread_id)
        lock.acquire()
        idempotency_service = None
        idempotency_claim = None
        conn = getattr(bl, "conn", None)
        try:
            transaction = transaction_connection(conn) if conn is not None else nullcontext()
            with transaction as transaction_conn:
                if str(idempotency_key or "").strip():
                    account_id = str(getattr(scope, "account_id", "") or DEFAULT_ACCOUNT_ID)
                    fingerprint = canonical_json_hash(
                        {
                            "approval_id": decision.approval_id,
                            "approved": decision.approved,
                            "action": decision.action,
                            "comment": decision.comment,
                            "override_content": decision.override_content,
                            "external_actor": (
                                decision.external_actor.model_dump(mode="json")
                                if decision.external_actor
                                else None
                            ),
                        }
                    )
                    if transaction_conn is None:
                        raise ServiceUnavailableError("Idempotency storage is unavailable")
                    idempotency_service = RequestIdempotencyService(
                        RequestIdempotencyRepository(transaction_conn)
                    )
                    idempotency_claim = idempotency_service.claim(
                        scope=IdempotencyScope(
                            operation="approval.decision",
                            account_id=account_id,
                            dimensions=(
                                ("thread", str(thread_id)),
                                ("approval", str(decision.approval_id or "current")),
                            ),
                        ),
                        opaque_key=str(idempotency_key),
                        request_fingerprint=fingerprint,
                        request_id=uuid.uuid4().hex,
                    )
                    if idempotency_claim.outcome == "replay":
                        replay = dict(idempotency_claim.response or {})
                        replay["deduplicated"] = True
                        return (
                            {},
                            "",
                            None,
                            ApprovalResumeAcceptedView.model_validate(replay),
                            True,
                            True,
                            None,
                        )
                    if idempotency_claim.outcome == "mismatch":
                        raise ServiceConflictError(
                            "Idempotency key was used with a different approval decision",
                            payload={
                                "error": "Idempotency key payload mismatch",
                                "code": "idempotency_payload_mismatch",
                            },
                        )
                    if idempotency_claim.outcome == "expired":
                        raise ServiceError(
                            "Idempotency replay result has expired",
                            status_code=410,
                            payload={
                                "error": "Idempotency replay result has expired",
                                "code": "idempotency_result_expired",
                            },
                        )
                    if not idempotency_claim.claimed:
                        raise ServiceConflictError(
                            "An identical approval decision is still in progress",
                            payload={
                                "error": "Approval decision is in progress",
                                "code": "idempotency_in_progress",
                                "retry_after": 2,
                            },
                        )

                if before_new_decision is not None:
                    before_new_decision()

                row = self._lock_job_row(bl, thread_id)
                if row is None:
                    raise ServiceNotFoundError("Approval job not found")
                try:
                    interrupt_payload = self._current_interrupt_payload(thread_id)
                except Exception as exc:
                    from core.application.jobs.workflow_run_projection import (
                        MultipleApprovalGatesUnsupported,
                    )

                    if isinstance(exc, MultipleApprovalGatesUnsupported):
                        raise ServiceConflictError(
                            str(exc), payload={"error": exc.code, "code": exc.code}
                        ) from exc
                    raise
                approval_id = self.validate_current_gate(
                    row=row,
                    payload=interrupt_payload,
                    decision=decision,
                    token_principal=token_principal,
                )
                persistence_result = self._persist_decision(
                    bl=bl,
                    transaction_conn=transaction_conn,
                    row=row,
                    thread_id=thread_id,
                    approval_id=approval_id,
                    payload=interrupt_payload,
                    decision=decision,
                    scope=scope,
                )
                accepted_in_transaction = False
                if idempotency_service is not None and idempotency_claim is not None:
                    effective_approved = (
                        bool(decision.approved)
                        or str(decision.action or "").strip().lower() == "override"
                    )
                    accepted_response = ApprovalResumeAcceptedView(
                        job_id=str(row.get("id") or ""),
                        thread_id=thread_id,
                        approval_id=approval_id,
                        deduplicated=False,
                        external_actor=decision.external_actor,
                        approved=effective_approved,
                        action=decision.action,
                        comment=decision.comment,
                        override_content=decision.override_content,
                        actor=scope.actor_name,
                        actor_type=str(scope.principal_type or "").strip() or None,
                        status=(
                            "resume_pending" if persistence_result is not None else "in_progress"
                        ),
                    )
                    idempotency_service.accept(
                        claim=idempotency_claim,
                        job_id=None,
                        response=accepted_response.model_dump(mode="json"),
                        result_expires_at=datetime.now(UTC) + timedelta(days=30),
                    )
                    accepted_in_transaction = True
                return (
                    interrupt_payload,
                    approval_id,
                    idempotency_service,
                    idempotency_claim,
                    accepted_in_transaction,
                    True,
                    persistence_result,
                )
        except Exception:
            if (
                idempotency_service is not None
                and idempotency_claim is not None
                and idempotency_claim.claimed
            ):
                with suppress(Exception):
                    fail_transaction = (
                        transaction_connection(conn) if conn is not None else nullcontext()
                    )
                    with fail_transaction as fail_conn:
                        if fail_conn is not None:
                            RequestIdempotencyService(RequestIdempotencyRepository(fail_conn)).fail(
                                claim=idempotency_claim, error_code="decision_rejected"
                            )
            raise
        finally:
            lock.release()


__all__ = ["ApprovalDecisionService"]
