from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from core.application.tooling.quality import verify as verify_module
from core.application.tooling.quality.check_registry import (
    CheckDefinition,
    CheckRegistry,
)
from core.application.tooling.quality.evidence import (
    EvidenceRecord,
    EvidenceStore,
)
from core.application.tooling.quality.fingerprint import FingerprintContext, fingerprint_check
from core.application.tooling.quality.verify import (
    SubprocessCheckRunner,
    VerificationRequest,
    main,
    verify,
)


class FakeCheckRunner:
    def __init__(self, results: dict[str, int]) -> None:
        self.results = results
        self.calls: list[str] = []

    def run(self, check: CheckDefinition, *, timeout_seconds: int) -> int:
        del timeout_seconds
        self.calls.append(check.id)
        return self.results[check.id]


def _committed_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = _repo(tmp_path)
    subprocess.run(("git", "add", "."), cwd=repo, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Quality test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "candidate",
        ),
        cwd=repo,
        check=True,
    )
    sha = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=repo, text=True).strip()
    return repo, sha


def test_tier3_defaults_to_clean_committed_head(tmp_path: Path) -> None:
    repo, sha = _committed_repo(tmp_path / "repo")
    registry = CheckRegistry(
        schema_version=1,
        checks=(
            CheckDefinition(
                id="backend-tests", owner_tier=3, command=("true",), inputs=("core/**",)
            ),
        ),
    )
    evidence_root = tmp_path / "evidence"
    summary = verify(
        VerificationRequest(tier=3, repository_root=repo, evidence_root=evidence_root),
        registry=registry,
        runner=FakeCheckRunner({"backend-tests": 0}),
    )
    expected = fingerprint_check(
        registry.checks[0], FingerprintContext(root=repo, source="commit", commit_sha=sha)
    )
    record = EvidenceStore(evidence_root).read("backend-tests", expected)
    assert summary.status == "passed"
    assert record is not None
    assert record.commit_sha == sha


@pytest.mark.parametrize("defect", ["dirty", "wrong-sha"])
def test_commit_evidence_cannot_label_a_different_executed_tree(tmp_path: Path, defect) -> None:
    repo, sha = _committed_repo(tmp_path / "repo")
    if defect == "dirty":
        (repo / "core/value.py").write_text("VALUE = 2\n")
    runner = FakeCheckRunner({"ruff": 0, "mypy": 0, "frontend-lint": 0})
    request = _request(repo, tmp_path / "evidence").model_copy(
        update={
            "commit_sha": sha if defect == "dirty" else "b" * 40,
        }
    )
    registry = _registry()
    with pytest.raises(ValueError):
        verify(request, registry=registry, runner=runner)
    assert runner.calls == []


def _registry() -> CheckRegistry:
    return CheckRegistry(
        schema_version=1,
        checks=(
            CheckDefinition(
                id="ruff",
                owner_tier=2,
                command=("ruff", "check"),
                inputs=("core/**/*.py",),
            ),
            CheckDefinition(
                id="mypy",
                owner_tier=2,
                command=("mypy", "core"),
                inputs=("core/**/*.py",),
                prerequisites=("ruff",),
            ),
            CheckDefinition(
                id="frontend-lint",
                owner_tier=2,
                command=("eslint", "."),
                inputs=("assets/frontend/**/*.ts",),
            ),
            CheckDefinition(
                id="browser",
                owner_tier=2,
                command=("playwright", "test"),
                inputs=("assets/frontend/**/*.ts",),
                execution="external",
            ),
        ),
    )


