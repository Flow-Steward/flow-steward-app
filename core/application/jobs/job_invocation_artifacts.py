"""Durable replay authority and bounded projections for job inputs."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Collection, Mapping
from typing import Any

from core.application.auth.secrets import decrypt_secret, encrypt_secret

_TOKEN_SUMMARY = "$summary"
_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL = "job.replay_snapshot_partial"
_TOKEN_JOB_REPLAY_SNAPSHOT_SCOPE_MISMATCH = "job.replay_snapshot_scope_mismatch"
_TOKEN_JOB_REPLAY_SNAPSHOT_DIGEST_MISMATCH = "job.replay_snapshot_digest_mismatch"

REPLAY_SNAPSHOT_KIND = "job_replay_snapshot"
INVOCATION_ARTIFACT_VERSION = 1
INVOCATION_PROJECTION_MAX_BYTES = 16 * 1024
INVOCATION_ARTIFACT_READ_MAX_BYTES = 64 * 1024 * 1024
MAX_WORKFLOW_SECRET_REF_FINGERPRINTS = 64
_REDACTED = "[REDACTED]"
_SECRET_KEY_PARTS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "session",
    "token",
}
_FILE_SUMMARY_FIELDS = (
    "artifact_id",
    "dataset_id",
    "sha256",
    "size_bytes",
    "filename",
    "content_type",
    "field_name",
)
_REPLAY_FILE_FIELDS = frozenset(
    {
        "artifact_id",
        "dataset_id",
        "sha256",
        "size_bytes",
        "owner_project_id",
        "owner_job_id",
        "filename",
        "content_type",
        "field_name",
    }
)
_REPLAY_SNAPSHOT_FIELDS = frozenset(
    {
        "workflow_id",
        "inputs",
        "files",
        "test_mode",
        "input_contract",
        "task_envelope",
        "_workflow_secret_ref_fingerprints",
    }
)
_SUPPORTED_INPUT_CONTRACTS = frozenset({"direct_inputs", "task_envelope"})
_REPLAY_POINTER_FIELDS = frozenset(
    {
        "kind",
        "version",
        "status",
        "artifact_id",
        "size_bytes",
        "sha256",
        "artifact_sha256",
        "retention_class",
        "expires_at",
    }
)


class JobReplaySnapshotError(ValueError):
    """Fail-closed replay validation error with a stable public code."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _json_value(value: Any) -> Any:
    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_secret_names(values: Collection[str] | None) -> set[str]:
    return {str(value or "").strip().lower() for value in (values or ()) if str(value).strip()}


def _secret_like_key(key: Any, declared_secret_inputs: set[str]) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in declared_secret_inputs:
        return True
    return any(
        normalized == part or normalized.startswith(f"{part}_") or normalized.endswith(f"_{part}")
        for part in _SECRET_KEY_PARTS
    )


def _redact_value(value: Any, *, declared_secret_inputs: set[str]) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key in sorted(value, key=lambda candidate: str(candidate)):
            normalized_key = str(key)
            if _secret_like_key(normalized_key, declared_secret_inputs):
                redacted[normalized_key] = _REDACTED
            else:
                redacted[normalized_key] = _redact_value(
                    value[key],
                    declared_secret_inputs=declared_secret_inputs,
                )
        return redacted
    if isinstance(value, (list, tuple)):
        return [
            _redact_value(item, declared_secret_inputs=declared_secret_inputs) for item in value
        ]
    return _json_value(value)


def _bounded_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= 2048:
            return value, False
        return {
            _TOKEN_SUMMARY: "string",
            "size_bytes": len(encoded),
            "sha256": _digest(encoded),
        }, True
    if isinstance(value, list):
        values = value[:25]
        bounded: list[Any] = []
        truncated = len(value) > len(values)
        for item in values:
            child, child_truncated = _bounded_value(item)
            bounded.append(child)
            truncated = truncated or child_truncated
        if len(value) > len(values):
            bounded.append({_TOKEN_SUMMARY: "remaining_items", "count": len(value) - len(values)})
        return bounded, truncated
    if isinstance(value, dict):
        keys = sorted(value)[:50]
        bounded_dict: dict[str, Any] = {}
        truncated = len(value) > len(keys)
        for key in keys:
            child, child_truncated = _bounded_value(value[key])
            bounded_dict[str(key)] = child
            truncated = truncated or child_truncated
        if len(value) > len(keys):
            bounded_dict[_TOKEN_SUMMARY] = {
                "kind": "remaining_keys",
                "count": len(value) - len(keys),
            }
        return bounded_dict, truncated
    return value, False


