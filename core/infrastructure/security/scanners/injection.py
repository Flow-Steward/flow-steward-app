"""SEC-005: Injection Guard v1 — context-aware injection detection.

Covers prompt override, exfiltration, SSRF, template/SQL/shell injection,
and obfuscation patterns.  Each rule is tagged with the contexts where it
applies (prompt, display, shell).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

from core.infrastructure.security.models import ScanFinding, meets_severity_threshold
from core.infrastructure.security.policy import InjectionConfig


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InjectionRule:
    rule_id: str
    pattern: re.Pattern
    contexts: list[str]
    severity: str
    confidence: float
    category: str
    description: str


_RULES: list[InjectionRule] = [
    # ---- Prompt override ----
    InjectionRule(
        rule_id="injection:prompt_override_ignore",
        pattern=re.compile(
            r"ignore\s+(?:previous|all|above|prior|earlier)\s+instructions?",
            re.IGNORECASE,
        ),
        contexts=["prompt", "display"],
        severity="high",
        confidence=0.95,
        category="prompt_override",
        description="Prompt override via 'ignore previous instructions'",
    ),
    InjectionRule(
        rule_id="injection:prompt_override_new_role",
        pattern=re.compile(
            r"you\s+are\s+now\s+(?:a|an|the)\s+",
            re.IGNORECASE,
        ),
        contexts=["prompt"],
        severity="high",
        confidence=0.9,
        category="prompt_override",
        description="Role reassignment attempt",
    ),
    InjectionRule(
        rule_id="injection:prompt_override_system",
        pattern=re.compile(
            r"(?:^|\n)\s*(?:system|assistant)\s*:\s*",
            re.IGNORECASE,
        ),
        contexts=["prompt"],
        severity="high",
        confidence=0.85,
        category="prompt_override",
        description="Fake system/assistant message injection",
    ),
    InjectionRule(
        rule_id="injection:prompt_override_disregard",
        pattern=re.compile(
            r"disregard\b.{0,80}\b(?:instructions?|rules?|guidelines?|constraints?)",
            re.IGNORECASE,
        ),
        contexts=["prompt", "display"],
        severity="high",
        confidence=0.9,
        category="prompt_override",
        description="Instruction disregard attempt",
    ),
    InjectionRule(
        rule_id="injection:prompt_override_jailbreak",
        pattern=re.compile(
            r"(?:DAN|do\s+anything\s+now|developer\s+mode|jailbreak)",
            re.IGNORECASE,
        ),
        contexts=["prompt"],
        severity="critical",
        confidence=0.85,
        category="prompt_override",
        description="Known jailbreak keyword",
    ),
    # ---- Exfiltration ----
    # A model can be told to emit an image whose URL carries data out; any
    # remote image in a prompt is suspect.
    InjectionRule(
        rule_id="injection:exfil_markdown_link",
        pattern=re.compile(
            r"!\[[^\]]*+\]\(https?://[^\s)]++\)",
        ),
        contexts=["prompt"],
        severity="high",
        confidence=0.7,
        category="exfiltration",
        description="Markdown image exfiltration (data in URL)",
    ),
    # In displayed output (step results, diagnostics) a remote image is routine
    # Markdown, and the admin's own policy loads no remote image anyway. What
    # still marks exfiltration is data carried in the URL's query.
    InjectionRule(
        rule_id="injection:exfil_markdown_link_data",
        pattern=re.compile(
            r"!\[[^\]]*+\]\(https?://[^\s)?]*+\?[^\s)=]*+=[^\s)]*+\)",
        ),
        contexts=["display"],
        severity="high",
        confidence=0.7,
        category="exfiltration",
        description="Markdown image exfiltration (data in URL query)",
    ),
    InjectionRule(
        rule_id="injection:exfil_fetch_url",
        pattern=re.compile(
            r"(?:fetch|curl|wget|requests\.get|httpx\.get|urllib)\s*\(\s*[\"']https?://",
            re.IGNORECASE,
        ),
        contexts=["prompt", "shell"],
        severity="high",
        confidence=0.8,
        category="exfiltration",
        description="URL fetch in injected context",
    ),
    # ---- SSRF ----
    InjectionRule(
        rule_id="injection:ssrf_internal",
        pattern=re.compile(
            r"https?://(?:localhost|127\.|0\.|10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.|169\.254\.|\[::1\])",
            re.IGNORECASE,
        ),
        contexts=["prompt", "shell"],
        severity="high",
        confidence=0.85,
        category="ssrf",
        description="SSRF to internal/metadata endpoint",
    ),
    # ---- Template injection ----
    InjectionRule(
        rule_id="injection:template_jinja",
        pattern=re.compile(
            r"\{\{.*?(?:config|self|request|lipsum|cycler|joiner|namespace)\b.*?\}\}"
        ),
        contexts=["prompt", "display"],
        severity="high",
        confidence=0.9,
        category="template_injection",
        description="Jinja2 SSTI payload",
    ),
    InjectionRule(
        rule_id="injection:template_generic",
        pattern=re.compile(r"\$\{[^}]*\b(?:Runtime|ProcessBuilder|exec|eval|system)\b[^}]*\}"),
        contexts=["prompt", "display"],
        severity="high",
        confidence=0.85,
        category="template_injection",
        description="Expression language injection",
    ),
    # ---- SQL injection ----
    InjectionRule(
        rule_id="injection:sql_union",
        pattern=re.compile(
            r"(?:UNION\s+(?:ALL\s+)?SELECT|;\s*(?:DROP|DELETE|INSERT|UPDATE|ALTER)\s)",
            re.IGNORECASE,
        ),
        contexts=["prompt", "display", "shell"],
        severity="high",
        confidence=0.85,
        category="sql_injection",
        description="SQL injection (UNION / stacked query)",
    ),
    InjectionRule(
        rule_id="injection:sql_comment_bypass",
        pattern=re.compile(r"(?:--\s*$|/\*.*?\*/|#\s*$)", re.MULTILINE),
        contexts=["prompt"],
        severity="medium",
        confidence=0.6,
        category="sql_injection",
        description="SQL comment-based bypass",
    ),
    # ---- Shell injection ----
    InjectionRule(
        rule_id="injection:shell_command_chain",
        pattern=re.compile(r";\s*(?:rm|cat|curl|wget|nc|ncat|bash|sh|python|perl|ruby|php)\s"),
        contexts=["shell"],
        severity="critical",
        confidence=0.9,
        category="shell_injection",
        description="Shell command chaining",
    ),
    InjectionRule(
        rule_id="injection:shell_backtick",
        pattern=re.compile(r"`[^`]{3,}`"),
        contexts=["shell"],
        severity="medium",
        confidence=0.7,
        category="shell_injection",
        description="Backtick command substitution",
    ),
    InjectionRule(
        rule_id="injection:shell_subshell",
        pattern=re.compile(r"\$\([^)]{3,}\)"),
        contexts=["shell"],
        severity="medium",
        confidence=0.7,
        category="shell_injection",
        description="Subshell command substitution",
    ),
    # ---- Obfuscation ----
    InjectionRule(
        rule_id="injection:obfuscation_base64",
        pattern=re.compile(
            r"(?:base64\s+[\-d]|atob\s*\(|btoa\s*\(|b64decode\s*\(|b64encode\s*\()",
            re.IGNORECASE,
        ),
        contexts=["prompt", "shell"],
        severity="medium",
        confidence=0.7,
        category="obfuscation",
        description="Base64 encode/decode in suspicious context",
    ),
    InjectionRule(
        rule_id="injection:obfuscation_unicode_escape",
        pattern=re.compile(r"(?:\\u[0-9a-fA-F]{4}){4,}"),
        contexts=["prompt", "display"],
        severity="medium",
        confidence=0.65,
        category="obfuscation",
        description="Suspicious Unicode escape sequence chain",
    ),
    InjectionRule(
        rule_id="injection:obfuscation_hex_escape",
        pattern=re.compile(r"(?:\\x[0-9a-fA-F]{2}){4,}"),
        contexts=["prompt", "display", "shell"],
        severity="medium",
        confidence=0.65,
        category="obfuscation",
        description="Suspicious hex escape sequence chain",
    ),
]


class InjectionGuard:
    """Context-aware injection detection engine."""

    def __init__(self, extra_rules: list[InjectionRule] | None = None):
        self._rules = list(_RULES)
        if extra_rules:
            self._rules.extend(extra_rules)

    def scan(
        self,
        text: str,
        surface: str,
        config: InjectionConfig,
        *,
        context_type: str = "prompt",
        **kwargs,
    ) -> list[ScanFinding]:
        if not config.enabled or not text:
            return []

        active_contexts = set(config.contexts)
        findings: list[ScanFinding] = []

        for rule in self._rules:
            # Skip rules not matching the active context
            if not any(ctx in active_contexts for ctx in rule.contexts):
                continue
            # Skip rules not matching the specific context_type if provided
            if context_type not in rule.contexts:
                continue
            if not meets_severity_threshold(rule.severity, config.severity_threshold):
                continue

            if rule.pattern.search(text):
                action = "block" if rule.severity in ("critical", "high") else "warn"
                normalized_severity = str(rule.severity or "medium").strip().lower()
                if normalized_severity not in {"low", "medium", "high", "critical"}:
                    normalized_severity = "medium"
                findings.append(
                    ScanFinding(
                        surface=surface,
                        severity=cast(
                            Literal["low", "medium", "high", "critical"],
                            normalized_severity,
                        ),
                        rule_id=rule.rule_id,
                        action=cast(
                            Literal["allow", "warn", "redact", "block"],
                            action,
                        ),
                        redacted_preview=f"[INJECTION:{rule.category}:BLOCKED]",
                        scanner="injection",
                        confidence=rule.confidence,
                        context={
                            "category": rule.category,
                            "context_type": context_type,
                            "description": rule.description,
                        },
                    )
                )

        return findings

    def redact(self, text: str, findings: list[ScanFinding]) -> str:
        """For injection findings, we don't surgically redact — block is the typical action.

        If redaction is needed, replace the first matched span per rule.
        """
        result = text
        for rule in self._rules:
            # Only redact rules that produced findings
            matching_ids = {f.rule_id for f in findings}
            if rule.rule_id in matching_ids:
                result = rule.pattern.sub(f"[INJECTION:{rule.category}:REDACTED]", result)
        return result
