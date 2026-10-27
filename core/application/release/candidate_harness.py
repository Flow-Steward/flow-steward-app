"""Typed release-candidate phase execution and evidence contracts."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class FailureClass(StrEnum):
    """Stable operator-facing failure classifications."""

    INPUT = "input"
    HARNESS = "harness"
    INFRASTRUCTURE = "infrastructure"
    PRODUCT = "product"
    INCOMPLETE = "incomplete"


class PhaseStatus(StrEnum):
    """Durable phase verdict states."""

    INCOMPLETE = "incomplete"
    PASSED = "passed"
    FAILED = "failed"


class CandidateFailure(RuntimeError):
    """Base class for safe, expected candidate verdicts."""

    failure_class: FailureClass

    def __init__(self, message: str, *, log_paths: Sequence[Path] = ()) -> None:
        super().__init__(message)
        self.log_paths = tuple(log_paths)


class InputFailure(CandidateFailure):
    failure_class = FailureClass.INPUT


class HarnessFailure(CandidateFailure):
    failure_class = FailureClass.HARNESS


class InfrastructureFailure(CandidateFailure):
    failure_class = FailureClass.INFRASTRUCTURE


class ProductFailure(CandidateFailure):
    failure_class = FailureClass.PRODUCT


@dataclass(frozen=True)
class CandidateRunIdentity:
    """Immutable identity shared by every phase in one evidence run."""

    candidate_commit: str
    fingerprint: str
    compose_project: str
    run_id: str
    image_digests: Mapping[str, str]


@dataclass(frozen=True)
class PhaseEvidence:
    """One phase state transition and its retained diagnostic logs."""

    name: str
    status: PhaseStatus
    duration_seconds: float | None
    failure_class: FailureClass | None
    reason: str | None
    check_id: str | None
    log_paths: tuple[Path, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status.value,
            "duration_seconds": self.duration_seconds,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "reason": self.reason,
            "check_id": self.check_id,
            "logs": [path.name for path in self.log_paths],
        }


@dataclass(frozen=True)
class CandidateRunEvidence:
    """Evidence from one candidate run; results from other runs are never merged."""

    identity: CandidateRunIdentity
    phases: tuple[PhaseEvidence, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": "flow-steward-candidate-evidence-v1",
            "candidate_commit": self.identity.candidate_commit,
            "fingerprint": self.identity.fingerprint,
            "compose_project": self.identity.compose_project,
            "run_id": self.identity.run_id,
            "image_digests": dict(sorted(self.identity.image_digests.items())),
            "phases": [phase_evidence.to_json() for phase_evidence in self.phases],
        }


PhaseAction = Callable[[], object]


@dataclass(frozen=True)
class CandidatePhase:
    """One sequential candidate phase."""

    name: str
    action: PhaseAction
    check_id: str | None = None


class EvidenceStore(Protocol):
    def write(self, evidence: CandidateRunEvidence) -> object:
        """Persist the latest state atomically."""


class _NullEvidenceStore:
    def write(self, evidence: CandidateRunEvidence) -> None:
        del evidence


class CandidateHarness:
    """Run sequential phases with durable fail-fast transitions."""

    def __init__(
        self,
        *,
        identity: CandidateRunIdentity,
        store: EvidenceStore | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._identity = identity
        self._store = store or _NullEvidenceStore()
        self._clock = clock

    def run(self, phases: Sequence[CandidatePhase]) -> CandidateRunEvidence:
        recorded: list[PhaseEvidence] = []
        for candidate_phase in phases:
            started_at = self._clock()
            started = PhaseEvidence(
                name=candidate_phase.name,
                status=PhaseStatus.INCOMPLETE,
                duration_seconds=None,
                failure_class=FailureClass.INCOMPLETE,
                reason=None,
                check_id=candidate_phase.check_id,
            )
            recorded.append(started)
            self._persist(recorded)
            try:
                result = candidate_phase.action()
            except CandidateFailure as exc:
                recorded[-1] = PhaseEvidence(
                    name=candidate_phase.name,
                    status=PhaseStatus.FAILED,
                    duration_seconds=self._clock() - started_at,
                    failure_class=exc.failure_class,
                    reason=str(exc),
                    check_id=candidate_phase.check_id,
                    log_paths=exc.log_paths,
                )
                return self._persist(recorded)
            except BaseException as exc:
                recorded[-1] = PhaseEvidence(
                    name=candidate_phase.name,
                    status=PhaseStatus.INCOMPLETE,
                    duration_seconds=self._clock() - started_at,
                    failure_class=FailureClass.INCOMPLETE,
                    reason=type(exc).__name__,
                    check_id=candidate_phase.check_id,
                    log_paths=_exception_log_paths(exc),
                )
                self._persist(recorded)
                raise
            recorded[-1] = PhaseEvidence(
                name=candidate_phase.name,
                status=PhaseStatus.PASSED,
                duration_seconds=self._clock() - started_at,
                failure_class=None,
                reason=None,
                check_id=candidate_phase.check_id,
                log_paths=_log_paths(result),
            )
            self._persist(recorded)
        return CandidateRunEvidence(identity=self._identity, phases=tuple(recorded))

    def _persist(self, phases: Sequence[PhaseEvidence]) -> CandidateRunEvidence:
        evidence = CandidateRunEvidence(identity=self._identity, phases=tuple(phases))
        self._store.write(evidence)
        return evidence


def phase(name: str, action: PhaseAction, *, check_id: str | None = None) -> CandidatePhase:
    """Declare one sequential candidate phase."""

    if not name.strip():
        raise ValueError("candidate phase name is required")
    return CandidatePhase(name=name, action=action, check_id=check_id)


def _log_paths(result: object) -> tuple[Path, ...]:
    paths: list[Path] = []
    if isinstance(result, Path):
        paths.append(result)
    elif isinstance(result, (tuple, list)) and all(isinstance(path, Path) for path in result):
        paths.extend(result)
    return tuple(paths)


def _exception_log_paths(exc: BaseException) -> tuple[Path, ...]:
    return _log_paths(getattr(exc, "log_paths", None))
