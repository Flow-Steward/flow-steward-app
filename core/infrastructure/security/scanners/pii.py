"""SEC-006: PII Guard v1 — detect and pseudonymize/redact personally identifiable information.

Supports three modes per policy: ``pseudonymize``, ``hard_redact``, ``allow``.
Pseudonymization is deterministic within a project scope (HMAC-based).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Literal, cast

from core.infrastructure.security.config import SECURITY_PII_SALT
from core.infrastructure.security.models import ScanFinding, meets_severity_threshold
from core.infrastructure.security.policy import PIIConfig


# ---------------------------------------------------------------------------
# PII detector definitions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PIIDetector:
    pii_type: str
    regex: re.Pattern
    severity: str
    description: str
    validator: str | None = None  # optional post-match validation function name


_DETECTORS: list[PIIDetector] = [
    PIIDetector(
        pii_type="email",
        regex=re.compile(r"(?<!\\)\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        severity="medium",
        description="Email address",
    ),
    PIIDetector(
        pii_type="phone_us",
        regex=re.compile(
            r"(?<![\w.])\+?1[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\w|\.\d|\.\.)"
        ),
        severity="medium",
        description="US phone number with country code",
    ),
    PIIDetector(
        pii_type="phone_us",
        regex=re.compile(r"(?<![\w.])\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\w|\.\d|\.\.)"),
        severity="medium",
        description="US phone number with area code",
    ),
    PIIDetector(
        pii_type="phone_us",
        regex=re.compile(r"(?<![\w.-])\d{3}[-.\s]?\d{4}(?!\w|\.\d|\.\.)"),
        severity="medium",
        description="US local phone number",
    ),
    PIIDetector(
        pii_type="phone_international",
        regex=re.compile(r"\+\d{1,3}[-.\s]?\d{4,14}"),
        severity="medium",
        description="International phone number",
    ),
    PIIDetector(
        pii_type="ssn",
        regex=re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        severity="critical",
        description="US Social Security Number",
    ),
    PIIDetector(
        pii_type="credit_card",
        regex=re.compile(r"(?<![0-9A-Fa-f-])(?:\d{4}[-\s]?){3}\d{4}(?![0-9A-Fa-f-])"),
        severity="critical",
        description="Credit card number",
        validator="luhn",
    ),
    PIIDetector(
        pii_type="ipv4",
        regex=re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        severity="low",
        description="IPv4 address",
        validator="ipv4",
    ),
    PIIDetector(
        pii_type="ipv6",
        regex=re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"),
        severity="low",
        description="IPv6 address",
    ),
    # Contextual personal name detectors (heuristic — require label prefix)
    PIIDetector(
        pii_type="person_name",
        regex=re.compile(
            r"(?i:(?:full\s*name|name|contact|author|assignee|owner|reporter|user)"
            r"\s*[:=]\s*)"
            r"([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){1,3})"
        ),
        severity="medium",
        description="Personal name (contextual label match)",
        validator="person_name_context",
    ),
    PIIDetector(
        pii_type="person_name",
        regex=re.compile(
            r"\b(?:Mr|Mrs|Ms|Miss|Dr|Prof)\.?\s+"
            r"([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){0,2})\b"
        ),
        severity="medium",
        description="Personal name (title prefix match)",
        validator="person_name_title",
    ),
]


# ---------------------------------------------------------------------------
# Luhn check for credit cards
# ---------------------------------------------------------------------------
def _luhn_valid(number_str: str) -> bool:
    """Validate a credit card number using the Luhn algorithm."""
    digits = [int(d) for d in re.sub(r"[-\s]", "", number_str) if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Common words that look like names but aren't (reduce false positives)
_NAME_FALSE_POSITIVES = frozenset(
    {
        "None",
        "True",
        "False",
        "Null",
        "Default",
        "Unknown",
        "System",
        "Admin",
        "Test",
        "User",
        "Guest",
        "Anonymous",
        "Service",
        "Agent",
        "Server",
        "Client",
        "Error",
        "Warning",
        "Info",
        "Debug",
        "Status",
        "Config",
        "Project",
        "Task",
        "Build",
        "Deploy",
        "Release",
        "Version",
        "Update",
    }
)


def _person_name_context_valid(full_match: str) -> bool:
    """Validate contextual name match — the regex group(0) includes the label prefix."""
    # Extract just the name portion (after the label:)
    parts = re.split(r"[:=]\s*", full_match, maxsplit=1)
    name = parts[-1].strip() if len(parts) > 1 else full_match.strip()
    words = name.split()
    if len(words) < 2:
        return False
    return not any(w in _NAME_FALSE_POSITIVES for w in words)


def _person_name_title_valid(full_match: str) -> bool:
    """Validate title-prefix name match (Mr./Dr./etc.)."""
    # Must have at least one name word after the title
    parts = full_match.split()
    return len(parts) >= 2


def _ipv4_valid(full_match: str) -> bool:
    parts = full_match.split(".")
    return (
        len(parts) == 4
        and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)
        and int(parts[0]) not in {0, 127}
    )


_VALIDATORS = {
    "luhn": _luhn_valid,
    "ipv4": _ipv4_valid,
    "person_name_context": _person_name_context_valid,
    "person_name_title": _person_name_title_valid,
}

_DETECTOR_POLICY_KEY = {
    "person_name": "person_name",
    "email": "email",
    "phone_us": "phone",
    "phone_international": "phone",
    "ssn": "ssn",
    "credit_card": "credit_card",
    "ipv4": "ipv4",
    "ipv6": "ipv6",
}


class PIIGuard:
    """PII detection engine with pseudonymization and hard-redaction modes."""

    def __init__(self, project_id: int | str = 0):
        self._project_id = str(project_id)
        self._hmac_key = hmac.new(
            SECURITY_PII_SALT.encode(),
            self._project_id.encode(),
            hashlib.sha256,
        ).digest()

    def _pseudonymize(self, pii_type: str, value: str) -> str:
        """Deterministic pseudonym: same value in same project = same token."""
        token = hmac.new(self._hmac_key, value.encode(), hashlib.sha256).hexdigest()[:12]
        return f"[PII:{pii_type}:{token}]"

    def _hard_redact(self, pii_type: str) -> str:
        return f"[PII:{pii_type}:REDACTED]"

    def scan(
        self,
        text: str,
        surface: str,
        config: PIIConfig,
        **kwargs,
    ) -> list[ScanFinding]:
        if not config.enabled or not text:
            return []

        findings: list[ScanFinding] = []

        for detector in _DETECTORS:
            if not self._detector_enabled(config, detector.pii_type):
                continue
            if not meets_severity_threshold(detector.severity, config.severity_threshold):
                continue

            for match in detector.regex.finditer(text):
                matched = match.group(0)

                # Post-match validation
                if detector.validator:
                    vfn = _VALIDATORS.get(detector.validator)
                    if vfn and not vfn(matched):
                        continue

                # Determine redacted preview based on mode
                if config.mode == "pseudonymize":
                    preview = self._pseudonymize(detector.pii_type, matched)
                elif config.mode == "hard_redact":
                    preview = self._hard_redact(detector.pii_type)
                else:
                    preview = matched  # allow mode

                action = "allow" if config.mode == "allow" else "redact"
                normalized_severity = str(detector.severity or "medium").strip().lower()
                if normalized_severity not in {"low", "medium", "high", "critical"}:
                    normalized_severity = "medium"

                findings.append(
                    ScanFinding(
                        surface=surface,
                        severity=cast(
                            Literal["low", "medium", "high", "critical"],
                            normalized_severity,
                        ),
                        rule_id=f"pii:{detector.pii_type}",
                        action=cast(
                            Literal["allow", "warn", "redact", "block"],
                            action,
                        ),
                        redacted_preview=preview,
                        scanner="pii",
                        confidence=0.85,
                        context={
                            "pii_type": detector.pii_type,
                            "description": detector.description,
                        },
                    )
                )

        return findings

    def redact(self, text: str, findings: list[ScanFinding]) -> str:
        """Apply redaction/pseudonymization to text based on findings."""
        result = text
        # Process detectors in reverse order of match position to avoid offset issues
        replacements: list[tuple[int, int, str]] = []

        for detector in _DETECTORS:
            matching = [f for f in findings if f.rule_id == f"pii:{detector.pii_type}"]
            if not matching:
                continue

            for match in detector.regex.finditer(text):
                matched = match.group(0)
                if detector.validator:
                    vfn = _VALIDATORS.get(detector.validator)
                    if vfn and not vfn(matched):
                        continue

                # Find the corresponding finding's preview
                preview = matching[0].redacted_preview
                replacements.append((match.start(), match.end(), preview))

        # Apply replacements from end to start
        for start, end, replacement in sorted(replacements, reverse=True):
            result = result[:start] + replacement + result[end:]

        return result

    @staticmethod
    def _detector_enabled(config: PIIConfig, pii_type: str) -> bool:
        detectors_cfg = getattr(config, "detectors", None)
        if detectors_cfg is None:
            return True
        key = _DETECTOR_POLICY_KEY.get(str(pii_type or "").strip(), "")
        if not key:
            return True
        toggle = getattr(detectors_cfg, key, None)
        if toggle is None:
            return True
        return bool(getattr(toggle, "enabled", True))
