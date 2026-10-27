"""R156 follow-up: `status` during `config apply` must say that an apply is running.

The auditor ran `status` six seconds into an apply and saw only "READY" and the counts.
The W1 finding was there -- "config apply ... is running (step ...)" -- but `status`
keeps only blockers, and a core warning becomes a recommendation, so it was dropped.
"""

from __future__ import annotations

from core.application.readiness import CoreReadinessService
from core.entrypoints.cli.platform import renderers

_RUNNING = {
    "id": "lifecycle_operation",
    "category": "core",
    "title": "Lifecycle operation in progress",
    "status": "warn",
    "detail": "config apply c15d47fe is running (step verify_health).",
    "remediation": "Let it finish.",
}
_OTHER_ADVICE = {
    "id": "tls_hint",
    "category": "core",
    "title": "Consider a certificate",
    "status": "warn",
    "detail": "",
}
_PASS = {"id": "database", "category": "core", "title": "Database", "status": "pass"}


def _status(*checks):
    return CoreReadinessService(probe=lambda: {"checks": [_PASS, *checks]}).status()


def test_status_keeps_a_running_lifecycle_operation_and_only_that_advice() -> None:
    report = _status(_RUNNING, _OTHER_ADVICE)

    assert report.status == "ready"
    assert [finding.check_id for finding in report.findings] == ["lifecycle_operation"]


def test_a_quiet_stack_still_reports_no_findings() -> None:
    assert _status(_OTHER_ADVICE).findings == ()  # the green direction


def test_the_status_line_names_the_running_operation(capsys) -> None:
    payload = _status(_RUNNING).model_dump(mode="json")

    renderers._render_status_plain(payload)

    out = capsys.readouterr().out
    assert "Status: ready" in out
    assert "config apply c15d47fe is running (step verify_health)." in out
