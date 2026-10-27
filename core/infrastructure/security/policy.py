"""SEC-002: Security policy schema — Pydantic models for global + project policies.

Supports three levels: ``minimal``, ``standard``, ``strict``.
Per-surface action matrix controls which boundaries are scanned.
Project overrides merge on top of the global policy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictPolicyModel(BaseModel):
    """Closed policy DTO: request/storage payloads cannot carry undeclared keys."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Scanner-level config
# ---------------------------------------------------------------------------
class ScannerConfig(_StrictPolicyModel):
    enabled: bool = True
    severity_threshold: Literal["low", "medium", "high", "critical"] = "low"
    allowlist: list[str] = Field(default_factory=list)


class InjectionConfig(ScannerConfig):
    contexts: list[str] = Field(default_factory=lambda: ["prompt", "display", "shell"])


class PIIDetectorToggle(_StrictPolicyModel):
    enabled: bool = True


class PIIDetectorSettings(_StrictPolicyModel):
    person_name: PIIDetectorToggle = Field(default_factory=PIIDetectorToggle)
    email: PIIDetectorToggle = Field(default_factory=PIIDetectorToggle)
    phone: PIIDetectorToggle = Field(default_factory=PIIDetectorToggle)
    ssn: PIIDetectorToggle = Field(default_factory=PIIDetectorToggle)
    credit_card: PIIDetectorToggle = Field(default_factory=PIIDetectorToggle)
    ipv4: PIIDetectorToggle = Field(default_factory=PIIDetectorToggle)
    ipv6: PIIDetectorToggle = Field(default_factory=PIIDetectorToggle)


class PIIConfig(ScannerConfig):
    mode: Literal["pseudonymize", "hard_redact", "allow"] = "pseudonymize"
    detectors: PIIDetectorSettings = Field(default_factory=PIIDetectorSettings)


# ---------------------------------------------------------------------------
# Surface-level config
# ---------------------------------------------------------------------------
class SurfaceConfig(_StrictPolicyModel):
    action: Literal["scan", "skip"] = "scan"


class SurfaceSettings(_StrictPolicyModel):
    inbound: SurfaceConfig = Field(default_factory=SurfaceConfig)
    outbound: SurfaceConfig = Field(default_factory=SurfaceConfig)
    tool: SurfaceConfig = Field(default_factory=SurfaceConfig)


# ---------------------------------------------------------------------------
# Scanner aggregate
# ---------------------------------------------------------------------------
class ScannerSettings(_StrictPolicyModel):
    secrets: ScannerConfig = Field(default_factory=ScannerConfig)
    injection: InjectionConfig = Field(default_factory=InjectionConfig)
    pii: PIIConfig = Field(default_factory=PIIConfig)


# ---------------------------------------------------------------------------
# Custom rules
# ---------------------------------------------------------------------------
class CustomRule(_StrictPolicyModel):
    rule_id: str
    scanner: Literal["secrets", "injection", "pii"]
    pattern: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    description: str = ""


# ---------------------------------------------------------------------------
# Top-level policy
# ---------------------------------------------------------------------------
class SecurityPolicy(_StrictPolicyModel):
    enabled: bool = True
    level: Literal["minimal", "standard", "strict"] = "standard"
    scanners: ScannerSettings = Field(default_factory=ScannerSettings)
    surfaces: SurfaceSettings = Field(default_factory=SurfaceSettings)
    custom_rules: list[CustomRule] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
MINIMAL_OVERRIDES: dict = {
    "scanners": {
        "secrets": {"severity_threshold": "high"},
        "injection": {"severity_threshold": "high", "contexts": ["prompt"]},
        "pii": {"enabled": False},
    },
}

STRICT_OVERRIDES: dict = {
    "scanners": {
        "secrets": {"severity_threshold": "low"},
        "injection": {"severity_threshold": "low", "contexts": ["prompt", "display", "shell"]},
        "pii": {"severity_threshold": "low", "mode": "hard_redact"},
    },
}