def _file_summaries(files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    summaries: list[dict[str, Any]] = []
    for row in files[:50]:
        if not isinstance(row, dict):
            continue
        summaries.append(
            {key: row[key] for key in _FILE_SUMMARY_FIELDS if row.get(key) is not None}
        )
    return summaries, len(files) > 50


def build_effective_input_projection(
    *,
    inputs: dict[str, Any],
    files: list[dict[str, Any]] | None = None,
    declared_secret_inputs: Collection[str] | None = None,
    max_bytes: int = INVOCATION_PROJECTION_MAX_BYTES,
) -> dict[str, Any]:
    """Return a deterministic, recursively redacted, non-authoritative UI projection."""
    normalized_inputs = _json_value(dict(inputs or {}))
    canonical = _canonical_bytes(normalized_inputs)
    declared = _normalized_secret_names(declared_secret_inputs)
    redacted = _redact_value(normalized_inputs, declared_secret_inputs=declared)
    bounded_inputs, input_truncated = _bounded_value(redacted)
    file_summaries, files_truncated = _file_summaries(list(files or []))
    projection: dict[str, Any] = {
        "mapped_inputs": bounded_inputs,
        "files": file_summaries,
        "source_keys": sorted(str(key) for key in normalized_inputs),
        "size_bytes": len(canonical),
        "sha256": _digest(canonical),
        "truncated": bool(input_truncated or files_truncated),
    }
    resolved_max = max(512, int(max_bytes or INVOCATION_PROJECTION_MAX_BYTES))
    if len(_canonical_bytes(projection)) > resolved_max:
        projection["mapped_inputs"] = {
            _TOKEN_SUMMARY: "object",
            "keys": projection["source_keys"][:100],
            "size_bytes": len(canonical),
            "sha256": projection["sha256"],
        }
        projection["files"] = file_summaries[:10]
        projection["truncated"] = True
    if len(_canonical_bytes(projection)) > resolved_max:
        projection["mapped_inputs"] = {
            _TOKEN_SUMMARY: "object",
            "key_count": len(projection["source_keys"]),
            "size_bytes": len(canonical),
            "sha256": projection["sha256"],
        }
        projection["source_keys"] = projection["source_keys"][:25]
        projection["files"] = file_summaries[:3]
    return projection


def _artifact_pointer(
    *,
    kind: str,
    artifact_id: str,
    plaintext: bytes,
    artifact_meta: dict[str, Any],
    job_id: str,
) -> dict[str, Any]:
    del job_id
    return {
        "kind": kind,
        "version": INVOCATION_ARTIFACT_VERSION,
        "status": "available",
        "artifact_id": artifact_id,
        "size_bytes": len(plaintext),
        "sha256": _digest(plaintext),
        "artifact_sha256": str(artifact_meta.get("sha256") or ""),
        "retention_class": str(artifact_meta.get("retention_class") or ""),
        "expires_at": artifact_meta.get("expires_at"),
    }


def _create_encrypted_artifact(
    *,
    artifact_plane: Any,
    kind: str,
    project_id: str,
    job_id: str,
    payload: dict[str, Any],
    filename: str,
    retention_class: str,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    plaintext = _canonical_bytes(payload)
    ciphertext = encrypt_secret(plaintext.decode("utf-8")).encode("utf-8")
    if len(ciphertext) > INVOCATION_ARTIFACT_READ_MAX_BYTES:
        raise JobReplaySnapshotError("job.replay_snapshot_too_large")
    handle, meta = artifact_plane.create_artifact(
        artifact_id=artifact_id,
        owner_scope={"project_id": str(project_id), "job_id": str(job_id)},
        run_id=str(job_id),
        step_id="job_invocation",
        filename=filename,
        content_type="application/vnd.flow-steward.encrypted+json",
        retention_class=retention_class,
        body=ciphertext,
    )
    return _artifact_pointer(
        kind=kind,
        artifact_id=handle.artifact_id,
        plaintext=plaintext,
        artifact_meta=meta,
        job_id=job_id,
    )


def _normalize_replay_file_row(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item).difference(_REPLAY_FILE_FIELDS):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    artifact_id = str(item.get("artifact_id") or "").strip()
    dataset_id = str(item.get("dataset_id") or "").strip()
    if bool(artifact_id) == bool(dataset_id):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    digest = str(item.get("sha256") or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    size_bytes = item.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    owner_project_id = str(item.get("owner_project_id") or "").strip()
    owner_job_id = str(item.get("owner_job_id") or "").strip()
    if not owner_project_id or not owner_job_id:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    normalized: dict[str, Any] = {
        "artifact_id" if artifact_id else "dataset_id": artifact_id or dataset_id,
        "sha256": digest,
        "size_bytes": size_bytes,
        "owner_project_id": owner_project_id,
        "owner_job_id": owner_job_id,
    }
    for key in ("filename", "content_type", "field_name"):
        value = str(item.get(key) or "").strip()
        if value:
            normalized[key] = value
    return normalized


def _normalize_secret_ref_fingerprints(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_WORKFLOW_SECRET_REF_FINGERPRINTS:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    normalized: list[str] = []
    for item in value:
        fingerprint = str(item or "").strip()
        if (
            len(fingerprint) != 64
            or fingerprint.lower() != fingerprint
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
        normalized.append(fingerprint)
    if len(set(normalized)) != len(normalized):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    return sorted(normalized)


def _normalize_replay_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    normalized = _json_value(dict(snapshot or {}))
    if not isinstance(normalized, dict) or set(normalized) != _REPLAY_SNAPSHOT_FIELDS:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    if not isinstance(normalized.get("workflow_id"), str) or not normalized["workflow_id"].strip():
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    if not isinstance(normalized.get("inputs"), dict):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    if not isinstance(normalized.get("files"), list):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    if not isinstance(normalized.get("test_mode"), bool):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    if normalized.get("input_contract") not in _SUPPORTED_INPUT_CONTRACTS:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    task_envelope = normalized.get("task_envelope")
    if not isinstance(task_envelope, dict):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    normalized["files"] = [_normalize_replay_file_row(item) for item in normalized["files"]]
    normalized["_workflow_secret_ref_fingerprints"] = _normalize_secret_ref_fingerprints(
        normalized.get("_workflow_secret_ref_fingerprints")
    )
    envelope_inputs = task_envelope.get("inputs")
    envelope_files = task_envelope.get("files")
    if (
        not isinstance(envelope_inputs, dict)
        or not isinstance(envelope_files, list)
        or envelope_inputs != normalized["inputs"]
        or envelope_files != normalized["files"]
    ):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    return normalized


def mint_replay_file_handles(
    *,
    artifact_plane: Any,
    project_id: str,
    job_id: str,
    files: list[Any],
) -> list[dict[str, Any]]:
    """Mint immutable replay rows from host-resolved artifact/dataset metadata."""
    minted: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
        artifact_id = str(item.get("artifact_id") or "").strip()
        dataset_id = str(item.get("dataset_id") or "").strip()
        if bool(artifact_id) == bool(dataset_id):
            raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
        kind = "artifact" if artifact_id else "dataset"
        handle_id = artifact_id or dataset_id
        getter = artifact_plane.get_artifact if kind == "artifact" else artifact_plane.get_dataset
        meta = getter(handle_id)
        if not isinstance(meta, dict):
            raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
        owner = meta.get("owner_scope")
        owner = dict(owner) if isinstance(owner, dict) else {}
        owner_project_id = str(owner.get("project_id") or "").strip()
        owner_job_id = str(owner.get("job_id") or "").strip()
        if not owner_job_id and owner_project_id in {"", str(project_id)}:
            bind_owner = getattr(artifact_plane, "bind_replay_owner", None)
            if not callable(bind_owner):
                raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_SCOPE_MISMATCH)
            try:
                meta = bind_owner(
                    artifact_id=artifact_id or None,
                    dataset_id=dataset_id or None,
                    project_id=str(project_id),
                    job_id=str(job_id),
                )
            except Exception as exc:
                raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_SCOPE_MISMATCH) from exc
            owner = meta.get("owner_scope") if isinstance(meta, dict) else None
            owner = dict(owner) if isinstance(owner, dict) else {}
            owner_project_id = str(owner.get("project_id") or "").strip()
            owner_job_id = str(owner.get("job_id") or "").strip()
        if owner_project_id != str(project_id) or not owner_job_id:
            raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_SCOPE_MISMATCH)
        candidate: dict[str, Any] = {
            f"{kind}_id": handle_id,
            "sha256": str(meta.get("sha256") or "").strip().lower(),
            "size_bytes": meta.get("size_bytes"),
            "owner_project_id": owner_project_id,
            "owner_job_id": owner_job_id,
        }
        for key in ("filename", "content_type", "field_name"):
            value = str(item.get(key) or meta.get(key) or "").strip()
            if value:
                candidate[key] = value
        minted.append(_normalize_replay_file_row(candidate))
    return minted


def capture_replay_snapshot(
    *,
    artifact_plane: Any,
    project_id: str,
    job_id: str,
    snapshot: dict[str, Any],
    retention_class: str = "audit",
    artifact_id: str | None = None,
) -> dict[str, Any]:
    normalized = _normalize_replay_snapshot(snapshot)
    payload = {
        "kind": REPLAY_SNAPSHOT_KIND,
        "version": INVOCATION_ARTIFACT_VERSION,
        "project_id": str(project_id),
        "job_id": str(job_id),
        "snapshot": normalized,
    }
    return _create_encrypted_artifact(
        artifact_plane=artifact_plane,
        kind=REPLAY_SNAPSHOT_KIND,
        project_id=project_id,
        job_id=job_id,
        payload=payload,
        filename="replay-snapshot.enc",
        retention_class=retention_class,
        artifact_id=artifact_id,
    )


def reserve_replay_snapshot_artifact() -> dict[str, str]:
    """Mint the exact cleanup reference before snapshot storage begins."""
    artifact_id = f"art_{uuid.uuid4().hex[:24]}"
    return {
        "artifact_id": artifact_id,
        "object_key": f"artifacts/{artifact_id}/replay-snapshot.enc",
    }


def _required_pointer(pointer: Any, *, kind: str) -> dict[str, Any]:
    if not isinstance(pointer, dict) or not pointer:
        raise JobReplaySnapshotError("job.replay_snapshot_absent")
    if set(pointer) != _REPLAY_POINTER_FIELDS:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    artifact_id = pointer.get("artifact_id")
    size_bytes = pointer.get("size_bytes")
    version = pointer.get("version")
    expires_at = pointer.get("expires_at")
    digests = (pointer.get("sha256"), pointer.get("artifact_sha256"))
    if (
        pointer.get("kind") != kind
        or pointer.get("status") != "available"
        or type(version) is not int
        or version != INVOCATION_ARTIFACT_VERSION
        or not isinstance(artifact_id, str)
        or not artifact_id.strip().startswith("art_")
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or not isinstance(pointer.get("retention_class"), str)
        or not pointer["retention_class"].strip()
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or digest.lower() != digest
            or any(char not in "0123456789abcdef" for char in digest)
            for digest in digests
        )
        or (
            expires_at is not None
            and (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)))
        )
    ):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        raise JobReplaySnapshotError("job.replay_snapshot_expired")
    return dict(pointer)


