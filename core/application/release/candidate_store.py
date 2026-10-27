"""Application-owned repositories for candidate snapshots and evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from core.application.release.candidate_harness import CandidateRunEvidence
from core.application.release.candidate_requirements import AggregatedRequirements
from core.application.release.candidate_snapshot import (
    ExpectedCandidateSnapshot,
    FinalizedCandidateSnapshot,
    SnapshotComparison,
    SnapshotInputs,
    calculate_expected_snapshot,
    compare_finalized_snapshot,
)
from core.infrastructure.release_candidate.env_source import read_last_wins


class CandidateSnapshotStore:
    """Persist and check a single candidate snapshot without exposing secrets."""

    def __init__(self, release_dir: Path) -> None:
        self.release_dir = release_dir
        self.key_path = release_dir / "snapshot-key"
        self.snapshot_path = release_dir / "candidate-snapshot.json"
        self.finalized_path = release_dir / "candidate-finalized.json"

    def materialize(
        self,
        inputs: SnapshotInputs,
        *,
        requirements: AggregatedRequirements,
    ) -> ExpectedCandidateSnapshot:
        self.release_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- owner-only directory requires execute permission.
        os.chmod(self.release_dir, 0o700)
        key = self._load_or_create_key()
        snapshot = self._calculate(inputs, requirements=requirements, key=key)
        _write_owner_only(self.snapshot_path, _canonical_json(snapshot.to_json()))
        return snapshot

    def check(
        self,
        inputs: SnapshotInputs,
        *,
        requirements: AggregatedRequirements,
    ) -> SnapshotComparison:
        key = self.key_path.read_bytes()
        stored_payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        stored = ExpectedCandidateSnapshot.from_json(stored_payload)
        expected = self._calculate(inputs, requirements=requirements, key=key)
        return compare_finalized_snapshot(stored, expected)

    def load_expected(self) -> ExpectedCandidateSnapshot:
        """Load the materialized secret-safe expected snapshot."""

        payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        return ExpectedCandidateSnapshot.from_json(payload)

    def finalize(
        self,
        *,
        image_digests: dict[str, str],
        resource_identities: dict[str, tuple[str, ...]],
        compose_working_dir: str | None = None,
        compose_config_files: str | None = None,
        extension_digest: str | None = None,
        provenance_checksum: str | None = None,
    ) -> FinalizedCandidateSnapshot:
        """Atomically bind runtime identities to the materialized expectation."""

        expected = self.load_expected()
        finalized = FinalizedCandidateSnapshot(
            expected=expected,
            image_digests=dict(sorted(image_digests.items())),
            resource_identities={
                kind: tuple(sorted(identities))
                for kind, identities in sorted(resource_identities.items())
            },
            compose_working_dir=compose_working_dir,
            compose_config_files=compose_config_files,
            extension_digest=extension_digest,
            provenance_checksum=provenance_checksum,
        )
        payload = {
            "schema_version": "flow-steward-candidate-finalized-v1",
            "expected_fingerprint": expected.fingerprint,
            "candidate_commit": expected.candidate_commit,
            "image_digests": dict(finalized.image_digests),
            "resource_identities": {
                kind: list(identities) for kind, identities in finalized.resource_identities.items()
            },
            "compose_working_dir": compose_working_dir,
            "compose_config_files": compose_config_files,
            "extension_digest": extension_digest,
            "provenance_checksum": provenance_checksum,
        }
        if self.finalized_path.exists():
            existing = json.loads(self.finalized_path.read_text(encoding="utf-8"))
            if existing != payload:
                raise ValueError("finalized candidate identity cannot be overwritten")
            return finalized
        _write_owner_only(self.finalized_path, _canonical_json(payload))
        return finalized

    def load_finalized(self) -> FinalizedCandidateSnapshot:
        """Load finalized runtime identities and bind them to the stored expectation."""

        expected = self.load_expected()
        payload = json.loads(self.finalized_path.read_text(encoding="utf-8"))
        if (
            payload.get("schema_version") != "flow-steward-candidate-finalized-v1"
            or payload.get("expected_fingerprint") != expected.fingerprint
            or payload.get("candidate_commit") != expected.candidate_commit
        ):
            raise ValueError("finalized candidate identity is stale or invalid")
        return FinalizedCandidateSnapshot(
            expected=expected,
            image_digests=_string_dict(payload.get("image_digests")),
            resource_identities=_string_tuple_dict(payload.get("resource_identities")),
            compose_working_dir=_optional_string(payload.get("compose_working_dir")),
            compose_config_files=_optional_string(payload.get("compose_config_files")),
            extension_digest=_optional_string(payload.get("extension_digest")),
            provenance_checksum=_optional_string(payload.get("provenance_checksum")),
        )

    def _calculate(
        self,
        inputs: SnapshotInputs,
        *,
        requirements: AggregatedRequirements,
        key: bytes,
    ) -> ExpectedCandidateSnapshot:
        environment = read_last_wins(inputs.source_env)
        source_hashes = {"environment": environment.sha256}
        for name, path in sorted(inputs.source_paths.items()):
            source_hashes[name] = _file_sha256(path)
        return calculate_expected_snapshot(
            inputs,
            requirements,
            key=key,
            environment=environment.values,
            source_hashes=source_hashes,
        )

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes()
        key = secrets.token_bytes(32)
        _write_owner_only(self.key_path, key)
        return key


class CandidateEvidenceStore:
    """Retain checksummed diagnostic evidence outside candidate-owned state."""

    def __init__(self, evidence_root: Path) -> None:
        self.evidence_root = evidence_root

    def write(self, evidence: CandidateRunEvidence) -> Path:
        run_dir = (
            self.evidence_root
            / _safe_component(evidence.identity.candidate_commit)
            / _safe_component(evidence.identity.run_id)
        )
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- retained evidence directories are owner-only.
        os.chmod(run_dir, 0o700)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- retained evidence directories are owner-only.
        os.chmod(artifacts_dir, 0o700)

        artifact_records: list[dict[str, str]] = []
        artifact_index = 0
        for phase_evidence in evidence.phases:
            for source in phase_evidence.log_paths:
                if not source.is_file():
                    raise FileNotFoundError(f"candidate evidence log is missing: {source}")
                artifact_index += 1
                filename = f"{artifact_index:03d}-{_safe_component(source.name)}"
                destination = artifacts_dir / filename
                _copy_owner_only(source, destination)
                artifact_records.append(
                    {
                        "phase": phase_evidence.name,
                        "path": destination.relative_to(run_dir).as_posix(),
                        "sha256": _bare_file_sha256(destination),
                    }
                )

        payload = {**evidence.to_json(), "artifacts": artifact_records}
        manifest = run_dir / "manifest.json"
        _write_owner_only(manifest, _canonical_json(payload))
        _write_owner_only(
            run_dir / "manifest.sha256",
            f"{_bare_file_sha256(manifest)}\n".encode(),
        )
        return run_dir

    def has_verified_evidence(self, candidate_commit: str, *, run_id: str | None = None) -> bool:
        """Return true when any retained run has a valid manifest and artifact checksums."""

        return self.verified_manifest(candidate_commit, run_id=run_id) is not None

    def verified_manifest(
        self,
        candidate_commit: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, object] | None:
        """Load the newest checksummed manifest bound to an exact candidate/run identity."""

        candidate_root = self.evidence_root / _safe_component(candidate_commit)
        if not candidate_root.is_dir():
            return None
        if run_id is not None:
            try:
                run_directories = [candidate_root / _safe_component(run_id)]
            except ValueError:
                return None
        else:
            run_directories = sorted(
                (path for path in candidate_root.iterdir() if path.is_dir()),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        for run_dir in run_directories:
            if not run_dir.is_dir():
                continue
            manifest = run_dir / "manifest.json"
            sidecar = run_dir / "manifest.sha256"
            if not manifest.is_file() or not sidecar.is_file():
                continue
            try:
                expected_manifest_checksum = sidecar.read_text(encoding="utf-8").strip()
                actual_manifest_checksum = _bare_file_sha256(manifest)
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if expected_manifest_checksum != actual_manifest_checksum:
                continue
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("schema_version") != "flow-steward-candidate-evidence-v1"
                or payload.get("candidate_commit") != candidate_commit
                or payload.get("run_id") != run_dir.name
                or (run_id is not None and payload.get("run_id") != run_id)
            ):
                continue
            phases = payload.get("phases")
            artifacts = payload.get("artifacts")
            if (
                not isinstance(phases, list)
                or not phases
                or not isinstance(artifacts, list)
                or not artifacts
            ):
                continue
            phase_names = _finalized_phase_names(phases)
            artifact_phases = {
                artifact.get("phase")
                for artifact in artifacts
                if isinstance(artifact, dict) and isinstance(artifact.get("phase"), str)
            }
            if (
                phase_names is not None
                and set(phase_names) <= artifact_phases
                and all(_artifact_checksum_matches(run_dir, artifact) for artifact in artifacts)
            ):
                return payload
        return None


def _canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _file_sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _bare_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_component(value: str) -> str:
    if not value or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError("candidate evidence path component is invalid")
    return value


def _artifact_checksum_matches(run_dir: Path, artifact: object) -> bool:
    if not isinstance(artifact, dict):
        return False
    relative_path = artifact.get("path")
    expected_sha256 = artifact.get("sha256")
    if not isinstance(relative_path, str) or not isinstance(expected_sha256, str):
        return False
    path = (run_dir / relative_path).resolve()
    if not path.is_relative_to(run_dir.resolve()) or not path.is_file():
        return False
    return _bare_file_sha256(path) == expected_sha256


def _finalized_phase_names(phases: list[object]) -> tuple[str, ...] | None:
    names: list[str] = []
    for phase in phases:
        if not isinstance(phase, dict):
            return None
        name = phase.get("name")
        status = phase.get("status")
        duration = phase.get("duration_seconds")
        if (
            not isinstance(name, str)
            or not name
            or status not in {"passed", "failed", "incomplete"}
            or not isinstance(duration, (int, float))
        ):
            return None
        names.append(name)
    return tuple(names)


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("finalized candidate string mapping is invalid")
    return dict(value)


def _string_tuple_dict(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ValueError("finalized candidate resource mapping is invalid")
    result: dict[str, tuple[str, ...]] = {}
    for key, items in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(items, list)
            or not all(isinstance(item, str) for item in items)
        ):
            raise ValueError("finalized candidate resource mapping is invalid")
        result[key] = tuple(items)
    return result


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("finalized candidate optional string is invalid")
    return value


def _copy_owner_only(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary_path)
        os.chmod(temporary_path, 0o600)
        with temporary_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_owner_only(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
