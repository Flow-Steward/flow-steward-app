from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.tooling.quality.check_registry import load_check_registry
from core.application.tooling.quality.fingerprint import FingerprintContext, fingerprint_check
from core.application.tooling.quality.release_gate import run_release_gate


def _canonical_repo_root() -> Path:
    """The checkout that has Git metadata.

    The core-only runner copies the tree into a temporary workspace without
    `.git`, deliberately — so a Git-dependent test must be told where the real
    checkout is rather than running `git` against wherever it happens to live.
    The runner exports that path; outside it, this file's own tree is canonical.
    """
    supplied = os.environ.get("PYTEST_SOURCE_REPO_ROOT", "").strip()
    return Path(supplied).resolve() if supplied else Path(__file__).resolve().parents[4]


ROOT = _canonical_repo_root()
SOURCE_ROOT = ROOT


@pytest.fixture(autouse=True)
def candidate_repository(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    registry = root / "infra/tooling/quality/check-registry.json"
    registry.parent.mkdir(parents=True)
    shutil.copyfile(SOURCE_ROOT / "infra/tooling/quality/check-registry.json", registry)
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    subprocess.run(("git", "-C", str(root), "add", "."), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Quality test",
            "-c",
            "user.email=quality-test@example.invalid",
            "commit",
            "-qm",
            "candidate",
        ),
        check=True,
    )
    monkeypatch.setattr(__import__(__name__, fromlist=["ROOT"]), "ROOT", root)
    monkeypatch.setattr(__import__(__name__, fromlist=["_SHA"]), "_SHA", _head_sha())
    return root


def test_release_gate_rejects_candidate_sha_mismatch(capsys) -> None:
    assert run_release_gate(repository_root=ROOT, expected_sha="not-the-candidate") == 1
    assert "candidate SHA does not match local HEAD" in capsys.readouterr().out


def test_release_gate_validates_registry_without_repeating_pytest() -> None:
    assert run_release_gate(repository_root=ROOT, expected_sha="") == 1
    source = (SOURCE_ROOT / "core/application/tooling/quality/release_gate.py").read_text(
        encoding="utf-8"
    )
    assert "pytest" not in source


# ---------------------------------------------------------------------------
# Tier 2 + Tier 3 evidence
#
# The gate matched the SHA and loaded the registry, then passed. It never asked
# whether Tier 3 had run for that SHA, or whether it was green — so a candidate
# with a red Tier 3, or none at all, cleared the release path. The one thing the
# gate exists to prevent was the thing it did not check.
# ---------------------------------------------------------------------------

# Successful fixtures must contain every current owner check, including newly
# registered Tier 2 checks; the production gate remains independently strict.
_REGISTRY_CHECKS = load_check_registry(
    SOURCE_ROOT / "infra/tooling/quality/check-registry.json"
).checks
_TIER3_CHECKS = tuple(check.id for check in _REGISTRY_CHECKS if check.owner_tier == 3)
_TIER2_CHECKS = tuple(check.id for check in _REGISTRY_CHECKS if check.owner_tier == 2)

_REQUIRED_CHECKS = _TIER2_CHECKS + _TIER3_CHECKS


def _head_sha() -> str:
    """The gate compares the candidate against the checkout's real HEAD, so the
    evidence these tests write has to be bound to that same commit.

    Resolved against the canonical checkout, never against the current working
    directory: under the core-only runner that directory is a copy with no
    repository metadata, and `git` there fails at collection time.
    """
    completed = subprocess.run(
        ("git", "-C", str(ROOT), "rev-parse", "HEAD"),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "(no stderr)"
        raise AssertionError(f"canonical checkout {ROOT} has no Git metadata: {detail}")
    return completed.stdout.strip()


_SHA = _head_sha()


def _write_record(
    root,
    check_id: str,
    *,
    status: str = "passed",
    trust: str = "local",
    commit_sha: str | None = None,
    wrong_fingerprint: bool = False,
    wrong_command: bool = False,
) -> None:
    check = next(
        check
        for check in load_check_registry(ROOT / "infra/tooling/quality/check-registry.json").checks
        if check.id == check_id
    )
    fingerprint = (
        hashlib.sha256(check_id.encode()).hexdigest()
        if wrong_fingerprint
        else fingerprint_check(
            check, FingerprintContext(root=ROOT, source="commit", commit_sha=_SHA)
        )
    )
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "check_id": check_id,
                "owner_tier": check.owner_tier,
                "fingerprint": fingerprint,
                "status": status,
                "trust": trust,
                "commit_sha": _SHA if commit_sha is None else commit_sha,
                "command": ["true"] if wrong_command else list(check.command),
                "started_at": datetime.now(UTC).isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_seconds": 1.0,
            },
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    destination = root / check_id / fingerprint / "evidence.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    destination.with_suffix(".sha256").write_text(
        hashlib.sha256(payload).hexdigest() + "\n", encoding="ascii"
    )


@pytest.fixture
def evidence_root(tmp_path) -> Path:
    return tmp_path / "evidence"


