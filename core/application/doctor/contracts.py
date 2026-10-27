"""What a doctor check finds, and what a repair did.

Principle 0 of the task (the owner's decision): nothing is trusted from logs, journals
or an earlier run. Every finding comes from the state as it is now, and a repair is
decided by a diagnosis made in the same run and checked again just before and after it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

FindingStatus = Literal["ok", "fail", "warn", "info"]
RepairOutcomeKind = Literal["repaired", "already_fine", "still_failing", "refused"]


class CheckBlocked(Exception):
    """A check that cannot look while something it reads is down -- a stopped runtime or
    database. Not a failure of its own: the finding says what to fix first."""

    def __init__(self, reason: str, next_step: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.next_step = next_step


@dataclass(frozen=True)
class DoctorFinding:
    check_id: str
    status: FindingStatus
    cause: str = ""
    next_step: str = ""  # a command, not "inspect"
    repairable: bool = False  # doctor --fix can repair it

    @property
    def needs_attention(self) -> bool:
        return self.status in {"fail", "warn"}


@dataclass(frozen=True)
class DoctorCheck:
    """One stable check. The id is what an operator passes to `doctor --fix <id>`."""

    check_id: str
    title: str
    diagnose: Callable[[Any], DoctorFinding]
    # A safe, idempotent repair; returns what it did. None: the finding is only a hint.
    repair: Callable[[Any, DoctorFinding], str] | None = None
    # Where the repair is not available and why (a provider without Compose).
    unavailable: Callable[[Any], str] | None = None
    profiles: frozenset[str] = frozenset({"compact", "scalable"})


@dataclass(frozen=True)
class DoctorReport:
    findings: tuple[DoctorFinding, ...]

    @property
    def failing(self) -> tuple[DoctorFinding, ...]:
        return tuple(finding for finding in self.findings if finding.needs_attention)

    @property
    def clean(self) -> bool:
        return not any(finding.status == "fail" for finding in self.findings)


@dataclass(frozen=True)
class RepairOutcome:
    check_id: str
    kind: RepairOutcomeKind
    detail: str = ""


@dataclass
class FixResult:
    plan: tuple[DoctorFinding, ...]
    outcomes: list[RepairOutcome] = field(default_factory=list)
    confirmed: bool = False
    final: DoctorReport | None = None
