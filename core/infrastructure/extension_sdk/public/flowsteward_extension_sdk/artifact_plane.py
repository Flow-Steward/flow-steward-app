"""SDK artifact and dataset metadata plane over object storage."""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .artifact_storage import InMemoryObjectStorage, ObjectStorageAdapter, create_storage_adapter
from .artifact_values import APPLICATION_JSON_VALUE, OCTET_STREAM_VALUE, _safe_artifact_filename

RETENTION_CLASSES = {"ephemeral", "run_window", "audit", "persistent"}


def _artifact_metadata_key(artifact_id: str) -> str:
    return f"artifacts/_metadata/{str(artifact_id or '').strip()}.json"


@dataclass(slots=True)
class ArtifactHandle:
    artifact_id: str


@dataclass(slots=True)
class DatasetHandle:
    dataset_id: str


class ArtifactDatasetPlane:
    def __init__(self, *, storage: ObjectStorageAdapter | None = None) -> None:
        self._storage = storage or InMemoryObjectStorage()
        self._artifacts: dict[str, dict[str, Any]] = {}
        self._datasets: dict[str, dict[str, Any]] = {}

    def create_artifact(
        self,
        *,
        owner_scope: dict[str, Any],
        run_id: str,
        step_id: str,
        filename: str,
        content_type: str,
        retention_class: str = "run_window",
        body: bytes | None = None,
    ) -> tuple[ArtifactHandle, dict[str, Any]]:
        retention = _normalize_retention(retention_class)
        artifact_id = _new_id("art")
        safe_filename = _safe_artifact_filename(filename)
        object_key = f"artifacts/{artifact_id}/{safe_filename}"
        now = time.time()
        content_type_value = str(content_type or OCTET_STREAM_VALUE).strip() or OCTET_STREAM_VALUE
        meta = {
            "artifact_id": artifact_id,
            "owner_scope": dict(owner_scope or {}),
            "run_id": str(run_id or "").strip(),
            "step_id": str(step_id or "").strip(),
            "filename": safe_filename,
            "content_type": content_type_value,
            "retention_class": retention,
            "expires_at": _owner_retention_expiry(retention, now, owner_scope),
            "expired": False,
            "object_key": object_key,
            "size_bytes": 0,
            "sha256": "",
            "preview_text": "",
            "created_at": now,
            "updated_at": now,
        }
        if body is not None:
            stored = self._storage.put(
                object_key,
                bytes(body),
                content_type=content_type_value,
            )
            meta["size_bytes"] = int(stored.get("size_bytes") or 0)
            meta["sha256"] = str(stored.get("sha256") or "").strip()
            meta["preview_text"] = _preview_text(body)
        self._artifacts[artifact_id] = meta
        with contextlib.suppress(Exception):
            self._storage.put(
                _artifact_metadata_key(artifact_id),
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
        return ArtifactHandle(artifact_id=artifact_id), dict(meta)

    def create_dataset(
        self,
        *,
        owner_scope: dict[str, Any],
        run_id: str,
        step_id: str,
        schema: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
        retention_class: str = "run_window",
    ) -> tuple[DatasetHandle, dict[str, Any]]:
        retention = _normalize_retention(retention_class)
        dataset_id = _new_id("dst")
        object_key = f"datasets/{dataset_id}/rows.json"
        body = json.dumps(rows or [], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        stored = self._storage.put(object_key, body, content_type=APPLICATION_JSON_VALUE)
        now = time.time()
        meta = {
            "dataset_id": dataset_id,
            "owner_scope": dict(owner_scope or {}),
            "run_id": str(run_id or "").strip(),
            "step_id": str(step_id or "").strip(),
            "retention_class": retention,
            "expires_at": _owner_retention_expiry(retention, now, owner_scope),
            "expired": False,
            "object_key": object_key,
            "schema": dict(schema or {}),
            "row_count": len(rows or []),
            "size_bytes": int(stored.get("size_bytes") or 0),
            "sha256": str(stored.get("sha256") or "").strip(),
            "preview_rows": list((rows or [])[:10]),
            "created_at": now,
            "updated_at": now,
        }
        self._datasets[dataset_id] = meta
        with contextlib.suppress(Exception):
            self._storage.put(
                f"datasets/{dataset_id}/meta.json",
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
        return DatasetHandle(dataset_id=dataset_id), dict(meta)

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        artifact_id = str(artifact_id or "").strip()
        row = self._artifacts.get(artifact_id)
        if row is None:
            with contextlib.suppress(Exception):
                raw = self._storage.get(_artifact_metadata_key(artifact_id))
                candidate = json.loads(raw) if raw else None
                if (
                    isinstance(candidate, dict)
                    and str(candidate.get("artifact_id") or "").strip() == artifact_id
                    and str(candidate.get("object_key") or "").startswith(
                        f"artifacts/{artifact_id}/"
                    )
                ):
                    row = candidate
                    self._artifacts[artifact_id] = row
        return dict(row) if row else None

    def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        dataset_id = str(dataset_id or "").strip()
        row = self._datasets.get(dataset_id)
        if row is None:
            with contextlib.suppress(Exception):
                raw = self._storage.get(f"datasets/{dataset_id}/meta.json")
                if raw:
                    row = json.loads(raw)
                    self._datasets[dataset_id] = row
        return dict(row) if row else None

    def artifact_presigned_url(
        self,
        artifact_id: str,
        *,
        method: str,
        ttl_seconds: int = 600,
    ) -> str:
        row = self.get_artifact(artifact_id)
        if not row:
            return ""
        return self._storage.presign(
            str(row.get("object_key") or ""),
            method=method,
            ttl_seconds=ttl_seconds,
        )


def artifact_plane_from_payload(payload: dict[str, Any] | None) -> ArtifactDatasetPlane:
    root = payload if isinstance(payload, dict) else {}
    raw_context = root.get("runtime_context")
    context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
    storage_config = context.get("artifact_storage")
    return ArtifactDatasetPlane(
        storage=create_storage_adapter(storage_config if isinstance(storage_config, dict) else None)
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _normalize_retention(value: str) -> str:
    retention = str(value or "").strip().lower()
    if not retention:
        return "run_window"
    if retention not in RETENTION_CLASSES:
        raise ValueError(f"Unsupported retention_class '{value}'")
    return retention


def _retention_expiry(retention_class: str, now: float) -> float | None:
    if retention_class == "ephemeral":
        return now + 3600
    if retention_class == "run_window":
        return now + 24 * 3600
    if retention_class == "audit":
        return now + 30 * 24 * 3600
    return None


def _owner_retention_expiry(
    retention_class: str,
    now: float,
    owner_scope: dict[str, Any] | None,
) -> float | None:
    if retention_class == "run_window" and str((owner_scope or {}).get("job_id") or "").strip():
        return None
    return _retention_expiry(retention_class, now)


def _preview_text(body: bytes, *, limit: int = 256) -> str:
    text = bytes(body[:limit]).decode("utf-8", errors="replace")
    return "".join(char for char in text if char in "\n\t" or ord(char) >= 32).strip()
