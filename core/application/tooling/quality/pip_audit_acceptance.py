"""Validate pip-audit findings against scoped, expiring risk acceptances."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

_SECTION_PATTERN = re.compile(
    r"^###\s+(?P<package>[^/\n]+?)\s*/\s*(?P<advisories>[^\n]+)\s*$"
    r"(?P<body>.*?)(?=^###\s+|^##\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)
_ADVISORY_PATTERN = re.compile(r"\b(?:CVE|GHSA|PYSEC)-[a-z0-9-]+\b", re.IGNORECASE)


class AcceptanceFormatError(ValueError):
    """Raised when an acceptance section cannot be enforced safely."""


@dataclass(frozen=True)
class Finding:
    label: str
    package: str
    advisory: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (
            self.label.casefold(),
            _canonical_package(self.package),
            self.advisory.casefold(),
        )


def _canonical_package(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip()).casefold()


def _metadata(body: str, field: str) -> str | None:
    match = re.search(
        rf"^-\s*{re.escape(field)}:\s*(?P<value>.+?)\s*$",
        body,
        re.IGNORECASE | re.MULTILINE,
    )
    return match.group("value").strip() if match else None


def acceptance_evaluation_date() -> date:
    """Use one UTC calendar boundary for acceptance evaluation and evidence reuse."""
    return datetime.now(UTC).date()


def load_acceptances(path: Path, *, today: date | None = None) -> set[tuple[str, str, str]]:
    """Load non-expired acceptance keys from the documented Markdown sections."""
    if not path.exists():
        return set()

    accepted_on = today if today is not None else acceptance_evaluation_date()
    text = path.read_text(encoding="utf-8")
    accepted: set[tuple[str, str, str]] = set()

    for section in _SECTION_PATTERN.finditer(text):
        package = section.group("package").strip()
        advisories = _ADVISORY_PATTERN.findall(section.group("advisories"))
        if not advisories:
            raise AcceptanceFormatError(
                f"acceptance for {package!r} has no supported advisory identifiers"
            )

        label = _metadata(section.group("body"), "Audit label")
        expires_text = _metadata(section.group("body"), "Accepted until")
        if not label or not expires_text:
            raise AcceptanceFormatError(
                f"acceptance for {package!r} requires Audit label and Accepted until"
            )
        try:
            expires = date.fromisoformat(expires_text)
        except ValueError as exc:
            raise AcceptanceFormatError(
                f"acceptance for {package!r} has invalid expiry {expires_text!r}"
            ) from exc

        if accepted_on > expires:
            continue
        for advisory in advisories:
            accepted.add((label.casefold(), _canonical_package(package), advisory.casefold()))

    return accepted


def load_findings(report_specs: Iterable[str]) -> list[Finding]:
    findings: list[Finding] = []
    for report_spec in report_specs:
        label, separator, report_value = report_spec.partition(":")
        if not separator or not label or not report_value:
            raise ValueError(f"invalid report specification: {report_spec!r}")
        report_path = Path(report_value)
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"pip-audit ({label}): failed to parse JSON output: {exc}") from exc

        for dependency in payload.get("dependencies", []):
            package = str(dependency.get("name") or "").strip()
            if not package:
                continue
            for vulnerability in dependency.get("vulns", []) or []:
                advisory = str(vulnerability.get("id") or "").strip()
                if advisory:
                    findings.append(Finding(label, package, advisory))
    return findings


def evaluate(
    acceptance_path: Path,
    report_specs: Iterable[str],
    *,
    today: date | None = None,
) -> tuple[list[Finding], list[Finding]]:
    accepted_keys = load_acceptances(acceptance_path, today=today)
    accepted: list[Finding] = []
    unaccepted: list[Finding] = []
    for finding in load_findings(report_specs):
        (accepted if finding.key in accepted_keys else unaccepted).append(finding)
    return accepted, unaccepted


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) < 2:
        print(
            "usage: pip_audit_acceptance ACCEPTANCE_FILE LABEL:REPORT [...]",
            file=sys.stderr,
        )
        return 2

    acceptance_path = Path(args[0])
    try:
        accepted, unaccepted = evaluate(acceptance_path, args[1:])
    except ValueError as exc:
        print(f"[lint] pip-audit: {exc}", file=sys.stderr)
        return 1

    if not accepted and not unaccepted:
        print("[lint] pip-audit: no known vulnerabilities found.")
        return 0
    if unaccepted:
        print("[lint] pip-audit: unaccepted vulnerabilities detected:")
        for finding in unaccepted:
            print(f"  - [{finding.label}] {finding.package}: {finding.advisory}")
        if acceptance_path.exists():
            print(f"[lint] Add risk acceptance entries to {acceptance_path}.")
        else:
            print(f"[lint] Acceptance file not found: {acceptance_path}")
        return 1

    print("[lint] pip-audit: all vulnerabilities are covered by risk acceptance:")
    for finding in accepted:
        print(f"  - [{finding.label}] {finding.package}: {finding.advisory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
