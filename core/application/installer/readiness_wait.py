"""How the installer reads shared readiness after it has started the runtime."""

from __future__ import annotations

from collections.abc import Callable

from core.application.installer.ports import BoundedInstallerReadiness, InstallerLog
from core.application.readiness import ReadinessArea
from core.application.readiness.contracts import ReadinessFinding, ReadinessReport

RECONCILABLE_READINESS_IDS = frozenset({"security_posture", "workflow_knowledge_sync"})
READINESS_RECONCILIATION_ATTEMPTS = 30
READINESS_RECONCILIATION_DELAY_SECONDS = 2.0
READINESS_RECONCILIATION_TIMEOUT_SECONDS = 60.0
# A Compact update verified the public route and then failed shared readiness two
# seconds later, on `compact_runtime`, while a role or worker of the recreated runtime
# was still starting; a rerun a minute later passed. The public /health answers before
# every role is up, so after the route the installer waits for the runtime the same
# way it waits for the route (90 x 2 s) and fails only on what still fails at the end.
RUNTIME_STARTING_READINESS_IDS = frozenset({"compact_runtime"})
RUNTIME_READINESS_ATTEMPTS = 90
RUNTIME_READINESS_DELAY_SECONDS = 2.0


def wait_for_runtime_readiness(
    check: Callable[[], ReadinessReport],
    reprobe: BoundedInstallerReadiness | None,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    log: InstallerLog,
) -> ReadinessReport:
    """Recheck while only a still-starting runtime blocks readiness, within a bound."""
    report = check()
    if not runtime_starting(report):
        return report
    write = getattr(log, "write", None)
    window = RUNTIME_READINESS_ATTEMPTS * RUNTIME_READINESS_DELAY_SECONDS
    deadline = clock() + window
    for attempt in range(1, RUNTIME_READINESS_ATTEMPTS + 1):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        if callable(write):
            write(
                f"Waiting for shared readiness ({attempt}/{RUNTIME_READINESS_ATTEMPTS}): "
                f"{core_failures_text(report)}; retrying in "
                f"{RUNTIME_READINESS_DELAY_SECONDS:g}s."
            )
        sleep(min(RUNTIME_READINESS_DELAY_SECONDS, remaining))
        remaining = deadline - clock()
        if remaining <= 0:
            break
        report = reprobe(remaining) if reprobe is not None else check()
        if not runtime_starting(report):
            if callable(write):
                write(f"Shared readiness: the runtime finished starting after {attempt} rechecks.")
            return report
    if callable(write):
        write(
            f"Shared readiness still failing after waiting {window:g}s for the runtime: "
            f"{core_failures_text(report)}."
        )
    return report


def core_failed_findings(report: ReadinessReport) -> tuple[ReadinessFinding, ...]:
    return tuple(
        finding
        for finding in report.findings
        if finding.area is ReadinessArea.CORE and finding.status.value == "failed"
    )


def core_failed_ids(report: ReadinessReport) -> set[str]:
    return {finding.check_id for finding in core_failed_findings(report)}


def runtime_starting(report: ReadinessReport) -> bool:
    """Whether a starting runtime is among the failures and nothing else blocks.

    Derived capabilities that the installer reconciles may fail alongside it; they are
    reconciled once the runtime is up. Any other core failure fails at once, as before.
    """
    failed_ids = core_failed_ids(report)
    return bool(failed_ids & RUNTIME_STARTING_READINESS_IDS) and failed_ids.issubset(
        RUNTIME_STARTING_READINESS_IDS | RECONCILABLE_READINESS_IDS
    )


def core_failures_text(report: ReadinessReport) -> str:
    """Each failed core check by title and id, with the last reason it gave."""
    parts = []
    for finding in core_failed_findings(report):
        title = finding.title.strip()
        name = (
            f"{title} ({finding.check_id})"
            if title and title != finding.check_id
            else finding.check_id
        )
        detail = finding.detail.strip()
        parts.append(f"{name}: {detail}" if detail else name)
    return "; ".join(parts) or f"{report.core_failed} core check(s)"


def warning_text(finding: ReadinessFinding) -> str:
    title = finding.title.replace("_", " ").strip()
    title = title[:1].upper() + title[1:]
    text = " ".join(part.strip() for part in (finding.detail, finding.remediation) if part.strip())
    return f"{title}: {text}" if text else f"{title}."
