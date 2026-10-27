"""Application-owned verification policy for a restored Clone target."""

from __future__ import annotations

from core.application.readiness.contracts import ReadinessReport


def require_clone_readiness(report: ReadinessReport) -> None:
    """Fail closed unless canonical readiness proves the restored target is ready."""
    if report.status != "ready" or report.core_failed or report.process_exit_code != 0:
        raise RuntimeError("Clone restore target readiness verification failed")


__all__ = ["require_clone_readiness"]
