"""Shared installer and doctor severity model."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from core.application.readiness.contracts import (
    ReadinessArea,
    ReadinessFinding,
    ReadinessPrerequisiteMissing,
    ReadinessReport,
    ReadinessStatus,
)

_OPTIONAL_MARKERS = ("ai", "embedding", "llm", "model_provider", "provider_cli")
_EXTENSION_MARKERS = ("extension", "extensions")
# What is happening to the stack right now: shown by `status` whatever its verdict. An
# operator watching `config apply` with `status` saw only READY while the apply ran.
_NOTICE_CHECK_IDS = frozenset({"lifecycle_operation"})


class CoreReadinessService:
    """Normalize technical probes into the first-release readiness taxonomy."""

    def __init__(self, *, probe: Callable[[], Mapping[str, Any]]) -> None:
        self._probe = probe

    def check(self) -> ReadinessReport:
        try:
            payload = self._probe()
        except ReadinessPrerequisiteMissing as exc:
            return _prerequisite_report(exc)
        raw_checks = payload.get("checks")
        if (
            not isinstance(raw_checks, Sequence)
            or isinstance(raw_checks, (str, bytes))
            or not raw_checks
        ):
            return _invalid_contract_report("Readiness response has no valid checks list.")
        findings_list: list[ReadinessFinding] = []
        for row in raw_checks:
            try:
                finding = _normalize_finding(row)
            except ValueError as exc:
                return _invalid_contract_report(str(exc))
            if finding is not None:
                findings_list.append(finding)
        findings = tuple(findings_list)
        if not any(finding.area is ReadinessArea.CORE for finding in findings):
            return _invalid_contract_report("Readiness response contains no core check evidence.")
        core_passed = sum(
            finding.area is ReadinessArea.CORE and finding.status is ReadinessStatus.PASSED
            for finding in findings
        )
        core_failed = sum(
            finding.area is ReadinessArea.CORE and finding.status is ReadinessStatus.FAILED
            for finding in findings
        )
        optional_warnings = sum(
            finding.area is ReadinessArea.OPTIONAL and finding.status is ReadinessStatus.WARNING
            for finding in findings
        )
        recommendations = sum(finding.area is ReadinessArea.RECOMMENDATION for finding in findings)
        return ReadinessReport(
            status="blocked" if core_failed else "ready",
            findings=findings,
            core_passed=core_passed,
            core_failed=core_failed,
            optional_warnings=optional_warnings,
            recommendations=recommendations,
            process_exit_code=4 if core_failed else 0,
        )

    def status(self) -> ReadinessReport:
        """Return counts plus the blockers required to recover a non-ready stack."""
        report = self.check()
        shown = tuple(
            finding
            for finding in report.findings
            if (finding.area is ReadinessArea.CORE and finding.status is ReadinessStatus.FAILED)
            or (
                finding.check_id in _NOTICE_CHECK_IDS
                and finding.status is not ReadinessStatus.PASSED
            )
        )
        return report.model_copy(update={"findings": shown})


def _prerequisite_report(exc: ReadinessPrerequisiteMissing) -> ReadinessReport:
    """Report the missing prerequisite as a finding, which is Doctor's whole job."""
    finding = ReadinessFinding(
        check_id="readiness_prerequisite",
        area=ReadinessArea.CORE,
        title="Installation configuration",
        status=ReadinessStatus.FAILED,
        detail=exc.detail,
        remediation=exc.remediation
        or "Run the installer on this host, or point the CLI at an installed stack.",
    )
    return ReadinessReport(
        status="failed",
        findings=(finding,),
        core_failed=1,
        process_exit_code=4,
    )


def _invalid_contract_report(detail: str) -> ReadinessReport:
    finding = ReadinessFinding(
        check_id="readiness_contract",
        area=ReadinessArea.CORE,
        title="Readiness response",
        status=ReadinessStatus.FAILED,
        detail=detail,
        remediation="Retry after the application returns a complete readiness response.",
    )
    return ReadinessReport(
        status="unknown",
        findings=(finding,),
        core_failed=1,
        process_exit_code=4,
    )


def _normalize_finding(value: object) -> ReadinessFinding | None:
    if not isinstance(value, Mapping):
        raise ValueError("Readiness check must be an object.")
    check_id = str(value.get("id") or value.get("check_id") or "").strip()
    if not check_id:
        raise ValueError("Readiness check is missing its id.")
    category = str(value.get("category") or "core").strip()
    title = str(value.get("title") or check_id).strip()
    marker_text = " ".join((check_id, category, title)).lower()
    if any(marker in marker_text for marker in _EXTENSION_MARKERS):
        return None
    raw_status = str(value.get("status") or "").strip().lower()
    if raw_status not in {"pass", "warn", "fail"}:
        raise ValueError(f"Readiness check {check_id!r} has unknown status {raw_status!r}.")
    optional = any(marker in marker_text for marker in _OPTIONAL_MARKERS)
    if optional:
        area = ReadinessArea.OPTIONAL
        status = ReadinessStatus.PASSED if raw_status == "pass" else ReadinessStatus.WARNING
    elif raw_status == "pass":
        area = ReadinessArea.CORE
        status = ReadinessStatus.PASSED
    elif raw_status == "fail":
        area = ReadinessArea.CORE
        status = ReadinessStatus.FAILED
    else:
        area = ReadinessArea.RECOMMENDATION
        status = ReadinessStatus.INFO
    return ReadinessFinding(
        check_id=check_id,
        area=area,
        title=title,
        status=status,
        detail=str(value.get("detail") or ""),
        remediation=str(value.get("remediation") or ""),
    )
