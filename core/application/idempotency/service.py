"""Application semantics for request reservation, replay, and recovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from core.application.idempotency.models import (
    IdempotencyClaim,
    IdempotencyOutcome,
    IdempotencyScope,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _response(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else None
    return None


class RequestIdempotencyService:
    def __init__(
        self,
        repository,
        *,
        now: Callable[[], datetime] = _utc_now,
        stale_after: timedelta = timedelta(seconds=120),
        tombstone_retention: timedelta = timedelta(days=7),
        reclaim_guard: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.repository = repository
        self.now = now
        self.stale_after = stale_after
        self.tombstone_retention = tombstone_retention
        self.reclaim_guard = reclaim_guard

    @staticmethod
    def digest_key(opaque_key: str) -> str:
        clean_key = str(opaque_key or "").strip()
        if not clean_key:
            raise ValueError("Idempotency key must not be empty")
        digest = hashlib.sha256(clean_key.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def claim(
        self,
        *,
        scope: IdempotencyScope,
        opaque_key: str,
        request_fingerprint: str,
        request_id: str,
    ) -> IdempotencyClaim:
        scope_key = scope.key()
        key_digest = self.digest_key(opaque_key)
        clean_fingerprint = str(request_fingerprint or "").strip()
        clean_request_id = str(request_id or "").strip()
        if not scope_key or not key_digest or not clean_fingerprint or not clean_request_id:
            raise ValueError("Idempotency claim requires a complete storage identity")
        identity = {
            "scope_key": scope_key,
            "key_digest": key_digest,
            "request_fingerprint": clean_fingerprint,
            "request_id": clean_request_id,
        }

        def result(
            outcome: IdempotencyOutcome,
            *,
            job_id: str | None = None,
            response: dict[str, Any] | None = None,
            result_expires_at: datetime | None = None,
            recovered: bool = False,
        ) -> IdempotencyClaim:
            return IdempotencyClaim(
                outcome=outcome,
                scope_key=scope_key,
                key_digest=key_digest,
                request_fingerprint=clean_fingerprint,
                request_id=clean_request_id,
                job_id=job_id,
                response=response,
                result_expires_at=result_expires_at,
                recovered=recovered,
            )

        now = _as_utc(self.now()) or _utc_now()
        lease_expires_at = now + self.stale_after
        inserted = self.repository.insert_pending(
            **identity,
            lease_expires_at=lease_expires_at,
        )
        if inserted:
            return result("claimed")

        row = self.repository.load_for_update(
            scope_key=scope_key,
            key_digest=key_digest,
        )
        if row is None:
            return result("retryable")
        stored_fingerprint = str(row.get("request_fingerprint") or "").strip()
        if stored_fingerprint and stored_fingerprint != clean_fingerprint:
            return result("mismatch")

        status = str(row.get("status") or "").strip()
        now = _as_utc(self.now()) or _utc_now()
        if status == "accepted":
            result_expires_at = _as_utc(row.get("result_expires_at"))
            tombstone_expires_at = _as_utc(row.get("tombstone_expires_at"))
            linked_job_id = str(row.get("job_id") or "").strip()
            # The job FK is cleared on retention deletion.  Until then its
            # idempotency response remains the only safe replay handle, even
            # if a long-running workflow outlives the initial replay window.
            if result_expires_at is None or result_expires_at > now or linked_job_id:
                return result(
                    "replay",
                    job_id=linked_job_id or None,
                    response=_response(row.get("replay_response_jsonb")),
                    result_expires_at=result_expires_at,
                )
            if tombstone_expires_at is None or tombstone_expires_at > now:
                return result("expired")
            return result("retryable")

        lease_expires_at = _as_utc(row.get("lease_expires_at"))
        is_stale = lease_expires_at is None or lease_expires_at <= now
        if status == "pending" and not is_stale:
            return result("in_progress")
        if status not in {"pending", "failed"}:
            return result("retryable")
        if self.reclaim_guard is not None and not self.reclaim_guard(dict(row)):
            return result("in_progress")

        pending_journal = _response(row.get("replay_response_jsonb"))
        reclaimed = self.repository.reclaim(
            **identity,
            lease_expires_at=now + self.stale_after,
        )
        if not reclaimed:
            return result("retryable")
        return result("claimed", response=pending_journal, recovered=True)

    def renew(self, *, claim: IdempotencyClaim) -> bool:
        """Fence side effects by extending only the current request's live lease."""
        if not claim.claimed:
            return False
        now = _as_utc(self.now()) or _utc_now()
        return bool(
            self.repository.renew(
                scope_key=claim.scope_key,
                key_digest=claim.key_digest,
                request_id=claim.request_id,
                lease_expires_at=now + self.stale_after,
            )
        )

    def journal(self, *, claim: IdempotencyClaim, journal: dict[str, Any]) -> bool:
        """Durably record recovery data while the caller still owns the lease."""
        if not claim.claimed:
            return False
        return bool(
            self.repository.journal(
                scope_key=claim.scope_key,
                key_digest=claim.key_digest,
                request_id=claim.request_id,
                journal=dict(journal),
            )
        )

    def accept(
        self,
        *,
        claim: IdempotencyClaim,
        job_id: str | None,
        response: dict[str, Any],
        result_expires_at: datetime,
    ) -> None:
        if not claim.claimed:
            raise ValueError("Only an owned idempotency claim can be accepted")
        expires_at = _as_utc(result_expires_at)
        if expires_at is None or expires_at <= (_as_utc(self.now()) or _utc_now()):
            raise ValueError("Idempotency replay expiry must be in the future")
        self.repository.accept(
            scope_key=claim.scope_key,
            key_digest=claim.key_digest,
            request_id=claim.request_id,
            request_fingerprint=claim.request_fingerprint,
            job_id=job_id,
            response=dict(response),
            result_expires_at=expires_at,
            tombstone_expires_at=expires_at + self.tombstone_retention,
        )

    def fail(self, *, claim: IdempotencyClaim, error_code: str) -> None:
        if not claim.claimed:
            raise ValueError("Only an owned idempotency claim can be failed")
        self.repository.fail(
            scope_key=claim.scope_key,
            key_digest=claim.key_digest,
            request_id=claim.request_id,
            request_fingerprint=claim.request_fingerprint,
            error_code=error_code,
        )
