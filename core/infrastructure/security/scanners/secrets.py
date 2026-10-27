"""SEC-004: Secret Scanner v1 — regex-based secret detection with redaction.

Detects API keys, tokens, PEM blocks, DB URIs, and other credentials
across major cloud providers and services.
"""

from __future__ import annotations

import hashlib
from typing import Literal, cast

from core.infrastructure.security.models import ScanFinding, meets_severity_threshold
from core.infrastructure.security.policy import ScannerConfig
from core.infrastructure.security.secret_detection import SECRET_PATTERNS, SecretPattern


class SecretScanner:
    """Regex-based secret detection engine."""

    def __init__(self, extra_patterns: list[SecretPattern] | None = None):
        self._patterns = list(SECRET_PATTERNS)
        if extra_patterns:
            self._patterns.extend(extra_patterns)

    def scan(
        self,
        text: str,
        surface: str,
        config: ScannerConfig,
        *,
        allowlist_hashes: set[str] | None = None,
        **kwargs,
    ) -> list[ScanFinding]:
        if not config.enabled or not text:
            return []

        findings: list[ScanFinding] = []
        seen_spans: set[tuple[int, int]] = set()

        for pat in self._patterns:
            if not meets_severity_threshold(pat.severity, config.severity_threshold):
                continue

            for match in pat.regex.finditer(text):
                span = match.span()
                # Skip overlapping matches
                if any(s <= span[0] < e or s < span[1] <= e for s, e in seen_spans):
                    continue
                seen_spans.add(span)

                matched = match.group(1) if match.lastindex else match.group(0)

                # Allowlist check
                if allowlist_hashes:
                    h = hashlib.sha256(matched.encode()).hexdigest()
                    if h in allowlist_hashes:
                        continue

                findings.append(
                    ScanFinding(
                        surface=surface,
                        severity=cast(
                            Literal["low", "medium", "high", "critical"],
                            (
                                str(pat.severity or "medium").strip().lower()
                                if str(pat.severity or "medium").strip().lower()
                                in {"low", "medium", "high", "critical"}
                                else "medium"
                            ),
                        ),
                        rule_id=f"secret:{pat.pattern_id}",
                        action="redact",
                        redacted_preview=f"[SECRET:{pat.pattern_id}:REDACTED]",
                        scanner="secrets",
                        confidence=0.9 if pat.pattern_id == "generic_api_key" else 1.0,
                        context={"description": pat.description, "match_length": len(matched)},
                    )
                )

        return findings

    def redact(self, text: str, findings: list[ScanFinding]) -> str:
        """Apply redaction by re-running patterns and replacing matches."""
        _ = findings
        result = text
        for pat in reversed(self._patterns):
            replacement = pat.replacement or f"[SECRET:{pat.pattern_id}:REDACTED]"
            result = pat.regex.sub(replacement, result)
        return result
