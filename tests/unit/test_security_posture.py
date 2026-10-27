"""SEC-014: Tests for the posture scoring engine.

Run:
    pytest tests/unit/test_security_posture.py -v
"""

from __future__ import annotations

from core.infrastructure.security.posture import (
    PostureSnapshot,
    _level_from_score,
    _score_from_counts,
    compute_posture,
)


class TestScoreFromCounts:
    def test_no_findings(self):
        assert _score_from_counts({}) == 100

    def test_critical_deduction(self):
        assert _score_from_counts({"critical": 1}) == 85

    def test_high_deduction(self):
        assert _score_from_counts({"high": 1}) == 92

    def test_medium_deduction(self):
        assert _score_from_counts({"medium": 1}) == 97

    def test_low_deduction(self):
        assert _score_from_counts({"low": 1}) == 99

    def test_mixed_severities(self):
        score = _score_from_counts({"critical": 2, "high": 3, "medium": 5, "low": 10})
        # 100 - 30 - 24 - 15 - 10 = 21
        assert score == 21

    def test_floor_at_zero(self):
        assert _score_from_counts({"critical": 100}) == 0

    def test_deterministic(self):
        counts = {"critical": 1, "high": 2, "medium": 3}
        s1 = _score_from_counts(counts)
        s2 = _score_from_counts(counts)
        assert s1 == s2


class TestLevelFromScore:
    def test_hardened(self):
        assert _level_from_score(100) == "hardened"
        assert _level_from_score(90) == "hardened"

    def test_secure(self):
        assert _level_from_score(89) == "secure"
        assert _level_from_score(70) == "secure"

    def test_needs_attention(self):
        assert _level_from_score(69) == "needs_attention"
        assert _level_from_score(40) == "needs_attention"

    def test_at_risk(self):
        assert _level_from_score(39) == "at_risk"
        assert _level_from_score(0) == "at_risk"


class TestComputePosture:
    def test_clean_project(self):
        snap = compute_posture(1, finding_counts={})
        assert snap.score == 100
        assert snap.level == "hardened"
        assert snap.project_id == 1

    def test_with_findings(self):
        snap = compute_posture(1, finding_counts={"critical": 1, "high": 2})
        # 100 - 15 - 16 = 69
        assert snap.score == 69
        assert snap.level == "needs_attention"

    def test_with_category_counts(self):
        snap = compute_posture(
            1,
            finding_counts={"high": 1},
            category_counts={
                "secrets": {"high": 1},
                "injection": {},
                "pii": {"low": 2},
            },
        )
        assert snap.category_scores["secrets"] == 92
        assert snap.category_scores["injection"] == 100
        assert snap.category_scores["pii"] == 98

    def test_determinism(self):
        counts = {"critical": 2, "high": 5}
        s1 = compute_posture(1, finding_counts=counts)
        s2 = compute_posture(1, finding_counts=counts)
        assert s1.score == s2.score
        assert s1.level == s2.level

    def test_returns_snapshot_type(self):
        snap = compute_posture(42)
        assert isinstance(snap, PostureSnapshot)
        assert snap.project_id == 42
