from __future__ import annotations

from types import TracebackType
from typing import NoReturn

from core.application.management.errors import ServiceError


class _CompensationFailureCapture:
    """Defer one failed cleanup to the owning extension lifecycle operation.

    The collector must be consumed by ``raise_update_failure`` or the install
    owner's ``BaseExceptionGroup`` before leaving its failure path. This keeps
    cancellation identities while allowing the remaining compensations to run.
    """

    def __init__(self, failures: list[BaseException], note: str) -> None:
        self._failures = failures
        self._note = note

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        if exc is None:
            return False
        exc.add_note(self._note)
        self._failures.append(exc)
        return True


def raise_update_failure(
    exc: BaseException,
    compensation_failures: list[BaseException],
) -> NoReturn:
    if not isinstance(exc, Exception) or any(
        not isinstance(failure, Exception) for failure in compensation_failures
    ):
        if compensation_failures:
            raise BaseExceptionGroup(
                "managed extension update mutation and compensation failed",
                [exc, *compensation_failures],
            ) from None
        raise exc

    recovery_status = (
        "recovery was incomplete" if compensation_failures else "previous version was preserved"
    )
    if isinstance(exc, ServiceError):
        recovery_message = f"Extension update failed and {recovery_status}: {exc.message}"
        source_payload = getattr(exc, "payload", None)
        recovery_payload = dict(source_payload) if isinstance(source_payload, dict) else {}
        recovery_payload["error"] = recovery_message
        if "message" in recovery_payload:
            recovery_payload["message"] = recovery_message
        reported_error = type(exc)(recovery_message, payload=recovery_payload)
        reported_error.status_code = exc.status_code
    else:
        reported_error = ServiceError(f"Extension update failed and {recovery_status}: {exc}")
    if compensation_failures:
        failure_group = BaseExceptionGroup(
            "managed extension update compensation failures",
            compensation_failures,
        )
        reported_error.__dict__["compensation_failures"] = failure_group
        failure_details = "; ".join(
            f"{type(failure).__name__}: {failure}" for failure in compensation_failures
        )
        reported_error.add_note(
            f"{len(compensation_failures)} managed extension update compensation "
            f"steps failed ({failure_details}); see compensation_failures."
        )
    raise reported_error from exc


__all__ = ["raise_update_failure"]