def _artifact_payload(
    *,
    artifact_plane: Any,
    pointer: dict[str, Any],
    project_id: str,
    job_id: str,
    kind: str,
) -> dict[str, Any]:
    artifact_id = str(pointer["artifact_id"])
    meta = artifact_plane.get_artifact(artifact_id)
    if not isinstance(meta, dict):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    expires_at = meta.get("expires_at")
    if meta.get("expired") is True or (
        isinstance(expires_at, (int, float)) and float(expires_at) <= time.time()
    ):
        raise JobReplaySnapshotError("job.replay_snapshot_expired")
    raw_owner = meta.get("owner_scope")
    owner: dict[str, Any] = raw_owner if isinstance(raw_owner, dict) else {}
    if (
        str(owner.get("project_id") or "") != str(project_id)
        or str(owner.get("job_id") or "") != str(job_id)
        or str(meta.get("run_id") or "") != str(job_id)
    ):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_SCOPE_MISMATCH)
    if str(meta.get("sha256") or "") != str(pointer.get("artifact_sha256") or ""):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_DIGEST_MISMATCH)
    try:
        ciphertext = artifact_plane.read_artifact_bytes(
            artifact_id,
            max_size_bytes=INVOCATION_ARTIFACT_READ_MAX_BYTES,
        )
    except Exception as exc:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL) from exc
    if ciphertext is None:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    if _digest(ciphertext) != str(pointer.get("artifact_sha256") or ""):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_DIGEST_MISMATCH)
    try:
        plaintext = decrypt_secret(ciphertext.decode("utf-8")).encode("utf-8")
        payload = json.loads(plaintext)
    except Exception as exc:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_DIGEST_MISMATCH) from exc
    if len(plaintext) != int(pointer.get("size_bytes") or -1) or _digest(plaintext) != str(
        pointer.get("sha256") or ""
    ):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_DIGEST_MISMATCH)
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    if payload.get("version") != INVOCATION_ARTIFACT_VERSION:
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    if str(payload.get("project_id") or "") != str(project_id) or str(
        payload.get("job_id") or ""
    ) != str(job_id):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_SCOPE_MISMATCH)
    return payload


