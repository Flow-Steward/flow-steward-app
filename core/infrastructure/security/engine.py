"""SEC-007: Security Orchestrator Engine — unified scan_input/scan_output.

Invokes scanners in fixed order: secrets -> injection -> PII -> custom_rules.
Resolves the most severe enforcement decision across all findings.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Literal, cast

from core.infrastructure.security.models import (
    EnforcementDecision,
    ScanFinding,
    ScanReport,
    worst_action,
)
from core.infrastructure.security.policy import CustomRule, SecurityPolicy
from core.infrastructure.security.scanners.injection import InjectionGuard
from core.infrastructure.security.scanners.pii import PIIGuard
from core.infrastructure.security.scanners.secrets import SecretScanner

_engine_logger = logging.getLogger(__name__)


class SecurityOrchestrator:
    """Unified security scanning across all engines."""

    def __init__(
        self,
        policy: SecurityPolicy,
        *,
        project_id: int | str = 0,
        allowlist_hashes: set[str] | None = None,
    ):
        self._policy = policy
        self._secret_scanner = SecretScanner()
        self._injection_guard = InjectionGuard()
        self._pii_guard = PIIGuard(project_id=project_id)
        self._allowlist_hashes = allowlist_hashes
        self._custom_rules = _compile_custom_rules(policy.custom_rules)

    def scan_input(
        self,
        text: str,
        surface: str,
        *,
        context_type: str = "prompt",
    ) -> EnforcementDecision:
        return self._scan(text, surface, context_type=context_type)

    def scan_output(
        self,
        text: str,
        surface: str,
        *,
        context_type: str = "display",
    ) -> EnforcementDecision:
        return self._scan(text, surface, context_type=context_type)

    def _scan(
        self,
        text: str,
        surface: str,
        *,
        context_type: str = "prompt",
    ) -> EnforcementDecision:
        # Check if surface is set to skip
        surface_config = getattr(self._policy.surfaces, surface, None)
        if surface_config and surface_config.action == "skip":
            return EnforcementDecision(action="allow", redacted_text=text)

        if not text:
            return EnforcementDecision(action="allow", redacted_text=text)

        start = time.monotonic()
        all_findings: list[ScanFinding] = []
        redacted = text

        # 1. Secrets scanner (first — so subsequent scanners see redacted text)
        secret_findings = self._secret_scanner.scan(
            redacted,
            surface,
            self._policy.scanners.secrets,
            allowlist_hashes=self._allowlist_hashes,
        )
        if secret_findings:
            all_findings.extend(secret_findings)
            redacted = self._secret_scanner.redact(redacted, secret_findings)

        # 2. Injection guard
        injection_findings = self._injection_guard.scan(
            redacted,
            surface,
            self._policy.scanners.injection,
            context_type=context_type,
        )
        if injection_findings:
            all_findings.extend(injection_findings)
            redacted = self._injection_guard.redact(redacted, injection_findings)

        # 3. PII guard (operates on already-cleaned text)
        pii_findings = self._pii_guard.scan(
            redacted,
            surface,
            self._policy.scanners.pii,
        )
        if pii_findings:
            all_findings.extend(pii_findings)
            redacted = self._pii_guard.redact(redacted, pii_findings)

        # 4. Custom rules (project-specific patterns)
        if self._custom_rules:
            custom_findings, redacted = _run_custom_rules(
                self._custom_rules,
                redacted,
                surface,
            )
            if custom_findings:
                all_findings.extend(custom_findings)

        elapsed_ms = (time.monotonic() - start) * 1000
        action = worst_action(all_findings)
        if action not in {"allow", "warn", "redact", "block"}:
            action = "allow"

        report = ScanReport(
            findings=all_findings,
            scan_id=str(uuid.uuid4()),
            surface=surface,
            duration_ms=round(elapsed_ms, 2),
            policy_level=self._policy.level,
        )

        return EnforcementDecision(
            action=cast(Literal["allow", "warn", "redact", "block"], action),
            findings=all_findings,
            redacted_text=redacted,
            scan_report=report,
        )


# ---------------------------------------------------------------------------
# Custom rule compilation and execution
# ---------------------------------------------------------------------------
_CompiledRule = tuple[re.Pattern, CustomRule]


def _compile_custom_rules(rules: list[CustomRule]) -> list[_CompiledRule]:
    """Pre-compile custom rule regexes (invalid patterns are skipped with a warning)."""
    compiled: list[_CompiledRule] = []
    for rule in rules:
        try:
            compiled.append((re.compile(rule.pattern, re.IGNORECASE), rule))
        except re.error as e:
            _engine_logger.warning("Skipping invalid custom rule %s: %s", rule.rule_id, e)
    return compiled


def _run_custom_rules(
    compiled: list[_CompiledRule],
    text: str,
    surface: str,
) -> tuple[list[ScanFinding], str]:
    """Execute compiled custom rules against text, redact matches."""
    findings: list[ScanFinding] = []
    redacted = text
    for pattern, rule in compiled:
        for match in pattern.finditer(text):
            findings.append(
                ScanFinding(
                    surface=surface,
                    severity=rule.severity,
                    rule_id=f"custom:{rule.rule_id}",
                    action="redact",
                    redacted_preview=f"[CUSTOM:{rule.rule_id}:REDACTED]",
                    scanner=rule.scanner,
                    confidence=0.9,
                    context={"description": rule.description, "pattern": rule.pattern},
                )
            )
            redacted = redacted.replace(match.group(0), f"[CUSTOM:{rule.rule_id}:REDACTED]")
    return findings, redacted
