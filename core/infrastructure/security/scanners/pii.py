"""SEC-006: PII Guard v1 — detect and pseudonymize/redact personally identifiable information.

Supports three modes per policy: ``pseudonymize``, ``hard_redact``, ``allow``.
Pseudonymization is deterministic within a project scope (HMAC-based).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import PrivateAttr

from core.infrastructure.security.config import SECURITY_PII_SALT
from core.infrastructure.security.models import ScanFinding, meets_severity_threshold
from core.infrastructure.security.policy import PIIConfig

_EMAIL_LOCAL_RUN = re.compile(r"[A-Za-z0-9._%+-]+")
_EMAIL_LOCAL_START = re.compile(r"(?<!\\)\b(?=[A-Za-z0-9._%+-])")
_EMAIL_DOMAIN_RUN = re.compile(r"[A-Za-z0-9.-]+")
_EMAIL_DOMAIN_SUFFIX = re.compile(r"\.[A-Za-z]{2,}\b")


def _email_spans(text: str) -> Iterator[tuple[int, int]]:
    """Find legacy email matches by scanning each delimiter-separated run once."""
    previous_end = 0
    for local in _EMAIL_LOCAL_RUN.finditer(text):
        at = local.end()
        if not text.startswith("@", at):
            continue
        start = _EMAIL_LOCAL_START.search(text, max(local.start(), previous_end), at)
        domain = _EMAIL_DOMAIN_RUN.match(text, at + 1)
        if start is None or domain is None:
            continue

        # The greedy legacy match takes the rightmost valid suffix, provided it
        # leaves at least one domain character before its dot. Include one more
        # character in the search bound so \b sees the actual right neighbor.
        address_end = None
        for suffix in _EMAIL_DOMAIN_SUFFIX.finditer(text, domain.start() + 1, domain.end() + 1):
            address_end = suffix.end()
        if address_end is not None:
            previous_end = address_end
            yield start.start(), address_end


# A phone is written with a "+" country code or with separators. A bare digit run
# (receipt 0045821, invoice 5551234567) is a document number, order ID or code, and
# pseudonymizing it destroys extracted data for good, since the token is one-way.
_SEP = r"[-.\s]"
_PHONE_END = r"(?!\w|[.-]\d|\.\.)"
_PHONE_US_COUNTRY = re.compile(
    rf"(?<![\w.+-])(?:\+1{_SEP}?|1{_SEP})(?:\(\d{{3}}\){_SEP}?|\d{{3}}{_SEP}?)\d{{3}}{_SEP}?\d{{4}}"
    + _PHONE_END
)
_PHONE_US_AREA = re.compile(
    r"(?<![\w.+-])(?:"
    rf"\(\d{{3}}\){_SEP}?\d{{3}}{_SEP}?\d{{4}}"  # (555) 123-4567
    rf"|\d{{3}}{_SEP}\d{{3}}{_SEP}?\d{{4}}"  # 555-123-4567, 555 1234567
    rf"|\d{{6}}{_SEP}\d{{4}}"  # 555123-4567
    rf"){_PHONE_END}"
)
# A local number is written 555-1234. "123.4567" is a decimal and "125 1500" two
# columns of an OCR'd table, so neither a dot nor a space makes a local phone.
_PHONE_US_LOCAL = re.compile(rf"(?<![\w.+-])\d{{3}}-\d{{4}}{_PHONE_END}")
_PHONE_INTERNATIONAL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[-.\s]?\(?\d{1,5}\)?){1,6}(?![\w+])")
# A number right after a document-number label is that document's number, unless the
# label belongs to a phone ("Phone No. 555-123-4567", "Tel. Nr. +370 612 34567").
_DOCUMENT_LABEL_BEFORE = re.compile(
    r"(?:(?<!\w)(?:nr|no|nº|num|number|ref|id)\.?|[#№])\s*[:#]?\s*$", re.IGNORECASE
)
_PHONE_WORD_BEFORE = re.compile(
    r"(?<!\w)(?:tel|telephone|phone|ph|mob|mobile|cell|fax|telefonas|telefon|telefono|tlf)"
    r"\.?\s*$",
    re.IGNORECASE,
)
_LABEL_LOOKBACK = 40


def _after_document_label(text: str, start: int) -> bool:
    before = text[max(0, start - _LABEL_LOOKBACK) : start]
    label = _DOCUMENT_LABEL_BEFORE.search(before)
    return label is not None and _PHONE_WORD_BEFORE.search(before[: label.start()]) is None


def _phone_spans(
    pattern: re.Pattern[str], *, min_digits: int = 0, max_digits: int = 15
) -> Callable[[str], Iterator[tuple[int, int]]]:
    def spans(text: str) -> Iterator[tuple[int, int]]:
        for match in pattern.finditer(text):
            digits = sum(char.isdigit() for char in match.group())
            if min_digits <= digits <= max_digits and not _after_document_label(
                text, match.start()
            ):
                yield match.span()

    return spans


# ---------------------------------------------------------------------------
# PII detector definitions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PIIDetector:
    pii_type: str
    regex: re.Pattern[str] | Callable[[str], Iterator[tuple[int, int]]]
    severity: str
    description: str
    validator: str | None = None  # optional post-match validation function name

    def spans(self, text: str) -> Iterator[tuple[int, int]]:
        if isinstance(self.regex, re.Pattern):
            yield from (match.span() for match in self.regex.finditer(text))
        else:
            yield from self.regex(text)


_DETECTORS: list[PIIDetector] = [
    PIIDetector(
        pii_type="email",
        regex=_email_spans,
        severity="medium",
        description="Email address",
    ),
    PIIDetector(
        pii_type="phone_us",
        regex=_phone_spans(_PHONE_US_COUNTRY),
        severity="medium",
        description="US phone number with country code",
    ),
    PIIDetector(
        pii_type="phone_us",
        regex=_phone_spans(_PHONE_US_AREA),
        severity="medium",
        description="US phone number with area code",
    ),
    PIIDetector(
        pii_type="phone_us",
        regex=_phone_spans(_PHONE_US_LOCAL),
        severity="medium",
        description="US local phone number",
    ),
    PIIDetector(
        pii_type="phone_international",
        regex=_phone_spans(_PHONE_INTERNATIONAL, min_digits=7),
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


class _PIIFinding(ScanFinding):
    _span: tuple[int, int] = PrivateAttr()
    _hard_redact: bool = PrivateAttr()


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

    def _hard_redact(self, pii_type: str, number: int | None = None) -> str:
        suffix = f"_{number}" if number is not None else ""
        return f"[PII:{pii_type}:REDACTED{suffix}]"

    def scan(
        self,
        text: str,
        surface: str,
        config: PIIConfig,
        **kwargs,
    ) -> list[_PIIFinding]:
        if not config.enabled or not text:
            return []

        findings: list[_PIIFinding] = []

        for detector in _DETECTORS:
            if not self._detector_enabled(config, detector.pii_type):
                continue
            if not meets_severity_threshold(detector.severity, config.severity_threshold):
                continue

            for start, end in detector.spans(text):
                matched = text[start:end]

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

                finding = _PIIFinding(
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
                finding._span = (start, end)
                finding._hard_redact = config.mode == "hard_redact"
                findings.append(finding)

        return findings

    def redact(self, text: str, findings: list[_PIIFinding]) -> str:
        """Apply redaction/pseudonymization to text based on findings."""
        ordered = sorted(
            (finding for finding in findings if finding.action == "redact"),
            key=lambda finding: (finding._span[0], -finding._span[1]),
        )
        replacements: list[tuple[_PIIFinding, int, int | None]] = []
        numbers_by_rule: dict[str, dict[str, int]] = {}
        for finding in ordered:
            start, end = finding._span
            if replacements and start < replacements[-1][1]:
                # The first span represents the union. Equal starts prefer the longest span.
                first, previous_end, number = replacements[-1]
                replacements[-1] = (first, max(previous_end, end), number)
                continue
            number = None
            if finding._hard_redact:
                numbers = numbers_by_rule.setdefault(finding.rule_id, {})
                number = numbers.setdefault(text[start:end], len(numbers) + 1)
            replacements.append((finding, end, number))

        parts: list[str] = []
        cursor = 0
        for finding, end, number in replacements:
            preview = finding.redacted_preview
            if number is not None and len(numbers_by_rule[finding.rule_id]) > 1:
                preview = self._hard_redact(finding.context["pii_type"], number)
            parts.extend((text[cursor : finding._span[0]], preview))
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts)

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
