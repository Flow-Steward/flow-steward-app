"""SEC-014: Posture Scoring Engine — deterministic security health score 0..100.

Scoring is based on findings from the last time window (default 24h).
Each finding deducts from a base score of 100 by severity.

Levels:
- hardened: 90-100
- secure: 70-89
- needs_attention: 40-69
- at_risk: 0-39
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SEVERITY_DEDUCTIONS: dict[str, int] = {
    "critical": 15,
    "high": 8,
    "medium": 3,
    "low": 1,
}

LEVEL_THRESHOLDS: list[tuple[int, str]] = [
    (90, "hardened"),
    (70, "secure"),
    (40, "needs_attention"),
    (0, "at_risk"),
]


@dataclass
class PostureSnapshot:
    project_id: str
    score: int
    level: str
    category_scores: dict[str, int]
    finding_counts: dict[str, int]


def _score_from_counts(counts: dict[str, int]) -> int:
    """Compute score from severity → count mapping."""
    score = 100
    for severity, count in counts.items():
        deduction = SEVERITY_DEDUCTIONS.get(severity, 0) * count
        score -= deduction
    return max(0, min(100, score))


def _level_from_score(score: int) -> str:
    """Map score to level string."""
    for threshold, level in LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return "at_risk"


def compute_posture(
    project_id: str,
    finding_counts: dict[str, int] | None = None,
    category_counts: dict[str, dict[str, int]] | None = None,
) -> PostureSnapshot:
    """Compute posture from provided counts (deterministic, no DB access).

    Args:
        project_id: The project ID.
        finding_counts: Overall ``{severity: count}`` dict.
        category_counts: Per-scanner ``{scanner: {severity: count}}`` dict.
    """
    counts = finding_counts or {}
    score = _score_from_counts(counts)
    level = _level_from_score(score)

    cat_scores: dict[str, int] = {}
    if category_counts:
        for scanner, cat_count in category_counts.items():
            cat_scores[scanner] = _score_from_counts(cat_count)

    return PostureSnapshot(
        project_id=project_id,
        score=score,
        level=level,
        category_scores=cat_scores,
        finding_counts=counts,
    )


def compute_and_save_posture(
    project_id: str,
    *,
    hours: int = 24,
    account_id: str = "default",
) -> PostureSnapshot | None:
    """Fetch finding counts from DB, compute posture, save snapshot.

    Returns PostureSnapshot or None if DB not available.
    """
    try:
        from core.infrastructure.security.event_logger import get_security_logger

        sec_logger = get_security_logger()
        if not sec_logger:
            return None

        counts = sec_logger.count_findings_by_severity(project_id, hours=hours)
        snapshot = compute_posture(project_id, finding_counts=counts)

        sec_logger.save_posture_snapshot(
            project_id=project_id,
            score=snapshot.score,
            level=snapshot.level,
            category_scores=snapshot.category_scores,
            finding_counts=snapshot.finding_counts,
            account_id=account_id,
        )

        return snapshot

    except Exception:
        logger.debug("compute_and_save_posture failed", exc_info=True)
        return None
