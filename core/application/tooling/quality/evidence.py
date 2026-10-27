"""Checksummed evidence records and reuse trust policy."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvidenceStatus = Literal["passed", "failed", "infrastructure_failed", "incomplete"]
EvidenceTrust = Literal["local", "trusted_ci"]

_CHECK_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_CHECKSUM_SUFFIX = ".sha256"


class EvidenceRecord(BaseModel):
    """Secret-free result of one fingerprinted check execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    check_id: str = Field(pattern=_CHECK_ID.pattern)
    owner_tier: Literal[1, 2, 3]
    fingerprint: str = Field(pattern=_FINGERPRINT.pattern)
    status: EvidenceStatus
    trust: EvidenceTrust
    commit_sha: str = ""
    command: tuple[str, ...] = Field(min_length=1)
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)
    report_paths: tuple[str, ...] = ()
    report_checksums: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class EvidenceExpectation(BaseModel):
    """Identity the current verification expects evidence to match."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str = Field(pattern=_CHECK_ID.pattern)
    owner_tier: Literal[1, 2, 3]
    fingerprint: str = Field(pattern=_FINGERPRINT.pattern)
    commit_sha: str = ""
    reuse_scope: Literal["commit", "inputs"] = "commit"


class EvidenceDecision(BaseModel):
    """Reusable-or-miss policy result with a stable diagnostic reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reusable: bool
    reason: str


class EvidencePolicy:
    """Evaluate evidence under local or exact-candidate release trust rules."""

    def __init__(self, *, trusted_ci_required: bool) -> None:
        self.trusted_ci_required = trusted_ci_required

    @classmethod
    def local(cls) -> EvidencePolicy:
        return cls(trusted_ci_required=False)

    @classmethod
    def release(cls) -> EvidencePolicy:
        return cls(trusted_ci_required=True)

    def evaluate(self, record: EvidenceRecord, expected: EvidenceExpectation) -> EvidenceDecision:
        if record.status != "passed":
            return EvidenceDecision(reusable=False, reason=f"status:{record.status}")
        if record.check_id != expected.check_id:
            return EvidenceDecision(reusable=False, reason="check_id_mismatch")
        if record.owner_tier != expected.owner_tier:
            return EvidenceDecision(reusable=False, reason="owner_tier_mismatch")
        if record.fingerprint != expected.fingerprint:
            return EvidenceDecision(reusable=False, reason="fingerprint_mismatch")
        if (
            expected.commit_sha
            and expected.reuse_scope == "commit"
            and record.commit_sha != expected.commit_sha
        ):
            return EvidenceDecision(reusable=False, reason="commit_sha_mismatch")
        if self.trusted_ci_required and record.trust != "trusted_ci":
            return EvidenceDecision(reusable=False, reason="trusted_ci_required")
        return EvidenceDecision(reusable=True, reason="reusable")


def shared_evidence_root() -> Path | None:
    """Evidence store shared by every checkout, when the audit container mounts one.

    The fs-audit service mounts the `flow-steward-quality-cache` volume here. Each
    worktree keeps its own `logs/quality/evidence`, so without this a fresh
    worktree starts with no evidence at all and reruns every check.
    """
    root = Path.home() / ".cache" / "flow-steward-quality"
    if root.is_dir() and os.access(root, os.W_OK):
        return root / "evidence"
    return None


class EvidenceStore:
    """Filesystem evidence store that treats corruption as a cache miss.

    With ``shared_root`` a local miss is looked up there and copied back, and
    every write goes to both, so release validation still reads local evidence.
    """

    def __init__(self, root: Path, *, shared_root: Path | None = None) -> None:
        self.root = root
        self.shared_root = shared_root
        self.last_error = ""

    def path_for(self, check_id: str, fingerprint: str) -> Path:
        if _CHECK_ID.fullmatch(check_id) is None:
            raise ValueError(f"invalid check id: {check_id!r}")
        if _FINGERPRINT.fullmatch(fingerprint) is None:
            raise ValueError("invalid evidence fingerprint")
        return self.root / check_id / fingerprint / "evidence.json"

    def write(self, record: EvidenceRecord) -> Path:
        destination = self.path_for(record.check_id, record.fingerprint)
        payload = record.model_dump_json(indent=2).encode("utf-8") + b"\n"
        checksum = hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n"
        _atomic_write(destination, payload)
        _atomic_write(destination.with_suffix(_CHECKSUM_SUFFIX), checksum)
        if self.shared_root is not None:
            shared = self.shared_root / destination.relative_to(self.root)
            try:
                _atomic_write(shared, payload)
                _atomic_write(shared.with_suffix(_CHECKSUM_SUFFIX), checksum)
            except OSError:
                pass  # The shared store only saves time; local evidence is the record.
        self.last_error = ""
        return destination

    def read(self, check_id: str, fingerprint: str) -> EvidenceRecord | None:
        record = self._read_from(self.root, check_id, fingerprint)
        if record is not None or self.last_error != "not_found" or self.shared_root is None:
            return record
        record = self._read_from(self.shared_root, check_id, fingerprint)
        if record is None:
            return None
        local = self.path_for(check_id, fingerprint)
        shared = self.shared_root / local.relative_to(self.root)
        try:
            _atomic_write(local, shared.read_bytes())
            _atomic_write(
                local.with_suffix(_CHECKSUM_SUFFIX),
                shared.with_suffix(_CHECKSUM_SUFFIX).read_bytes(),
            )
        except OSError:
            self.last_error = "read_failed"
            return None
        return record

    def _read_from(self, root: Path, check_id: str, fingerprint: str) -> EvidenceRecord | None:
        destination = root / self.path_for(check_id, fingerprint).relative_to(self.root)
        checksum_path = destination.with_suffix(_CHECKSUM_SUFFIX)
        if not destination.is_file() or not checksum_path.is_file():
            self.last_error = "not_found"
            return None
        try:
            payload = destination.read_bytes()
            expected_checksum = checksum_path.read_text(encoding="ascii").strip()
        except OSError:
            self.last_error = "read_failed"
            return None
        if hashlib.sha256(payload).hexdigest() != expected_checksum:
            self.last_error = "checksum_mismatch"
            return None
        try:
            record = EvidenceRecord.model_validate_json(payload)
        except ValueError:
            self.last_error = "invalid_record"
            return None
        if record.check_id != check_id or record.fingerprint != fingerprint:
            self.last_error = "identity_mismatch"
            return None
        self.last_error = ""
        return record


def _atomic_write(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError as exc:
                # Bind mounts can reject fsync on an otherwise intact file.
                # Only degrade durability after verifying the flushed bytes
                # and descriptor identity; a missing/replaced file still fails.
                # Checksummed evidence is a cache, never the check's outcome.
                if exc.errno not in {errno.EEXIST, errno.ENOENT}:
                    raise
                try:
                    intact = (
                        os.path.samestat(os.fstat(handle.fileno()), temporary_path.stat())
                        and temporary_path.read_bytes() == payload
                    )
                except OSError:
                    intact = False
                if not intact:
                    raise
        os.replace(temporary_path, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory_descriptor)
            except OSError as exc:
                unsupported_directory_fsync = {
                    errno.EBADF,
                    errno.EINVAL,
                    errno.ENOTSUP,
                    errno.EOPNOTSUPP,
                }
                if exc.errno not in unsupported_directory_fsync:
                    raise
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)
