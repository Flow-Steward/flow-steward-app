"""SEC-003: Scan result contract — ScanFinding, ScanReport, EnforcementDecision.

All scanner engines produce ``ScanFinding`` instances.  The orchestrator
aggregates them into a ``ScanReport`` and resolves a single
``EnforcementDecision`` per scan invocation.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

SEVERITY_ORDER: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}

ACTION_ORDER: dict[str, int] = {
    "allow": 0,
    "warn": 1,
    "redact": 2,
    "block": 3,
}


class ScanFinding(BaseModel):
    """A single finding from one scanner."""

    surface: str  # inbound|outbound|tool|index
    severity: Literal["low", "medium", "high", "critical"]
    rule_id: str  # e.g. "secret:aws_access_key"
    action: Literal["allow", "warn", "redact", "block"]
    redacted_preview: str = ""
    trace_id: str | None = None
    scanner: Literal["secrets", "injection", "pii"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    context: dict = Field(default_factory=dict)


class ScanReport(BaseModel):
    """Aggregated report from all scanners for one scan invocation."""

    findings: list[ScanFinding] = Field(default_factory=list)
    scan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    surface: str = "unknown"
    duration_ms: float = 0.0
    policy_level: str = "standard"


class EnforcementDecision(BaseModel):
    """Final decision: what the system should do with the scanned text."""

    action: Literal["allow", "warn", "redact", "block"] = "allow"
    findings: list[ScanFinding] = Field(default_factory=list)
    redacted_text: str | None = None
    scan_report: ScanReport | None = None


def worst_action(findings: list[ScanFinding]) -> str:
    """Return the most severe action across all findings."""
    if not findings:
        return "allow"
    return max(findings, key=lambda f: ACTION_ORDER.get(f.action, 0)).action


def meets_severity_threshold(severity: str, threshold: str) -> bool:
    """Return True if *severity* >= *threshold*."""
    return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(threshold, 0)