def _repo(tmp_path: Path) -> Path:
    import subprocess

    tmp_path.mkdir(parents=True)
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    (tmp_path / "core").mkdir()
    (tmp_path / "core/value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "assets/frontend").mkdir(parents=True)
    (tmp_path / "assets/frontend/view.ts").write_text("export const value = 1\n", encoding="utf-8")
    return tmp_path


def _request(
    repo: Path,
    evidence_root: Path,
    *,
    mode: str = "report",
    execution: Literal["orchestrator", "external"] = "orchestrator",
    tier: Literal[1, 2, 3] = 2,
    jobs: int = 1,
) -> VerificationRequest:
    return VerificationRequest(
        jobs=jobs,
        tier=tier,
        evidence_mode=mode,
        fingerprint_source="worktree",
        trust="local",
        commit_sha="",
        timeout_seconds=60,
        repository_root=repo,
        evidence_root=evidence_root,
        execution=execution,
    )


def test_report_only_executes_all_owned_checks_and_records_results(tmp_path: Path) -> None:
    runner = FakeCheckRunner({"ruff": 0, "mypy": 0, "frontend-lint": 0})

    summary = verify(
        _request(_repo(tmp_path / "repo"), tmp_path / "evidence"),
        registry=_registry(),
        runner=runner,
    )

    assert summary.status == "passed"
    assert summary.executed == ("ruff", "mypy", "frontend-lint")
    assert summary.reused == ()
    assert summary.would_reuse == ()


def test_external_execution_runs_only_external_owned_checks(tmp_path: Path) -> None:
    runner = FakeCheckRunner({"browser": 0})

    summary = verify(
        _request(
            _repo(tmp_path / "repo"),
            tmp_path / "evidence",
            execution="external",
        ),
        registry=_registry(),
        runner=runner,
    )

    assert summary.status == "passed"
    assert summary.executed == ("browser",)
    assert runner.calls == ["browser"]


def test_report_only_never_skips_a_valid_hit(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    evidence_root = tmp_path / "evidence"
    first_runner = FakeCheckRunner({"ruff": 0, "mypy": 0, "frontend-lint": 0})
    verify(_request(repo, evidence_root), registry=_registry(), runner=first_runner)
    second_runner = FakeCheckRunner({"ruff": 0, "mypy": 0, "frontend-lint": 0})

    summary = verify(_request(repo, evidence_root), registry=_registry(), runner=second_runner)

    assert second_runner.calls == ["ruff", "mypy", "frontend-lint"]
    assert summary.would_reuse == ("ruff", "mypy", "frontend-lint")
    assert summary.reused == ()


def test_reuse_mode_skips_valid_hits(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    evidence_root = tmp_path / "evidence"
    verify(
        _request(repo, evidence_root),
        registry=_registry(),
        runner=FakeCheckRunner({"ruff": 0, "mypy": 0, "frontend-lint": 0}),
    )
    runner = FakeCheckRunner({"ruff": 1, "mypy": 1, "frontend-lint": 1})

    summary = verify(
        _request(repo, evidence_root, mode="reuse"),
        registry=_registry(),
        runner=runner,
    )

    assert runner.calls == []
    assert summary.reused == ("ruff", "mypy", "frontend-lint")
    assert summary.status == "passed"


def test_tier2_failure_stops_before_dependent_or_independent_checks(tmp_path: Path) -> None:
    runner = FakeCheckRunner({"ruff": 1, "mypy": 0, "frontend-lint": 0})

    summary = verify(
        _request(_repo(tmp_path / "repo"), tmp_path / "evidence"),
        registry=_registry(),
        runner=runner,
    )

    assert runner.calls == ["ruff"]
    assert summary.status == "failed"
    assert summary.blocked == {}


def test_tier3_failure_runs_independent_checks_and_blocks_dependents(tmp_path: Path) -> None:
    registry = CheckRegistry(
        schema_version=1,
        checks=tuple(check.model_copy(update={"owner_tier": 3}) for check in _registry().checks),
    )
    runner = FakeCheckRunner({"ruff": 1, "mypy": 0, "frontend-lint": 0})
    repo, _sha = _committed_repo(tmp_path / "repo")

    summary = verify(
        _request(repo, tmp_path / "evidence", tier=3),
        registry=registry,
        runner=runner,
    )

    assert runner.calls == ["ruff", "frontend-lint"]
    assert summary.status == "failed"
    assert summary.blocked == {"mypy": "prerequisite_failed:ruff"}


def test_incomplete_evidence_does_not_satisfy_reuse(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    evidence_root = tmp_path / "evidence"
    registry = _registry()

    check = registry.checks[0]
    fingerprint = fingerprint_check(check, FingerprintContext(root=repo, source="worktree"))
    now = datetime.now(UTC)
    EvidenceStore(evidence_root).write(
        EvidenceRecord(
            check_id="ruff",
            owner_tier=2,
            fingerprint=fingerprint,
            status="incomplete",
            trust="local",
            command=check.command,
            started_at=now,
            completed_at=now,
            duration_seconds=0,
        )
    )
    runner = FakeCheckRunner({"ruff": 0, "mypy": 0, "frontend-lint": 0})

    summary = verify(_request(repo, evidence_root, mode="reuse"), registry=registry, runner=runner)

    assert runner.calls == ["ruff", "mypy", "frontend-lint"]
    assert summary.invalidated["ruff"] == "status:incomplete"


def test_trusted_ci_cannot_be_claimed_outside_exact_sha_github_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--tier",
                "3",
                "--trust",
                "trusted_ci",
                "--fingerprint-source",
                "commit",
                "--commit-sha",
                "a" * 40,
            ]
        )

    assert exc_info.value.code == 2


class TimelineRunner:
    """Record start/finish order; checks named in ``meet`` must overlap in time."""

    def __init__(self, results: dict[str, int], *, meet: tuple[str, ...] = ()) -> None:
        self.results = results
        self.events: list[str] = []
        self._lock = threading.Lock()
        self._meet = meet
        self._barrier = threading.Barrier(len(meet), timeout=10) if meet else None

    def run(self, check: CheckDefinition, *, timeout_seconds: int) -> int:
        del timeout_seconds
        with self._lock:
            self.events.append(f"start:{check.id}")
        result = self.results[check.id]
        if self._barrier is not None and check.id in self._meet:
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                result = 99
        with self._lock:
            self.events.append(f"end:{check.id}")
        return result


def test_parallel_jobs_run_independent_checks_at_the_same_time(tmp_path: Path) -> None:
    # The barrier only opens when ruff and frontend-lint are both in flight; a
    # serial schedule would break it and turn both results red.
    runner = TimelineRunner(
        {"ruff": 0, "mypy": 0, "frontend-lint": 0}, meet=("ruff", "frontend-lint")
    )

    summary = verify(
        _request(_repo(tmp_path / "repo"), tmp_path / "evidence", jobs=2),
        registry=_registry(),
        runner=runner,
    )

    assert summary.status == "passed"
    assert summary.executed == ("ruff", "mypy", "frontend-lint")
    assert runner.events.index("start:mypy") > runner.events.index("end:ruff")


def test_parallel_tier2_failure_lets_running_checks_finish_and_starts_nothing_new(
    tmp_path: Path,
) -> None:
    runner = TimelineRunner(
        {"ruff": 1, "mypy": 0, "frontend-lint": 0}, meet=("ruff", "frontend-lint")
    )

    summary = verify(
        _request(_repo(tmp_path / "repo"), tmp_path / "evidence", jobs=2),
        registry=_registry(),
        runner=runner,
    )

    assert summary.status == "failed"
    assert summary.executed == ("ruff", "frontend-lint")
    assert "start:mypy" not in runner.events
    assert summary.blocked == {}


def test_parallel_tier3_blocks_dependents_of_a_failed_prerequisite(tmp_path: Path) -> None:
    registry = CheckRegistry(
        schema_version=1,
        checks=tuple(check.model_copy(update={"owner_tier": 3}) for check in _registry().checks),
    )
    runner = TimelineRunner({"ruff": 1, "mypy": 0, "frontend-lint": 0})
    repo, _sha = _committed_repo(tmp_path / "repo")

    summary = verify(
        _request(repo, tmp_path / "evidence", tier=3, jobs=3),
        registry=registry,
        runner=runner,
    )

    assert summary.status == "failed"
    assert summary.executed == ("ruff", "frontend-lint")
    assert summary.blocked == {"mypy": "prerequisite_failed:ruff"}


def test_captured_check_output_is_printed_as_one_labelled_block(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    check = CheckDefinition(
        id="semgrep",
        owner_tier=2,
        command=("sh", "-c", "echo first; echo second >&2; exit 3"),
        inputs=("core/**",),
    )

    exit_code = SubprocessCheckRunner(tmp_path, capture_output=True).run(check, timeout_seconds=30)

    out = capfd.readouterr().out
    assert exit_code == 3
    assert out.index("===== semgrep =====") < out.index("first") < out.index("second")
    assert out.rstrip().endswith("===== semgrep: exit 3 =====")


@pytest.mark.parametrize(
    ("tier", "argv", "expected"), [(2, [], 4), (3, [], 1), (2, ["--jobs", "1"], 1)]
)
def test_main_picks_tier_default_jobs_unless_given(
    monkeypatch: pytest.MonkeyPatch, tier: int, argv: list[str], expected: int
) -> None:
    seen: list[tuple[int, bool]] = []

    def fake_verify(request, *, registry, runner):
        del registry
        seen.append((request.jobs, runner.capture_output))
        return verify_module.VerificationSummary(
            tier=tier, status="passed", executed=(), reused=(), would_reuse=()
        )

    monkeypatch.setattr(verify_module, "verify", fake_verify)
    monkeypatch.setattr(verify_module, "load_check_registry", lambda _path: _registry())

    assert main(["--tier", str(tier), *argv]) == 0
    assert seen == [(expected, expected > 1)]
