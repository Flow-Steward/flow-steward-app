from __future__ import annotations

import pytest

from core.application.clone.verification import require_clone_readiness
from core.application.readiness.contracts import ReadinessReport


def test_clone_verification_rejects_blocked_readiness_report() -> None:
    report = ReadinessReport(
        status="blocked",
        core_failed=1,
        process_exit_code=4,
    )

    with pytest.raises(RuntimeError, match="readiness"):
        require_clone_readiness(report)


def test_clone_verification_accepts_only_ready_zero_exit_report() -> None:
    require_clone_readiness(ReadinessReport(status="ready", process_exit_code=0))

    failed = ReadinessReport(status="ready", process_exit_code=4)
    with pytest.raises(RuntimeError, match="readiness"):
        require_clone_readiness(failed)
