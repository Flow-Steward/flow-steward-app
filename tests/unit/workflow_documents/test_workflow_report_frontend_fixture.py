"""The renderer's fixture is the frozen schema fixture, byte for byte.

Frontend tests run in a workspace that holds only ``assets/``, so the renderer
keeps its own copy of the snapshot; this test keeps the two from drifting.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_frontend_report_fixture_matches_the_schema_fixture() -> None:
    frozen = ROOT / "tests" / "fixtures" / "workflow_reports" / "sales_report_snapshot.json"
    copy = (
        ROOT
        / "assets"
        / "frontend"
        / "src"
        / "components"
        / "report"
        / "__fixtures__"
        / "sales-report-snapshot.json"
    )
    assert copy.read_bytes() == frozen.read_bytes()
