"""verify.py narrows a selected Tier 3 suite and records what vouches for it (ADR 0068)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

from core.application.tooling.quality.check_registry import (
    CheckDefinition,
    CheckRegistry,
    TestSelection,
)
from core.application.tooling.quality.evidence import EvidenceStore
from core.application.tooling.quality.verify import (
    SELECTION_PLAN_ENV,
    VerificationRequest,
    verify,
)

CHECK = CheckDefinition(
    id="unit-suite",
    owner_tier=3,
    command=("true",),
    inputs=("**",),
    reuse_scope="inputs",
    selection=TestSelection(kind="pytest-impact"),
)


# Modules no change here reaches keep a selection below the whole-suite share.
UNRELATED_TESTS = {f"tests/unit/test_unrelated_{index}.py": "\n" for index in range(3)}


class RecordingRunner:
    """Stands in for lint.sh: reads the plan and writes what the recorder would."""

    def __init__(self, failed: tuple[str, ...] = ()) -> None:
        self.failed = failed
        self.plans: list[dict[str, object]] = []

    def run(
        self,
        check: CheckDefinition,
        *,
        timeout_seconds: int,
        extra_env: Mapping[str, str] | None = None,
    ) -> int:
        del timeout_seconds
        assert extra_env is not None and check.id == "unit-suite"
        plan_path = Path(extra_env[SELECTION_PLAN_ENV])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.plans.append(plan)
        run = plan_path.parent / "run"
        run.mkdir(parents=True, exist_ok=True)
        contexts = {
            "tests/unit/test_library.py": ["core/library.py"],
            "tests/unit/test_other.py": ["core/other.py"],
        }
        (run / "map-main.json").write_text(
            json.dumps({"complete": True, "modules": sorted(contexts), "contexts": contexts})
        )
        status = 1 if self.failed else 0
        (run / "result.json").write_text(
            json.dumps({"exitstatus": status, "failed": list(self.failed), "recorded": True})
        )
        return status


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-c", "user.name=Quality test", "-c", "user.email=test@example.invalid", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return _git(repo, "rev-parse", "HEAD")


def _verify(repo: Path, runner: RecordingRunner, **update: object):
    request = VerificationRequest(
        tier=3,
        repository_root=repo,
        evidence_root=repo / "logs/quality/evidence",
        evidence_mode="reuse",
    ).model_copy(update=update)
    return verify(
        request,
        registry=CheckRegistry(schema_version=1, checks=(CHECK,)),
        runner=runner,
    )


def test_first_run_is_whole_then_only_reached_tests_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    first = _commit(
        repo,
        {
            ".gitignore": "logs/\n",
            "core/library.py": "def double(value):\n    return value * 2\n",
            "core/other.py": "def other():\n    return 1\n",
            "tests/unit/test_library.py": "\n",
            "tests/unit/test_other.py": "\n",
            **UNRELATED_TESTS,
        },
    )
    runner = RecordingRunner()

    first_summary = _verify(repo, runner)
    second = _commit(repo, {"core/library.py": "def double(value):\n    return value + value\n"})
    second_summary = _verify(repo, runner)

    assert first_summary.status == second_summary.status == "passed"
    assert runner.plans[0]["full"] is True
    assert runner.plans[1]["full"] is False
    assert runner.plans[1]["modules"] == ["tests/unit/test_library.py"]
    assert runner.plans[1]["base_sha"] == first
    records = {
        record.commit_sha: record
        for record in EvidenceStore(repo / "logs/quality/evidence").records("unit-suite")
    }
    assert records[first].selection_base_sha == "" and records[first].runtime_digest
    assert records[second].selection_base_sha == first
    assert records[second].selected_modules == 1
    assert (
        "full" in first_summary.selected["unit-suite"]
        or "whole" in first_summary.selected["unit-suite"]
    )


def test_a_failed_selection_records_its_failing_modules_for_the_next(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(
        repo,
        {
            ".gitignore": "logs/\n",
            "core/library.py": "def double(value):\n    return value * 2\n",
            "core/other.py": "def other():\n    return 1\n",
            "tests/unit/test_library.py": "\n",
            "tests/unit/test_other.py": "\n",
            **UNRELATED_TESTS,
        },
    )
    _verify(repo, RecordingRunner())
    failing = _commit(repo, {"core/library.py": "def double(value):\n    return value - value\n"})
    failed_summary = _verify(repo, RecordingRunner(failed=("tests/unit/test_library.py",)))
    _commit(repo, {"core/other.py": "def other():\n    return 2\n"})
    fixed = RecordingRunner()
    _verify(repo, fixed)

    assert failed_summary.status == "failed"
    failed = next(
        record
        for record in EvidenceStore(repo / "logs/quality/evidence").records("unit-suite")
        if record.commit_sha == failing
    )
    assert failed.failed_modules == ("tests/unit/test_library.py",)
    assert fixed.plans[0]["base_sha"] == failing
    assert fixed.plans[0]["modules"] == ["tests/unit/test_library.py", "tests/unit/test_other.py"]


def test_full_suite_request_ignores_an_earlier_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(
        repo,
        {
            ".gitignore": "logs/\n",
            "core/library.py": "def double(value):\n    return value * 2\n",
            "tests/unit/test_library.py": "\n",
        },
    )
    runner = RecordingRunner()
    _verify(repo, runner)
    _commit(repo, {"core/library.py": "def double(value):\n    return value + value\n"})
    _verify(repo, runner, full_suite=True)

    assert runner.plans[1]["full"] is True
    assert runner.plans[1]["reason"] == "full suite requested"
