"""Expiry-sensitive pip-audit evidence reuse for one immutable candidate."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import ClassVar, Literal

from core.application.tooling.quality import pip_audit_acceptance
from core.application.tooling.quality.check_registry import load_check_registry
from core.application.tooling.quality.release_gate import run_release_gate
from core.application.tooling.quality.verify import VerificationRequest, verify


class ControlledDate(date):
    current: ClassVar[date] = date(2026, 10, 14)

    @classmethod
    def today(cls) -> date:
        return cls.current


class AcceptanceRunner:
    def __init__(self, acceptance: Path, report: Path) -> None:
        self.acceptance = acceptance
        self.report = report
        self.calls: list[str] = []

    def run(self, check, *, timeout_seconds: int) -> int:
        del timeout_seconds
        self.calls.append(check.id)
        if check.id != "pip-audit":
            return 0
        _accepted, unaccepted = pip_audit_acceptance.evaluate(
            self.acceptance,
            [f"semgrep:{self.report}"],
        )
        return 1 if unaccepted else 0


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _candidate(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    repo = tmp_path / "candidate"
    acceptance = repo / "infra/tooling/quality/pip-audit-risk-acceptance.md"
    report = repo / "pip-audit-semgrep.json"
    evaluator = repo / "core/application/tooling/quality/pip_audit_acceptance.py"
    registry_path = repo / "infra/tooling/quality/check-registry.json"
    acceptance.parent.mkdir(parents=True)
    evaluator.parent.mkdir(parents=True)
    (repo / "core/value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "requirements.lock").write_text("PyJWT==2.15.1\n", encoding="utf-8")
    acceptance.write_text(
        """# pip-audit Risk Acceptance

## Accepted Vulnerabilities

### pyjwt / GHSA-example-1234

- Audit label: semgrep
- Accepted until: 2026-10-14
- Scope: isolated Semgrep audit-tool environment only
""",
        encoding="utf-8",
    )
    report.write_text(
        json.dumps({"dependencies": [{"name": "PyJWT", "vulns": [{"id": "GHSA-example-1234"}]}]}),
        encoding="utf-8",
    )
    evaluator.write_text("# fingerprinted evaluator fixture\n", encoding="utf-8")
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checks": [
                    {
                        "id": "ordinary",
                        "owner_tier": 2,
                        "command": ["true"],
                        "inputs": ["core/value.py"],
                    },
                    {
                        "id": "pip-audit",
                        "owner_tier": 2,
                        "command": ["pip-audit"],
                        "inputs": [
                            "requirements.lock",
                            "infra/tooling/quality/pip-audit-risk-acceptance.md",
                            "core/application/tooling/quality/pip_audit_acceptance.py",
                        ],
                    },
                    {
                        "id": "backend-tests",
                        "owner_tier": 3,
                        "command": ["pytest"],
                        "inputs": ["core/value.py"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.name", "Quality Test")
    _run_git(repo, "config", "user.email", "quality@example.invalid")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-qm", "immutable candidate")
    return repo, _run_git(repo, "rev-parse", "HEAD"), acceptance, report


def _request(
    repo: Path,
    sha: str,
    evidence_root: Path,
    *,
    tier: Literal[2, 3],
    mode: Literal["report", "reuse"],
) -> VerificationRequest:
    return VerificationRequest(
        tier=tier,
        evidence_mode=mode,
        fingerprint_source="commit",
        commit_sha=sha,
        repository_root=repo,
        evidence_root=evidence_root,
    )


def test_expired_acceptance_reexecutes_real_evaluator_for_same_sha(
    tmp_path: Path, monkeypatch
) -> None:
    repo, sha, acceptance, report = _candidate(tmp_path)
    evidence_root = tmp_path / "evidence"
    registry = load_check_registry(repo / "infra/tooling/quality/check-registry.json")
    monkeypatch.setattr(pip_audit_acceptance, "acceptance_evaluation_date", ControlledDate.today)

    monkeypatch.setattr(ControlledDate, "current", date(2026, 10, 14))
    initial = AcceptanceRunner(acceptance, report)
    first = verify(
        _request(repo, sha, evidence_root, tier=2, mode="report"),
        registry=registry,
        runner=initial,
    )
    assert first.status == "passed"
    assert initial.calls == ["ordinary", "pip-audit"]

    same_day_runner = AcceptanceRunner(acceptance, report)
    same_day = verify(
        _request(repo, sha, evidence_root, tier=2, mode="reuse"),
        registry=registry,
        runner=same_day_runner,
    )
    assert same_day.status == "passed"
    assert same_day.reused == ("ordinary", "pip-audit")
    assert same_day.executed == ()
    assert same_day_runner.calls == []

    monkeypatch.setattr(ControlledDate, "current", date(2026, 10, 15))
    expired = AcceptanceRunner(acceptance, report)
    second = verify(
        _request(repo, sha, evidence_root, tier=2, mode="reuse"),
        registry=registry,
        runner=expired,
    )

    assert second.status == "failed"
    assert second.reused == ("ordinary",)
    assert second.executed == ("pip-audit",)
    assert expired.calls == ["pip-audit"]


def test_release_gate_rejects_pre_expiry_pip_audit_evidence_for_same_sha(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo, sha, acceptance, report = _candidate(tmp_path)
    evidence_root = tmp_path / "evidence"
    registry = load_check_registry(repo / "infra/tooling/quality/check-registry.json")
    monkeypatch.setattr(pip_audit_acceptance, "acceptance_evaluation_date", ControlledDate.today)

    monkeypatch.setattr(ControlledDate, "current", date(2026, 10, 14))
    runner = AcceptanceRunner(acceptance, report)
    for tier in (2, 3):
        summary = verify(
            _request(repo, sha, evidence_root, tier=tier, mode="report"),
            registry=registry,
            runner=runner,
        )
        assert summary.status == "passed"
    assert (
        run_release_gate(
            repository_root=repo,
            expected_sha=sha,
            evidence_root=evidence_root,
        )
        == 0
    )
    capsys.readouterr()

    monkeypatch.setattr(ControlledDate, "current", date(2026, 10, 15))
    assert (
        run_release_gate(
            repository_root=repo,
            expected_sha=sha,
            evidence_root=evidence_root,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "pip-audit: no retained Tier 2 evidence for this fingerprint" in output
