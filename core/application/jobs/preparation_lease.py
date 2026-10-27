"""Renewable ownership lease for long-running submission payload preparation."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any


class PreparationLeaseLostError(RuntimeError):
    """Raised when payload preparation no longer owns its durable outbox row."""


class PreparationLeaseHeartbeat:
    """Keep a preparation lease alive while storage adapters may block."""

    def __init__(
        self,
        bl: Any,
        *,
        job_id: str,
        owner_token: str,
        lease_seconds: int = 300,
        interval_seconds: float | None = None,
        renew_preparation: Callable[[Any | None], bool] | None = None,
        additional_heartbeat: Callable[[Any | None], None] | None = None,
        connection_provider: Callable[[], AbstractContextManager[Any]] | None = None,
    ) -> None:
        self.bl = bl
        self.job_id = str(job_id)
        self.owner_token = str(owner_token)
        self.lease_seconds = max(30, int(lease_seconds))
        # Submission preparation also renews the 120-second idempotency lease.
        # Keep a wide scheduling/retry margin even though the outbox lease is
        # normally longer (300 seconds).
        self.interval_seconds = interval_seconds or min(30.0, max(5.0, self.lease_seconds / 3))
        self.renew_preparation = renew_preparation
        self.additional_heartbeat = additional_heartbeat
        self.connection_provider = connection_provider
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def heartbeat(self) -> None:
        connection_context = (
            self.connection_provider()
            if self.connection_provider is not None
            else nullcontext(None)
        )
        with connection_context as connection:
            if self._lost.is_set() or not self._run_preparation_heartbeat(connection):
                self._lost.set()
                raise PreparationLeaseLostError("Submission preparation lease was lost")
            self._run_additional_heartbeat(connection)

    def _run_preparation_heartbeat(self, connection: Any | None) -> bool:
        try:
            return self._renew_preparation(connection)
        except Exception as exc:
            self._lost.set()
            raise PreparationLeaseLostError("Submission preparation lease was lost") from exc

    def _run_additional_heartbeat(self, connection: Any | None) -> None:
        if self.additional_heartbeat is None:
            return
        try:
            self.additional_heartbeat(connection)
        except Exception as exc:
            self._lost.set()
            raise PreparationLeaseLostError("Submission ownership heartbeat was lost") from exc

    def _renew_preparation(self, connection: Any | None) -> bool:
        if self.renew_preparation is not None:
            return bool(self.renew_preparation(connection))
        if connection is None:
            return False
        from core.application.jobs.dispatch_outbox_repository import (
            DispatchOutboxRepository,
        )

        return DispatchOutboxRepository(connection).renew_preparation_lease(
            job_id=self.job_id,
            owner_token=self.owner_token,
            lease_seconds=self.lease_seconds,
        )

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval_seconds):
                self.heartbeat()
        except Exception:
            self._lost.set()

    def __enter__(self) -> PreparationLeaseHeartbeat:
        self.heartbeat()
        self._thread = threading.Thread(
            target=self._run,
            name=f"submission-preparation-lease-{self.job_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(5.0, self.interval_seconds + 0.5))
        if _exc_type is None:
            self.heartbeat()