def _validate_referenced_artifacts(
    *,
    artifact_plane: Any,
    project_id: str,
    files: list[Any],
) -> None:
    for item in files:
        row = _normalize_replay_file_row(item)
        artifact_id = str(row.get("artifact_id") or "")
        dataset_id = str(row.get("dataset_id") or "")
        meta = (
            artifact_plane.get_artifact(artifact_id)
            if artifact_id
            else artifact_plane.get_dataset(dataset_id)
        )
        if not isinstance(meta, dict):
            raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
        raw_owner = meta.get("owner_scope")
        owner: dict[str, Any] = raw_owner if isinstance(raw_owner, dict) else {}
        owner_project = str(owner.get("project_id") or "")
        owner_job = str(owner.get("job_id") or "")
        if (
            owner_project != str(project_id)
            or owner_project != str(row["owner_project_id"])
            or owner_job != str(row["owner_job_id"])
        ):
            raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_SCOPE_MISMATCH)
        if str(meta.get("sha256") or "").strip().lower() != str(row["sha256"]):
            raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_DIGEST_MISMATCH)
        meta_size_bytes = meta.get("size_bytes")
        if (
            isinstance(meta_size_bytes, bool)
            or not isinstance(meta_size_bytes, int)
            or meta_size_bytes != row["size_bytes"]
        ):
            raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_DIGEST_MISMATCH)


