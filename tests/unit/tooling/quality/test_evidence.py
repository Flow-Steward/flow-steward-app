from __future__ import annotations

import errno
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.application.tooling.quality import evidence as evidence_module
from core.application.tooling.quality.evidence import (
    EvidenceExpectation,
    EvidencePolicy,
    EvidenceRecord,
    EvidenceStatus,
    EvidenceStore,
    EvidenceTrust,
)

FINGERPRINT = "a" * 64
COMMIT_SHA = "b" * 40
STARTED_AT = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _record(
    *,
    status: EvidenceStatus = "passed",
    trust: EvidenceTrust = "local",
    fingerprint: str = FINGERPRINT,
    commit_sha: str = COMMIT_SHA,
) -> EvidenceRecord:
    return EvidenceRecord(
        check_id="mypy",
        owner_tier=2,
        fingerprint=fingerprint,
        status=status,
        trust=trust,
        commit_sha=commit_sha,
        command=("mypy", "core"),
        started_at=STARTED_AT,
        completed_at=STARTED_AT + timedelta(seconds=2),
        duration_seconds=2.0,
    )


def _expected(*, fingerprint: str = FINGERPRINT) -> EvidenceExpectation:
    return EvidenceExpectation(
        check_id="mypy",
        owner_tier=2,
        fingerprint=fingerprint,
        commit_sha=COMMIT_SHA,
    )


def test_atomic_round_trip_and_checksum(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    record = _record()

    path = store.write(record)

    assert path.name == "evidence.json"
    assert path.with_suffix(".sha256").is_file()
    assert store.read(record.check_id, record.fingerprint) == record
    assert not list(path.parent.glob("*.tmp"))


def test_atomic_write_tolerates_unsupported_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync

    def reject_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EBADF, "directory fsync unsupported")
        real_fsync(descriptor)

    monkeypatch.setattr(evidence_module.os, "fsync", reject_directory_fsync)
    store = EvidenceStore(tmp_path)
    record = _record()

    store.write(record)

    assert store.read(record.check_id, record.fingerprint) == record


@pytest.mark.parametrize("error_number", [errno.EEXIST, errno.ENOENT])
def test_atomic_write_tolerates_bind_mount_file_fsync_when_flushed_file_is_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    real_fsync = os.fsync

    def reject_bind_mounted_file_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(error_number, "Docker Desktop bind-mount fsync is unsupported")
        real_fsync(descriptor)

    monkeypatch.setattr(evidence_module.os, "fsync", reject_bind_mounted_file_fsync)
    store = EvidenceStore(tmp_path)
    record = _record()

    store.write(record)

    assert store.read(record.check_id, record.fingerprint) == record


def test_file_fsync_error_does_not_publish_a_replaced_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def replace_then_fail(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            path.unlink()
            path.write_text("not the flushed evidence")
            raise OSError(errno.ENOENT, "temporary file replaced")

    monkeypatch.setattr(evidence_module.os, "fsync", replace_then_fail)
    store = EvidenceStore(tmp_path)
    record = _record()
    with pytest.raises(OSError, match="temporary file replaced"):
        store.write(record)
    assert store.read(record.check_id, record.fingerprint) is None


def test_checksum_mismatch_returns_no_record_with_diagnostic(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    record = _record()
    path = store.write(record)
    path.write_text("{}\n", encoding="utf-8")

    assert store.read(record.check_id, record.fingerprint) is None
    assert store.last_error == "checksum_mismatch"


@pytest.mark.parametrize("status", ["failed", "infrastructure_failed", "incomplete"])
def test_non_pass_never_satisfies_prerequisite(
    status: EvidenceStatus,
) -> None:
    decision = EvidencePolicy.local().evaluate(_record(status=status), _expected())

    assert decision.reusable is False
    assert decision.reason == f"status:{status}"


def test_release_policy_rejects_local_exact_sha_evidence() -> None:
    decision = EvidencePolicy.release().evaluate(_record(), _expected())

    assert (decision.reusable, decision.reason) == (False, "trusted_ci_required")


def test_release_policy_accepts_trusted_ci_for_exact_sha() -> None:
    decision = EvidencePolicy.release().evaluate(_record(trust="trusted_ci"), _expected())

    assert (decision.reusable, decision.reason) == (True, "reusable")


def test_fingerprint_mismatch_is_rejected_before_trust() -> None:
    decision = EvidencePolicy.release().evaluate(
        _record(trust="trusted_ci"), _expected(fingerprint="c" * 64)
    )

    assert (decision.reusable, decision.reason) == (False, "fingerprint_mismatch")


def test_commit_mismatch_is_rejected_for_release() -> None:
    expected = _expected().model_copy(update={"commit_sha": "d" * 40})

    decision = EvidencePolicy.release().evaluate(_record(trust="trusted_ci"), expected)

    assert (decision.reusable, decision.reason) == (False, "commit_sha_mismatch")


def test_secret_values_are_not_serializable_fields() -> None:
    assert "environment" not in EvidenceRecord.model_fields
    assert "token" not in EvidenceRecord.model_fields
    assert "runtime_identity" not in EvidenceRecord.model_fields


def test_input_scoped_evidence_counts_for_a_later_commit_with_the_same_fingerprint() -> None:
    expected = _expected().model_copy(update={"commit_sha": "d" * 40, "reuse_scope": "inputs"})

    decision = EvidencePolicy.local().evaluate(_record(), expected)

    assert (decision.reusable, decision.reason) == (True, "reusable")


def test_input_scope_never_overrides_a_fingerprint_mismatch() -> None:
    expected = _expected(fingerprint="c" * 64).model_copy(
        update={"commit_sha": "d" * 40, "reuse_scope": "inputs"}
    )

    decision = EvidencePolicy.local().evaluate(_record(), expected)

    assert (decision.reusable, decision.reason) == (False, "fingerprint_mismatch")


def test_local_miss_is_served_from_the_shared_store_and_copied_back(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    record = _record()
    EvidenceStore(tmp_path / "other-worktree", shared_root=shared).write(record)
    store = EvidenceStore(tmp_path / "fresh-worktree", shared_root=shared)

    assert store.read(record.check_id, record.fingerprint) == record
    assert EvidenceStore(tmp_path / "fresh-worktree").read(record.check_id, record.fingerprint) == (
        record
    )


def test_corrupt_shared_evidence_is_a_miss_not_a_pass(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    record = _record()
    path = EvidenceStore(tmp_path / "a", shared_root=shared).write(record)
    shared_copy = shared / path.relative_to(tmp_path / "a")
    shared_copy.write_bytes(shared_copy.read_bytes().replace(b"passed", b"failed"))
    store = EvidenceStore(tmp_path / "b", shared_root=shared)

    assert store.read(record.check_id, record.fingerprint) is None
    assert store.last_error == "checksum_mismatch"
    assert not (tmp_path / "b").exists()


def test_shared_store_is_used_only_when_the_audit_cache_volume_is_mounted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(evidence_module.Path, "home", lambda: tmp_path)
    assert evidence_module.shared_evidence_root() is None

    (tmp_path / ".cache/flow-steward-quality").mkdir(parents=True)

    assert evidence_module.shared_evidence_root() == (
        tmp_path / ".cache/flow-steward-quality/evidence"
    )
