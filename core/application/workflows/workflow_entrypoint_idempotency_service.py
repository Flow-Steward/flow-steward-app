"""Idempotency claim, replay adoption, and recovery for workflow entrypoints."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from core.application.workflows.workflow_entrypoint_idempotency import (
    accept_idempotency_reservation,
    claim_idempotency_reservation,
    fail_idempotency_reservation,
    find_existing_idempotent_job,
)
from core.application.workflows.workflow_entrypoint_models import (
    WorkflowEntrypointIdempotencyResolution,
)


def durable_job_dispatch_confirmed(logger: Any, job_id: str) -> bool:
    """Return whether a durable job has proof that execution was dispatched."""
    get_job = getattr(logger, "get_job", None)
    if not callable(get_job):
        return False
    try:
        job = get_job(str(job_id))
    except Exception:
        return False
    if not isinstance(job, dict):
        return False
    worker_id = str(job.get("worker_id") or "").strip()
    meta = job.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            meta = {}
    delivery = meta.get("queue_delivery") if isinstance(meta, dict) else {}
    delivery = delivery if isinstance(delivery, dict) else {}
    dispatch_state = str(delivery.get("dispatch_state") or "").strip().lower()
    delivery_state = str(delivery.get("state") or "").strip().lower()
    return (
        dispatch_state == "publisher_confirmed"
        or bool(delivery.get("delivery_succeeded"))
        or delivery_state == "delivery_succeeded"
        or bool(worker_id)
    )


class WorkflowEntrypointIdempotencyService:
    """Own claim, stale recovery, replay adoption, and safe redelivery decisions."""

    def __init__(
        self,
        *,
        logger: Any,
        entry_key: str,
        request_fingerprint: str,
        claim_owner_id: str,
        redeliver_task: Callable[..., Any] | None,
        artifact_plane: Any,
        claim_reservation: Callable[..., dict[str, Any]] = claim_idempotency_reservation,
        find_existing_job: Callable[..., str | None] = find_existing_idempotent_job,
        accept_reservation: Callable[..., Any] = accept_idempotency_reservation,
        fail_reservation: Callable[..., Any] = fail_idempotency_reservation,
        dispatch_confirmed: Callable[[Any, str], bool] = durable_job_dispatch_confirmed,
    ) -> None:
        self._logger = logger
        self._entry_key = str(entry_key or "").strip()
        self._request_fingerprint = str(request_fingerprint or "").strip()
        self._claim_owner_id = str(claim_owner_id or "").strip()
        self._redeliver_task = redeliver_task
        self._artifact_plane = artifact_plane
        self._claim_reservation = claim_reservation
        self._find_existing_job_fn = find_existing_job
        self._accept_reservation = accept_reservation
        self._fail_reservation = fail_reservation
        self._dispatch_confirmed = dispatch_confirmed

    def fail(self, idempotency_key: str, error_text: str) -> None:
        self._fail_reservation(
            self._logger,
            entry_key=self._entry_key,
            idempotency_key=idempotency_key,
            request_id=self._claim_owner_id,
            error_text=error_text,
            request_fingerprint=self._request_fingerprint,
        )

    def accept(self, idempotency_key: str, job_id: str, *, request_id: str = "") -> None:
        self._accept_reservation(
            self._logger,
            entry_key=self._entry_key,
            idempotency_key=idempotency_key,
            request_id=str(request_id or self._claim_owner_id),
            job_id=job_id,
            request_fingerprint=self._request_fingerprint,
        )

    def _delete_recovery_artifacts(self, claim: dict[str, Any]) -> None:
        journal = claim.get("recovery_journal")
        pending_ids = journal.get("pending_artifact_ids") if isinstance(journal, dict) else []
        delete_artifact = getattr(self._artifact_plane, "delete_artifact", None)
        if not callable(delete_artifact):
            return
        for artifact_id in pending_ids if isinstance(pending_ids, list) else []:
            try:
                delete_artifact(str(artifact_id))
            except Exception:
                continue

    def _find_existing_job(self, claim: dict[str, Any], idempotency_key: str) -> str:
        existing_job_id = str(claim.get("job_id") or "").strip()
        if existing_job_id and not bool(claim.get("recovered")):
            return existing_job_id
        try:
            return str(
                self._find_existing_job_fn(
                    self._logger,
                    entry_key=self._entry_key,
                    idempotency_key=idempotency_key,
                )
                or ""
            ).strip()
        except Exception:
            return existing_job_id

    def resolve(self, idempotency_key: str) -> WorkflowEntrypointIdempotencyResolution:
        """Claim a key or return one stable replay/error decision."""
        clean_key = str(idempotency_key or "").strip()
        try:
            claim = self._claim_reservation(
                self._logger,
                entry_key=self._entry_key,
                idempotency_key=clean_key,
                request_id=self._claim_owner_id,
                request_fingerprint=self._request_fingerprint,
            )
        except Exception as exc:
            missing_table = (
                "request_idempotency" in str(exc).lower() and "does not exist" in str(exc).lower()
            )
            return WorkflowEntrypointIdempotencyResolution(
                failure_kind="unavailable",
                code="idempotency_storage_unavailable"
                if missing_table
                else "idempotency_reservation_failed",
                message="Idempotency reservation storage is unavailable"
                if missing_table
                else "Idempotency reservation failed; retry later",
            )

        outcome = str(claim.get("outcome") or "").strip().lower()
        if outcome == "mismatch":
            return WorkflowEntrypointIdempotencyResolution(
                claim=claim,
                failure_kind="conflict",
                code="idempotency_payload_mismatch",
                message="Idempotency key was used with a different payload",
            )
        if outcome == "expired":
            return WorkflowEntrypointIdempotencyResolution(
                claim=claim,
                failure_kind="gone",
                code="idempotency_result_expired",
                message="Idempotency replay result has expired",
            )

        claimed = bool(claim.get("claimed"))
        recovered = claimed and bool(claim.get("recovered"))
        identity = (
            {
                "scope_key": str(claim.get("scope_key") or "").strip(),
                "key_digest": str(claim.get("key_digest") or "").strip(),
                "request_fingerprint": self._request_fingerprint,
            }
            if claimed
            else {}
        )
        existing_job_id = self._find_existing_job(claim, clean_key)
        if recovered and not existing_job_id:
            self._delete_recovery_artifacts(claim)

        if existing_job_id:
            dispatch_confirmed = self._dispatch_confirmed(
                self._logger,
                existing_job_id,
            )
            if not dispatch_confirmed and recovered:
                try:
                    dispatch_confirmed = bool(
                        self._redeliver_task(bl=self._logger, job_id=existing_job_id)
                        if callable(self._redeliver_task)
                        else False
                    )
                except Exception:
                    self.fail(clean_key, "replay_recovery_publish_failed")
                    return WorkflowEntrypointIdempotencyResolution(
                        claimed=claimed,
                        recovered=recovered,
                        claim=claim,
                        identity=identity,
                        failure_kind="unavailable",
                        code="idempotency_recovery_publish_failed",
                        message="Webhook queue recovery failed; retry later",
                    )
            if not dispatch_confirmed:
                return WorkflowEntrypointIdempotencyResolution(
                    claimed=claimed,
                    recovered=recovered,
                    claim=claim,
                    identity=identity,
                    failure_kind="conflict",
                    code=(
                        "idempotency_recovery_not_safe" if recovered else "idempotency_in_progress"
                    ),
                    message=(
                        "Recovered idempotency job has no confirmed queue dispatch"
                        if recovered
                        else "Idempotency-Key is already being processed"
                    ),
                    replay_job_id=existing_job_id,
                )
            with suppress(Exception):
                self.accept(
                    clean_key,
                    existing_job_id,
                    request_id=str(claim.get("request_id") or ""),
                )
            return WorkflowEntrypointIdempotencyResolution(
                claimed=claimed,
                recovered=recovered,
                claim=claim,
                identity=identity,
                replay_job_id=existing_job_id,
            )

        if claimed:
            return WorkflowEntrypointIdempotencyResolution(
                proceed=True,
                claimed=True,
                recovered=recovered,
                claim=claim,
                identity=identity,
            )
        return WorkflowEntrypointIdempotencyResolution(
            claim=claim,
            failure_kind="conflict",
            code="idempotency_in_progress",
            message="Idempotency-Key is already being processed",
        )


__all__ = [
    "WorkflowEntrypointIdempotencyService",
    "durable_job_dispatch_confirmed",
]
