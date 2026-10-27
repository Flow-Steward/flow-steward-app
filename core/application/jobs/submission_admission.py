"""Fair process-bounded admission for resource-heavy payload preparation."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from core.application.management.errors import ServiceUnavailableError

_PAUSE_MARKER_NAME = "job-admission-paused"
_MAINTENANCE_MARKER_NAME = "maintenance-state.json"
_CANONICAL_RUNTIME_DIR = Path("/run/flow-steward-runtime")


def lifecycle_submission_pause_active() -> bool:
    """Read the shared, host-published lifecycle pause marker."""
    return (_CANONICAL_RUNTIME_DIR / _PAUSE_MARKER_NAME).is_file()


def maintenance_submission_pause_active() -> bool:
    """Read the independent shared maintenance marker."""
    return (_CANONICAL_RUNTIME_DIR / _MAINTENANCE_MARKER_NAME).is_file()


def public_submission_pause_active() -> bool:
    """Pause new work while either lifecycle owner has closed admission."""
    return lifecycle_submission_pause_active() or maintenance_submission_pause_active()


class PublicSubmissionPausedError(ServiceUnavailableError):
    """A lifecycle operation has temporarily paused new public jobs."""


class SubmissionAdmissionLease:
    """Idempotently releases one acquired admission slot."""

    def __init__(self, release_slot: Callable[[], None]) -> None:
        self._release_slot = release_slot
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            self._release_slot()


class SubmissionAdmissionController:
    """Reject excess preparations without waiting or consuming request bodies."""

    def __init__(
        self,
        *,
        capacity: int,
        per_account_capacity: int | None = None,
        per_principal_capacity: int | None = None,
        lifecycle_pause_active: Callable[[], bool] | None = None,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.per_account_capacity = max(
            1, min(self.capacity, int(per_account_capacity or self.capacity))
        )
        self.per_principal_capacity = max(
            1,
            min(
                self.per_account_capacity, int(per_principal_capacity or self.per_account_capacity)
            ),
        )
        self._lock = threading.Lock()
        self._active_total = 0
        self._active_accounts: Counter[str] = Counter()
        self._active_principals: Counter[tuple[str, str]] = Counter()
        self._lifecycle_pause_active = lifecycle_pause_active or (lambda: False)

    def try_acquire(
        self,
        *,
        account_id: str = "",
        principal_id: str = "",
    ) -> SubmissionAdmissionLease | None:
        if self._lifecycle_pause_active():
            return None
        account_key = str(account_id or "unknown-account").strip() or "unknown-account"
        principal_key = str(principal_id or account_key).strip() or account_key
        scoped_principal = (account_key, principal_key)
        with self._lock:
            if self._lifecycle_pause_active():
                return None
            if (
                self._active_total >= self.capacity
                or self._active_accounts[account_key] >= self.per_account_capacity
                or self._active_principals[scoped_principal] >= self.per_principal_capacity
            ):
                return None
            self._active_total += 1
            self._active_accounts[account_key] += 1
            self._active_principals[scoped_principal] += 1

        def release_slot() -> None:
            with self._lock:
                self._active_total -= 1
                self._active_accounts[account_key] -= 1
                self._active_principals[scoped_principal] -= 1
                if not self._active_accounts[account_key]:
                    del self._active_accounts[account_key]
                if not self._active_principals[scoped_principal]:
                    del self._active_principals[scoped_principal]

        return SubmissionAdmissionLease(release_slot)


class PublicSubmissionAdmissionBoundary:
    """The sole lifecycle-pause gate for every new job enqueue.

    Payload parsers may use ``SubmissionAdmissionController`` for bounded body
    staging, but only this boundary reads the lifecycle marker.  This keeps
    CLI, MCP, HTTP, webhook, provider and scheduled submission behaviour
    identical. Existing workers may continue, but no caller-supplied metadata
    can authorize a new enqueue while the lifecycle pause is active.
    """

    def __init__(
        self,
        *,
        lifecycle_pause_active: Callable[[], bool],
        controller: SubmissionAdmissionController | None = None,
    ) -> None:
        self._lifecycle_pause_active = lifecycle_pause_active
        self._controller = controller

    @contextmanager
    def admit(
        self,
        *,
        task_source: str,
        meta: dict[str, Any] | None,
    ) -> Iterator[SubmissionAdmissionLease | None]:
        # Classification fields belong to the submitted job and are therefore
        # never authorization to bypass a lifecycle-wide new-job pause.
        del task_source, meta
        if self._lifecycle_pause_active():
            raise PublicSubmissionPausedError(
                "New public task submission is temporarily paused; retry later"
            )
        if self._controller is None:
            yield None
            return
        lease = self._controller.try_acquire()
        if lease is None:
            raise PublicSubmissionPausedError(
                "New public task submission is temporarily paused; retry later"
            )
        try:
            yield lease
        finally:
            lease.release()


public_submission_admission = PublicSubmissionAdmissionBoundary(
    lifecycle_pause_active=public_submission_pause_active,
)


def admit_public_submission(
    *, task_source: str, meta: dict[str, Any] | None
) -> AbstractContextManager[SubmissionAdmissionLease | None]:
    """Enter the canonical lifecycle admission boundary for one new job."""
    return public_submission_admission.admit(task_source=task_source, meta=meta)
