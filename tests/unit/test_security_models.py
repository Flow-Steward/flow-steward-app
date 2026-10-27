"""SEC-003: Tests for scan result contract types.

Run:
    pytest tests/unit/test_security_models.py -v
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.infrastructure.security.models import (
    ACTION_ORDER,
    SEVERITY_ORDER,
    EnforcementDecision,
    ScanFinding,
    ScanReport,
    meets_severity_threshold,
    worst_action,
)


def _finding(**overrides) -> ScanFinding:
    defaults = {
        "surface": "inbound",
        "severity": "medium",
        "rule_id": "test:rule",
        "action": "warn",
        "scanner": "secrets",
        "redacted_preview": "[REDACTED]",
    }
    defaults.update(overrides)
    return ScanFinding(**defaults)


class TestScanFinding:
    def test_required_fields(self):
        f = _finding()
        assert f.surface == "inbound"
        assert f.severity == "medium"
        assert f.scanner == "secrets"

    def test_confidence_bounds(self):
        f = _finding(confidence=0.5)
        assert f.confidence == 0.5
        with pytest.raises(ValidationError):
            _finding(confidence=1.5)
        with pytest.raises(ValidationError):
            _finding(confidence=-0.1)

    def test_serialization_round_trip(self):
        f = _finding(context={"key": "value"})
        d = f.model_dump(mode="json")
        restored = ScanFinding(**d)
        assert restored == f

    def test_trace_id_optional(self):
        f = _finding()
        assert f.trace_id is None
        f2 = _finding(trace_id="abc-123")
        assert f2.trace_id == "abc-123"


class TestScanReport:
    def test_empty_report(self):
        r = ScanReport(surface="inbound")
        assert r.findings == []
        assert r.scan_id  # auto-generated UUID

    def test_with_findings(self):
        r = ScanReport(
            surface="outbound",
            findings=[_finding(), _finding(severity="critical")],
            policy_level="strict",
        )
        assert len(r.findings) == 2
        assert r.policy_level == "strict"

    def test_unique_scan_ids(self):
        r1 = ScanReport(surface="a")
        r2 = ScanReport(surface="b")
        assert r1.scan_id != r2.scan_id


class TestEnforcementDecision:
    def test_default_allow(self):
        d = EnforcementDecision()
        assert d.action == "allow"
        assert d.findings == []

    def test_with_redacted_text(self):
        d = EnforcementDecision(action="redact", redacted_text="safe text")
        assert d.redacted_text == "safe text"

    def test_serialization(self):
        d = EnforcementDecision(
            action="block",
            findings=[_finding(action="block", severity="critical")],
        )
        dumped = d.model_dump(mode="json")
        restored = EnforcementDecision(**dumped)
        assert restored.action == "block"
        assert len(restored.findings) == 1


class TestHelpers:
    def test_worst_action_empty(self):
        assert worst_action([]) == "allow"

    def test_worst_action_mixed(self):
        findings = [
            _finding(action="warn"),
            _finding(action="block"),
            _finding(action="redact"),
        ]
        assert worst_action(findings) == "block"

    def test_severity_threshold(self):
        assert meets_severity_threshold("critical", "low") is True
        assert meets_severity_threshold("low", "critical") is False
        assert meets_severity_threshold("medium", "medium") is True

    def test_severity_order_completeness(self):
        assert set(SEVERITY_ORDER.keys()) == {"low", "medium", "high", "critical"}

    def test_action_order_completeness(self):
        assert set(ACTION_ORDER.keys()) == {"allow", "warn", "redact", "block"}
