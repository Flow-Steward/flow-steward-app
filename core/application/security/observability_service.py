"""Application-level security observability queries."""

from __future__ import annotations

from typing import Any

from core.infrastructure.security.event_logger import get_security_logger
from core.infrastructure.security.posture import compute_and_save_posture

#: Scan modes in which a finding actually stops the operation. In "monitor" the scanners run
#: and redaction of stored data still applies, but nothing is blocked; "disabled" turns the
#: scanners off entirely.
_BLOCKING_SCAN_MODES = frozenset({"enforce_standard", "enforce_strict"})


def _enforcement_payload() -> dict[str, Any]:
    """AC17-R74: say which mode the scanners are actually running in.

    A posture score describes findings. It says nothing about whether a finding would be
    acted on, so a production install running in monitor mode reported a perfect score while
    blocking was off — and the screen had no way to mention it, because the mode never
    travelled to the client. The runtime logs this at every start; the UI could not.
    """
    from core.infrastructure.security.config import SECURITY_SCAN_MODE

    mode = str(SECURITY_SCAN_MODE or "").strip().lower()
    return {
        "scan_mode": mode,
        "enforcement_blocking": mode in _BLOCKING_SCAN_MODES,
    }


def get_posture_payload(
    project_id: Any | None = None,
    *,
    account_id: str | None = None,
) -> dict[str, Any] | None:
    sec_logger = get_security_logger()
    if not sec_logger:
        return None

    snapshot = sec_logger.get_latest_posture_snapshot(project_id=project_id, account_id=account_id)
    if snapshot:
        return {
            "score": snapshot.get("score", 100),
            "level": snapshot.get("level", "hardened"),
            "category_scores": snapshot.get("category_scores"),
            "finding_counts": snapshot.get("finding_counts"),
            "snapshot_at": str(snapshot.get("created_at") or ""),
            **_enforcement_payload(),
        }

    if project_id is None:
        return None

    computed = compute_and_save_posture(project_id)
    if not computed:
        return None
    return {
        "score": computed.score,
        "level": computed.level,
        "category_scores": computed.category_scores,
        "finding_counts": computed.finding_counts,
        "snapshot_at": "live",
        **_enforcement_payload(),
    }


def empty_posture_payload(*, status: str) -> dict[str, Any]:
    return {"score": 100, "level": "hardened", "status": status, **_enforcement_payload()}
