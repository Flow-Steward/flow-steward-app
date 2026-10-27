"""Application-owned idempotency lifecycle for public workflow entrypoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.application.idempotency import (
    IdempotencyClaim,
    IdempotencyScope,
    RequestIdempotencyRepository,
    RequestIdempotencyService,
)

_ACCOUNT_ID = "00000000-0000-0000-0000-000000000002"
_REPLAY_RETENTION = timedelta(days=30)


def workflow_entrypoint_idempotency_scope(entry_key: str) -> IdempotencyScope:
    return IdempotencyScope(
        operation="workflow.entrypoint",
        account_id=_ACCOUNT_ID,
        dimensions=(("entrypoint", str(entry_key or "").strip()),),
    )


def _owned_claim(
    *,
    entry_key: str,
    idempotency_key: str,
    request_id: str,
    request_fingerprint: str | None = None,
) -> IdempotencyClaim:
    scope = workflow_entrypoint_idempotency_scope(entry_key)
    clean_entry_key = str(entry_key or "").strip()
    return IdempotencyClaim(
        outcome="claimed",
        scope_key=scope.key(),
        key_digest=RequestIdempotencyService.digest_key(idempotency_key),
        request_fingerprint=(
            str(request_fingerprint or "").strip() or f"workflow-entrypoint:v1:{clean_entry_key}"
        ),
        request_id=str(request_id or "").strip(),
    )


def find_existing_idempotent_job(
    logger: Any,
    *,
    entry_key: str,
    idempotency_key: str,
) -> str | None:
    scope = workflow_entrypoint_idempotency_scope(entry_key)
    job = RequestIdempotencyRepository(logger.conn).find_job_by_request_identity(
        scope_key=scope.key(),
        key_digest=RequestIdempotencyService.digest_key(idempotency_key),
    )
    job_id = str((job or {}).get("id") or "").strip()
    return job_id or None


def claim_idempotency_reservation(
    logger: Any,
    *,
    entry_key: str,
    idempotency_key: str,
    request_id: str,
    request_fingerprint: str | None = None,
    stale_after_seconds: int = 120,
) -> dict[str, Any]:
    clean_entry_key = str(entry_key or "").strip()
    clean_idempotency_key = str(idempotency_key or "").strip()
    clean_request_id = str(request_id or "").strip()
    if not clean_entry_key or not clean_idempotency_key or not clean_request_id:
        return {"claimed": False, "status": "invalid"}
    claim = RequestIdempotencyService(
        RequestIdempotencyRepository(logger.conn),
        stale_after=timedelta(seconds=max(1, int(stale_after_seconds))),
    ).claim(
        scope=workflow_entrypoint_idempotency_scope(clean_entry_key),
        opaque_key=clean_idempotency_key,
        request_fingerprint=(
            str(request_fingerprint or "").strip() or f"workflow-entrypoint:v1:{clean_entry_key}"
        ),
        request_id=clean_request_id,
    )
    status = {
        "claimed": "pending",
        "replay": "accepted",
        "in_progress": "pending",
        "mismatch": "failed",
        "expired": "accepted",
        "retryable": "missing",
    }[claim.outcome]
    return {
        "entry_key": clean_entry_key,
        "request_id": clean_request_id,
        "status": status,
        "outcome": claim.outcome,
        "job_id": claim.job_id,
        "claimed": claim.claimed,
        "recovered": claim.recovered,
        "scope_key": claim.scope_key,
        "key_digest": claim.key_digest,
        "recovery_journal": dict(claim.response or {}),
    }


def accept_idempotency_reservation(
    logger: Any,
    *,
    entry_key: str,
    idempotency_key: str,
    request_id: str,
    job_id: str,
    request_fingerprint: str | None = None,
) -> None:
    RequestIdempotencyService(RequestIdempotencyRepository(logger.conn)).accept(
        claim=_owned_claim(
            entry_key=entry_key,
            idempotency_key=idempotency_key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
        ),
        job_id=job_id,
        response={"job_id": str(job_id)},
        result_expires_at=datetime.now(UTC) + _REPLAY_RETENTION,
    )


def fail_idempotency_reservation(
    logger: Any,
    *,
    entry_key: str,
    idempotency_key: str,
    request_id: str,
    error_text: str,
    request_fingerprint: str | None = None,
) -> None:
    RequestIdempotencyService(RequestIdempotencyRepository(logger.conn)).fail(
        claim=_owned_claim(
            entry_key=entry_key,
            idempotency_key=idempotency_key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
        ),
        error_code=error_text,
    )


def renew_idempotency_reservation(
    logger: Any,
    *,
    entry_key: str,
    idempotency_key: str,
    request_id: str,
    request_fingerprint: str,
) -> bool:
    return RequestIdempotencyService(RequestIdempotencyRepository(logger.conn)).renew(
        claim=_owned_claim(
            entry_key=entry_key,
            idempotency_key=idempotency_key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
        )
    )


def journal_idempotency_reservation(
    logger: Any,
    *,
    entry_key: str,
    idempotency_key: str,
    request_id: str,
    request_fingerprint: str,
    journal: dict[str, Any],
) -> bool:
    return RequestIdempotencyService(RequestIdempotencyRepository(logger.conn)).journal(
        claim=_owned_claim(
            entry_key=entry_key,
            idempotency_key=idempotency_key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
        ),
        journal=dict(journal),
    )
