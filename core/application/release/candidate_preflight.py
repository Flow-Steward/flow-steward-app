"""Ordered, non-mutating preflight checks for release candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from core.application.release.candidate_harness import (
    HarnessFailure,
    InfrastructureFailure,
    InputFailure,
)
from core.application.release.candidate_requirements import AggregatedRequirements
from core.application.release.candidate_snapshot import (
    ExpectedCandidateSnapshot,
    SnapshotComparison,
)

_FULL_SHA = re.compile(r"[0-9a-f]{40}")


class HostInspectionPort(Protocol):
    """Read-only host checks required by static preflight."""

    def current_commit(self) -> str: ...

    def tracked_tree_clean(self) -> bool: ...

    def docker_storage_readable(self) -> bool: ...


@dataclass(frozen=True)
class StaticPreflightResult:
    """Ordered static checks completed for one exact candidate."""

    candidate_commit: str
    checks: tuple[str, ...]


def run_static_preflight(
    snapshot: ExpectedCandidateSnapshot,
    *,
    requirements: AggregatedRequirements,
    host: HostInspectionPort,
    snapshot_comparison: SnapshotComparison,
) -> StaticPreflightResult:
    """Fail on the first deterministic static prerequisite mismatch."""

    checks: list[str] = []
    if not _FULL_SHA.fullmatch(snapshot.candidate_commit):
        raise InputFailure("candidate commit must be a full lowercase Git SHA")
    try:
        current_commit = host.current_commit()
        clean_tree = host.tracked_tree_clean()
    except RuntimeError as exc:
        raise InfrastructureFailure(str(exc)) from exc
    if current_commit != snapshot.candidate_commit:
        raise InputFailure("candidate commit does not match the current checkout")
    checks.append("exact-commit")
    if not clean_tree:
        raise InputFailure("tracked working tree must be clean")
    checks.append("clean-tree")

    missing = [
        name
        for name in requirements.required_env
        if snapshot.input_presence.get(name, "missing") in {"missing", "empty"}
    ]
    if missing:
        raise InputFailure("required candidate inputs are missing: " + ", ".join(sorted(missing)))
    checks.append("required-inputs")

    if not host.docker_storage_readable():
        raise InfrastructureFailure("Docker daemon or storage is not readable")
    checks.append("docker-storage")

    if not snapshot_comparison.matches:
        raise HarnessFailure(
            "candidate snapshot is stale: " + "; ".join(snapshot_comparison.reasons)
        )
    checks.append("snapshot-consistency")
    return StaticPreflightResult(
        candidate_commit=snapshot.candidate_commit,
        checks=tuple(checks),
    )