def test_a_candidate_with_no_quality_evidence_is_refused(evidence_root) -> None:
    evidence_root.mkdir(parents=True)

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 1
    )


def test_a_red_tier_3_check_is_refused(evidence_root) -> None:
    for check_id in _REQUIRED_CHECKS:
        _write_record(evidence_root, check_id)
    _write_record(evidence_root, "backend-tests", status="failed")

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 1
    )


def test_evidence_for_another_sha_does_not_count(evidence_root) -> None:
    """The point of the gate is the exact candidate, not a green run somewhere."""
    for check_id in _REQUIRED_CHECKS:
        _write_record(evidence_root, check_id, commit_sha="b" * 40)

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 1
    )


def test_local_tier_two_and_tier_three_satisfy_release_evidence(evidence_root) -> None:
    for check_id in _REQUIRED_CHECKS:
        _write_record(evidence_root, check_id, trust="local")

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 0
    )


def test_a_missing_tier_3_check_is_refused(evidence_root) -> None:
    """Green Tier 2 and an incomplete Tier 3 are not release-ready."""
    for check_id in _TIER2_CHECKS + _TIER3_CHECKS[:-1]:
        _write_record(evidence_root, check_id)

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 1
    )


def test_missing_ui_i18n_evidence_is_refused(evidence_root, capsys) -> None:
    assert "ui-i18n" in _TIER2_CHECKS
    for check_id in _REQUIRED_CHECKS:
        if check_id != "ui-i18n":
            _write_record(evidence_root, check_id)

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 1
    )
    assert "ui-i18n: no retained Tier 2 evidence" in capsys.readouterr().out


def test_complete_green_tier_two_and_tier_three_pass(evidence_root) -> None:
    for check_id in _REQUIRED_CHECKS:
        _write_record(evidence_root, check_id)

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 0
    )


def test_the_gate_does_not_claim_tier_3_it_never_checked(capsys) -> None:
    """Without a candidate SHA there is nothing to bind evidence to, so the gate
    skips the Tier 3 check — and used to print that seven results were valid
    anyway. A release log saying the gate verified something it did not look at
    is worse than no line at all."""
    assert run_release_gate(repository_root=ROOT, expected_sha="") == 1

    printed = capsys.readouterr().out
    assert "Release gate failed: no complete green Tier 2 + Tier 3" in printed
    assert "Release gate passed" not in printed
    assert "green Tier 3 results are valid" not in printed


def test_default_local_release_gate_refuses_missing_evidence(
    evidence_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    assert run_release_gate(repository_root=ROOT, evidence_root=evidence_root) == 1


def test_default_local_release_gate_binds_green_evidence_to_head(evidence_root) -> None:
    for check_id in _REQUIRED_CHECKS:
        _write_record(evidence_root, check_id)
    assert run_release_gate(repository_root=ROOT, evidence_root=evidence_root) == 0


def test_release_gate_refuses_dirty_tree_even_with_green_head_evidence(evidence_root) -> None:
    for check_id in _REQUIRED_CHECKS:
        _write_record(evidence_root, check_id)
    (ROOT / "uncommitted.py").write_text("VALUE = 2\n")
    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 1
    )


@pytest.mark.parametrize("defect", ["wrong_fingerprint", "wrong_command"])
def test_release_gate_refuses_wrong_check_identity(evidence_root, defect) -> None:
    for check_id in _REQUIRED_CHECKS:
        if check_id != "backend-tests":
            _write_record(evidence_root, check_id)
    _write_record(evidence_root, "backend-tests", **{defect: True})
    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 1
    )


def test_green_tier_three_without_tier_two_is_refused(evidence_root) -> None:
    for check_id in _TIER3_CHECKS:
        _write_record(evidence_root, check_id)

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 1
    )


@pytest.mark.parametrize(
    ("check_id", "defect", "value"),
    (
        ("pip-audit", "status", "failed"),
        # env-artifacts is commit-scoped: its evidence describes one commit only.
        ("env-artifacts", "commit_sha", "b" * 40),
        ("pip-audit", "wrong_fingerprint", True),
        ("pip-audit", "wrong_command", True),
    ),
)
def test_release_gate_refuses_invalid_tier_two_evidence(
    evidence_root, check_id, defect, value
) -> None:
    for other in _REQUIRED_CHECKS:
        if other != check_id:
            _write_record(evidence_root, other)
    _write_record(evidence_root, check_id, **{defect: value})

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 1
    )


def test_input_scoped_evidence_from_an_earlier_commit_satisfies_the_release_gate(
    evidence_root,
) -> None:
    """ADR 0036: the fingerprint recomputed from the final tree is the proof."""
    for check_id in _REQUIRED_CHECKS:
        if check_id != "pip-audit":
            _write_record(evidence_root, check_id)
    _write_record(evidence_root, "pip-audit", commit_sha="b" * 40)

    assert (
        run_release_gate(repository_root=ROOT, expected_sha=_SHA, evidence_root=evidence_root) == 0
    )
