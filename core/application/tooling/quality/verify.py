"""Evidence-aware quality check orchestration."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.application.tooling.quality.check_registry import (
    CheckDefinition,
    CheckRegistry,
    load_check_registry,
)
from core.application.tooling.quality.evidence import (
    EvidenceExpectation,
    EvidencePolicy,
    EvidenceRecord,
    EvidenceStatus,
    EvidenceStore,
    EvidenceTrust,
    shared_evidence_root,
)
from core.application.tooling.quality.fingerprint import (
    FingerprintContext,
    FingerprintSource,
    committed_candidate_sha,
    fingerprint_check,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

EvidenceMode = Literal["off", "report", "reuse"]

# Tier 2 checks each work in their own temporary copy of the tree (frontend
# checks run `npm ci` in a mktemp workspace), so they can share the audit
# container. Tier 3 stays serial: backend-tests reads the frontend shell that
# frontend-tests publishes, and both suites are memory-heavy.
DEFAULT_JOBS_BY_TIER: dict[int, int] = {1: 1, 2: 4, 3: 1}


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: Literal[1, 2, 3]
    evidence_mode: EvidenceMode = "report"
    fingerprint_source: FingerprintSource = "worktree"
    trust: EvidenceTrust = "local"
    commit_sha: str = ""
    timeout_seconds: int = Field(default=7200, ge=1)
    repository_root: Path
    evidence_root: Path
    execution: Literal["orchestrator", "external"] = "orchestrator"
    jobs: int = Field(default=1, ge=1)
    shared_evidence_root: Path | None = None


class CheckRunner(Protocol):
    def run(self, check: CheckDefinition, *, timeout_seconds: int) -> int: ...


class SubprocessCheckRunner:
    """Run one check command; with ``capture_output`` print its log as one block.

    Concurrent checks would interleave their lines on the shared terminal, so a
    parallel run buffers each check and prints it whole when the check finishes.
    """

    def __init__(self, repository_root: Path, *, capture_output: bool = False) -> None:
        self.repository_root = repository_root
        self.capture_output = capture_output
        self._output_lock = threading.Lock()

    def run(self, check: CheckDefinition, *, timeout_seconds: int) -> int:
        if not self.capture_output:
            result = subprocess.run(
                check.command,
                cwd=self.repository_root,
                check=False,
                timeout=timeout_seconds,
            )
            return result.returncode
        try:
            captured = subprocess.run(
                check.command,
                cwd=self.repository_root,
                check=False,
                timeout=timeout_seconds,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except subprocess.TimeoutExpired as exc:
            self._emit(check.id, exc.output or b"", "timed out")
            raise
        self._emit(check.id, captured.stdout or b"", f"exit {captured.returncode}")
        return captured.returncode

    def _emit(self, check_id: str, output: bytes, outcome: str) -> None:
        with self._output_lock:
            sys.stdout.flush()
            stream = sys.stdout.buffer
            stream.write(f"\n[verify] ===== {check_id} =====\n".encode())
            stream.write(output)
            stream.write(f"[verify] ===== {check_id}: {outcome} =====\n".encode())
            stream.flush()


@dataclass(frozen=True)
class VerificationSummary:
    tier: int
    status: str
    executed: tuple[str, ...]
    reused: tuple[str, ...]
    would_reuse: tuple[str, ...]
    invalidated: dict[str, str] = field(default_factory=dict)
    blocked: dict[str, str] = field(default_factory=dict)


_Outcome = tuple[EvidenceStatus, datetime, datetime, float]


class _TierRun:
    """Schedule one tier's checks on a bounded pool.

    With ``jobs == 1`` this is the historical serial loop. A check starts only
    after every earlier prerequisite has a result; outside Tier 3 a failure
    stops new checks from starting while running ones finish.
    """

    def __init__(
        self,
        request: VerificationRequest,
        selected: tuple[CheckDefinition, ...],
        runner: CheckRunner,
    ) -> None:
        self.request = request
        self.runner = runner
        self.store = EvidenceStore(request.evidence_root, shared_root=request.shared_evidence_root)
        self.context = FingerprintContext(
            root=request.repository_root,
            source=request.fingerprint_source,
            commit_sha=request.commit_sha,
        )
        self.policy = (
            EvidencePolicy.release() if request.trust == "trusted_ci" else EvidencePolicy.local()
        )
        self.order = {check.id: index for index, check in enumerate(selected)}
        self.pending = list(selected)
        self.running: dict[Future[_Outcome], tuple[CheckDefinition, str]] = {}
        self.stop_scheduling = False
        self.known_status: dict[str, bool] = {}
        self.executed: list[str] = []
        self.reused: list[str] = []
        self.would_reuse: list[str] = []
        self.invalidated: dict[str, str] = {}
        self.blocked: dict[str, str] = {}

    def run(self) -> VerificationSummary:
        with ThreadPoolExecutor(max_workers=self.request.jobs) as pool:
            while self.pending or self.running:
                self._schedule(pool)
                if self.running:
                    self._collect()
        self.executed.sort(key=self.order.__getitem__)
        status = "passed" if all(self.known_status.values()) and not self.blocked else "failed"
        return VerificationSummary(
            tier=self.request.tier,
            status=status,
            executed=tuple(self.executed),
            reused=tuple(self.reused),
            would_reuse=tuple(self.would_reuse),
            invalidated=self.invalidated,
            blocked=self.blocked,
        )

    def _schedule(self, pool: ThreadPoolExecutor) -> None:
        for check in self.pending.copy():
            if self.stop_scheduling or len(self.running) >= self.request.jobs:
                break
            if self._awaits_prerequisite(check):
                continue
            self.pending.remove(check)
            fingerprint = self._admit(check)
            if fingerprint is not None:
                self.running[pool.submit(self._execute, check)] = (check, fingerprint)
        if self.stop_scheduling:
            self.pending.clear()

    def _awaits_prerequisite(self, check: CheckDefinition) -> bool:
        # Only an earlier check in registry order is awaited: the serial schedule
        # stays the historical one and a prerequisite cycle cannot deadlock.
        return any(
            item in self.order
            and self.order[item] < self.order[check.id]
            and item not in self.known_status
            for item in check.prerequisites
        )

    def _admit(self, check: CheckDefinition) -> str | None:
        """Return the fingerprint to execute under, or None when settled without running."""
        failed_prerequisite = next(
            (item for item in check.prerequisites if self.known_status.get(item) is False), None
        )
        if failed_prerequisite:
            self.blocked[check.id] = f"prerequisite_failed:{failed_prerequisite}"
            self.known_status[check.id] = False
            return None

        fingerprint = fingerprint_check(check, self.context)
        evidence_on = self.request.evidence_mode != "off"
        record = self.store.read(check.id, fingerprint) if evidence_on else None
        if record is None:
            if evidence_on and self.store.last_error != "not_found":
                self.invalidated[check.id] = self.store.last_error
            return fingerprint
        decision = self.policy.evaluate(
            record,
            EvidenceExpectation(
                check_id=check.id,
                owner_tier=check.owner_tier,
                fingerprint=fingerprint,
                commit_sha=self.request.commit_sha,
                reuse_scope=check.reuse_scope,
            ),
        )
        if not decision.reusable:
            self.invalidated[check.id] = decision.reason
        elif self.request.evidence_mode == "reuse":
            self.reused.append(check.id)
            self.known_status[check.id] = True
            return None
        else:
            self.would_reuse.append(check.id)
        return fingerprint

    def _execute(self, check: CheckDefinition) -> _Outcome:
        started_at = datetime.now(UTC)
        started = time.monotonic()
        status: EvidenceStatus = "incomplete"
        try:
            exit_code = self.runner.run(check, timeout_seconds=self.request.timeout_seconds)
            status = "passed" if exit_code == 0 else "failed"
        except (OSError, subprocess.SubprocessError):
            status = "infrastructure_failed"
        return status, started_at, datetime.now(UTC), time.monotonic() - started

    def _collect(self) -> None:
        done, _still_running = wait(self.running, return_when=FIRST_COMPLETED)
        for future in sorted(done, key=lambda item: self.order[self.running[item][0].id]):
            check, fingerprint = self.running.pop(future)
            self._record(check, fingerprint, future.result())

    def _record(self, check: CheckDefinition, fingerprint: str, outcome: _Outcome) -> None:
        status, started_at, completed_at, duration_seconds = outcome
        if self.request.commit_sha:
            # A tool or concurrent editor changing the tree invalidates its claimed
            # commit identity, even when the check itself returned zero.
            try:
                committed_candidate_sha(self.request.repository_root, self.request.commit_sha)
            except (OSError, ValueError):
                status = "infrastructure_failed"
        self.executed.append(check.id)
        self.known_status[check.id] = status == "passed"
        if self.request.evidence_mode != "off":
            self.store.write(
                EvidenceRecord(
                    check_id=check.id,
                    owner_tier=check.owner_tier,
                    fingerprint=fingerprint,
                    status=status,
                    trust=self.request.trust,
                    commit_sha=self.request.commit_sha,
                    command=check.command,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_seconds=duration_seconds,
                )
            )
        # Tier 3 runs every check to the end so one run reports the whole batch of
        # failures; a check whose prerequisite failed is still blocked above.
        if status != "passed" and self.request.tier != 3:
            self.stop_scheduling = True


def verify(
    request: VerificationRequest,
    *,
    registry: CheckRegistry,
    runner: CheckRunner,
) -> VerificationSummary:
    """Run tier-owned checks while reporting or applying valid evidence hits."""
    registry.validate_contract()
    if request.tier == 3 or request.commit_sha or request.fingerprint_source == "commit":
        candidate_sha = committed_candidate_sha(request.repository_root, request.commit_sha)
        request = request.model_copy(
            update={
                "commit_sha": candidate_sha,
                "fingerprint_source": "commit",
            }
        )
    selected = tuple(
        check
        for check in registry.checks
        if check.owner_tier == request.tier and check.execution == request.execution
    )
    return _TierRun(request, selected, runner).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify one canonical quality tier.")
    parser.add_argument("--tier", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--evidence-mode", choices=("off", "report", "reuse"), default="reuse")
    parser.add_argument(
        "--fingerprint-source", choices=("worktree", "index", "commit"), default="worktree"
    )
    parser.add_argument("--commit-sha", default="")
    parser.add_argument("--trust", choices=("local", "trusted_ci"), default="local")
    parser.add_argument("--repository-root", default=".")
    parser.add_argument("--evidence-root", default="logs/quality/evidence")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--execution", choices=("orchestrator", "external"), default="orchestrator")
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="Checks run at once; 0 picks the tier default (Tier 2 runs 4, others 1).",
    )
    args = parser.parse_args(argv)
    if args.trust == "trusted_ci" and not (
        _runtime_env.get("GITHUB_ACTIONS") == "true"
        and args.fingerprint_source == "commit"
        and bool(args.commit_sha)
        and args.commit_sha == _runtime_env.get("GITHUB_SHA")
    ):
        parser.error("trusted_ci requires GitHub Actions and commit source matching GITHUB_SHA")
    repository_root = Path(args.repository_root).resolve()
    jobs = args.jobs if args.jobs > 0 else DEFAULT_JOBS_BY_TIER[args.tier]
    summary = verify(
        VerificationRequest(
            tier=args.tier,
            evidence_mode=args.evidence_mode,
            fingerprint_source=args.fingerprint_source,
            trust=args.trust,
            commit_sha=args.commit_sha,
            timeout_seconds=args.timeout_seconds,
            repository_root=repository_root,
            evidence_root=(repository_root / args.evidence_root).resolve(),
            execution=args.execution,
            jobs=jobs,
            shared_evidence_root=shared_evidence_root(),
        ),
        registry=load_check_registry(repository_root / "infra/tooling/quality/check-registry.json"),
        runner=SubprocessCheckRunner(repository_root, capture_output=jobs > 1),
    )
    print(json.dumps(summary.__dict__, sort_keys=True))
    return 0 if summary.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
