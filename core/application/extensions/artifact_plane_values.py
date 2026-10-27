"""Artifact and dataset identities, metadata and grant policy."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

DEFAULT_ARTIFACT_UPLOAD_FINALIZE_MAX_SIZE_BYTES = 100 * 1024 * 1024


PREPARING_RESERVATION_TTL_SECONDS = 300


REPLAY_CLEANUP_COMPLETION_PREFIX = "_replay_job_cleanup/"


FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV = "FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS"


def artifact_metadata_key(artifact_id: str) -> str:
    """Return the reserved metadata key for an artifact sidecar."""
    return f"artifacts/_metadata/{str(artifact_id or '').strip()}.json"


def _owner_digest(value: str) -> str:
    return hashlib.sha256(str(value or "").strip().encode("utf-8")).hexdigest()


def replay_owner_manifest_prefix(project_id: str, job_id: str) -> str:
    return f"_replay_job_manifests/{_owner_digest(project_id)}/{_owner_digest(job_id)}/"


def replay_cleanup_completion_key(project_id: str, job_id: str) -> str:
    normalized_project_id = str(project_id or "").strip()
    normalized_job_id = str(job_id or "").strip()
    if not normalized_project_id or not normalized_job_id:
        raise ValueError("cleanup completion identity requires project_id and job_id")
    identity_digest = hashlib.sha256(
        f"{normalized_project_id}\0{normalized_job_id}".encode()
    ).hexdigest()
    radix_path = "/".join(identity_digest[2:])
    return f"{REPLAY_CLEANUP_COMPLETION_PREFIX}{identity_digest[:2]}/{radix_path}/journal.json"


def replay_owner_manifest_key(
    *,
    project_id: str,
    job_id: str,
    kind: str,
    object_id: str,
) -> str:
    normalized_kind = str(kind or "").strip()
    normalized_id = str(object_id or "").strip()
    if normalized_kind not in {"artifact", "dataset"} or not normalized_id or "/" in normalized_id:
        raise ValueError("invalid replay owner manifest identity")
    return (
        f"{replay_owner_manifest_prefix(project_id, job_id)}{normalized_kind}/{normalized_id}.json"
    )


def _replay_owner_manifest(
    row: dict[str, Any],
    *,
    kind: str,
) -> tuple[str, dict[str, Any]] | None:
    owner = row.get("owner_scope")
    owner = owner if isinstance(owner, dict) else {}
    project_id = str(owner.get("project_id") or "").strip()
    job_id = str(owner.get("job_id") or "").strip()
    object_id = str(row.get(f"{kind}_id") or "").strip()
    object_key = str(row.get("object_key") or "").strip()
    if not project_id or not job_id or not object_id or not object_key:
        return None
    metadata_key = (
        artifact_metadata_key(object_id)
        if kind == "artifact"
        else f"datasets/{object_id}/meta.json"
    )
    key = replay_owner_manifest_key(
        project_id=project_id,
        job_id=job_id,
        kind=kind,
        object_id=object_id,
    )
    manifest = {
        "version": 1,
        "kind": kind,
        "object_id": object_id,
        "project_id": project_id,
        "job_id": job_id,
        "object_key": object_key,
        "metadata_key": metadata_key,
    }
    if kind == "dataset":
        manifest["representation_prefix"] = f"datasets/{object_id}/representations/"
    return key, manifest


def _valid_dataset_object_key(dataset_id: str, object_key: str) -> bool:
    prefix = f"datasets/{dataset_id}/"
    basename = object_key.removeprefix(prefix)
    return (
        object_key.startswith(prefix)
        and bool(basename)
        and "/" not in basename
        and basename != "meta.json"
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _reject_nonstandard_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _ndjson_dataset_projection(payload: bytes) -> tuple[int, list[dict[str, Any]], dict[str, Any]]:
    row_count = 0
    preview_rows: list[dict[str, Any]] = []
    columns: list[str] = []
    seen_columns: set[str] = set()
    for raw_line in payload.splitlines():
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(
                raw_line.decode("utf-8"),
                parse_constant=_reject_nonstandard_json_constant,
            )
        except ValueError as exc:
            raise ValueError("invalid NDJSON dataset row") from exc
        if not isinstance(parsed, dict):
            raise ValueError("NDJSON dataset rows must be objects")
        for raw_name in parsed:
            if (
                not isinstance(raw_name, str)
                or not 1 <= len(raw_name) <= 255
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_name)
            ):
                raise ValueError("invalid NDJSON dataset column")
            if raw_name not in seen_columns:
                seen_columns.add(raw_name)
                columns.append(raw_name)
        row_count += 1
        if len(preview_rows) < 10:
            preview_rows.append(dict(parsed))
    schema = {"columns": [{"name": name, "type": "text"} for name in columns]}
    return row_count, preview_rows, schema


def _trusted_local_fs_artifact_grants_enabled() -> bool:
    value = (
        str(_runtime_env.get("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS") or "").strip().lower()
    )
    return value in {"1", "true", "yes", "on"}


def _artifact_grant_url_is_supported(value: str) -> bool:
    scheme = str(urlparse(str(value or "")).scheme or "").lower()
    if scheme in {"http", "https"}:
        return True
    if scheme == "file":
        return _trusted_local_fs_artifact_grants_enabled()
    return False


@dataclass(slots=True)
class ArtifactHandle:
    artifact_id: str


@dataclass(slots=True)
class DatasetHandle:
    dataset_id: str


def _preview_text(body: bytes, *, limit: int = 256) -> str:
    text = bytes(body[:limit]).decode("utf-8", errors="replace")
    return "".join(char for char in text if char in "\n\t" or ord(char) >= 32).strip()