def load_replay_snapshot(
    *,
    artifact_plane: Any,
    pointer: dict[str, Any] | None,
    project_id: str,
    job_id: str,
) -> dict[str, Any]:
    resolved_pointer = _required_pointer(pointer, kind=REPLAY_SNAPSHOT_KIND)
    payload = _artifact_payload(
        artifact_plane=artifact_plane,
        pointer=resolved_pointer,
        project_id=project_id,
        job_id=job_id,
        kind=REPLAY_SNAPSHOT_KIND,
    )
    raw_snapshot = payload.get("snapshot")
    if not isinstance(raw_snapshot, dict):
        raise JobReplaySnapshotError(_TOKEN_JOB_REPLAY_SNAPSHOT_PARTIAL)
    snapshot = _normalize_replay_snapshot(raw_snapshot)
    _validate_referenced_artifacts(
        artifact_plane=artifact_plane,
        project_id=project_id,
        files=list(snapshot["files"]),
    )
    return snapshot


__all__ = [
    "INVOCATION_ARTIFACT_VERSION",
    "INVOCATION_PROJECTION_MAX_BYTES",
    "MAX_WORKFLOW_SECRET_REF_FINGERPRINTS",
    "REPLAY_SNAPSHOT_KIND",
    "JobReplaySnapshotError",
    "build_effective_input_projection",
    "capture_replay_snapshot",
    "load_replay_snapshot",
    "mint_replay_file_handles",
]
