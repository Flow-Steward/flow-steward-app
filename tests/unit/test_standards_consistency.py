from __future__ import annotations

from pathlib import Path


def test_canonical_standards_define_delta_tiers_and_trust_boundary() -> None:
    testing = Path("docs/standards/testing-standard.md").read_text(encoding="utf-8")
    workflow = Path("docs/standards/agent-workflow-standard.md").read_text(encoding="utf-8")
    matrix = Path("docs/standards/enforcement-matrix.md").read_text(encoding="utf-8")

    assert "one owning tier" in testing
    assert "local Docker" in testing
    assert "Tier 3" in testing
    assert "delta" in testing
    assert "exact commit SHA" in workflow
    assert "Quality check ownership and evidence reuse" in matrix


def test_operator_guide_documents_evidence_states_and_diagnostic_bypass() -> None:
    guide = Path("docs/GUIDE.md").read_text(encoding="utf-8")

    for state in (
        "executed",
        "reused",
        "invalidated",
        "blocked",
        "incomplete",
        "infrastructure_failed",
    ):
        assert f"`{state}`" in guide
    assert "--evidence-mode off" in guide
