"""Artifact and dataset retention policy and durable cleanup."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from core.application.extensions.artifact_plane_values import (
    _replay_owner_manifest,
    _valid_dataset_object_key,
)
from core.infrastructure.extension_runtime.object_storage import (
    STORAGE_CLEANUP_PAGE_SIZE,
    ObjectStorageAdapter,
    iter_storage_keys,
)

RETENTION_CLASSES: set[str] = {"ephemeral", "run_window", "audit", "persistent"}


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


def _normalize_retention(value: str) -> str:
    retention = str(value or "").strip().lower()
    if not retention:
        return "run_window"
    if retention not in RETENTION_CLASSES:
        raise ValueError(f"Unsupported retention_class '{value}'")
    return retention


class ArtifactRetentionMixin:
    _storage: ObjectStorageAdapter
    _datasets: dict[str, dict[str, Any]]
    _artifacts: dict[str, dict[str, Any]]

    def cleanup_expired(
        self,
        *,
        now: float | None = None,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Sweep expired objects from durable sidecars, including after restart."""
        ts = float(now if now is not None else time.time())
        stats = {"artifacts_expired": 0, "datasets_expired": 0, "cleanup_failed": 0}

        def _sidecar_keys(prefix: str, *, dataset: bool = False) -> Iterator[str]:
            try:
                for key in iter_storage_keys(
                    self._storage,
                    prefix,
                    page_size=STORAGE_CLEANUP_PAGE_SIZE,
                ):
                    if not dataset or (len(key.split("/")) == 3 and key.endswith("/meta.json")):
                        yield key
            except Exception:
                stats["cleanup_failed"] += 1

        def _load_sidecar(key: str) -> dict[str, Any] | None:
            try:
                raw = self._storage.get(key)
                row = json.loads(raw) if raw else None
            except Exception:
                return None
            return row if isinstance(row, dict) else None

        def _is_expired(row: dict[str, Any]) -> bool:
            owner_scope = row.get("owner_scope")
            owner_scope = dict(owner_scope) if isinstance(owner_scope, dict) else {}
            if (
                str(row.get("persistence_status") or "").strip().lower() != "preparing"
                and str(row.get("retention_class") or "").strip().lower() == "run_window"
                and str(owner_scope.get("job_id") or "").strip()
            ):
                return False
            expiry_key = (
                "preparation_expires_at"
                if row.get("persistence_status") == "preparing"
                else "expires_at"
            )
            expires_at = row.get(expiry_key)
            if expires_at is None:
                return False
            try:
                return float(expires_at) <= ts
            except (TypeError, ValueError):
                return False

        def _delete_verified(key: str) -> bool:
            try:
                abort_fn = getattr(self._storage, "abort_incomplete_uploads", None)
                if callable(abort_fn):
                    abort_fn(key)
                self._storage.delete(key)
                return self._storage.stat(key) is None
            except Exception:
                return False

        for metadata_key in _sidecar_keys("artifacts/_metadata/"):
            artifact_id = metadata_key.removeprefix("artifacts/_metadata/").removesuffix(".json")
            row = _load_sidecar(metadata_key)
            if (
                not artifact_id
                or "/" in artifact_id
                or not isinstance(row, dict)
                or str(row.get("artifact_id") or "").strip() != artifact_id
            ):
                stats["cleanup_failed"] += 1
                continue
            object_key = str(row.get("object_key") or "").strip()
            if not object_key.startswith(f"artifacts/{artifact_id}/"):
                stats["cleanup_failed"] += 1
                continue
            if not _is_expired(row):
                continue
            if dry_run:
                stats["artifacts_expired"] += 1
                continue
            claim_key = f"_replay_owners/artifact/{artifact_id}.json"
            manifest = _replay_owner_manifest(row, kind="artifact")
            from .artifact_presentation import presentation_key

            cleanup_keys = [object_key, claim_key, presentation_key(artifact_id)]
            if manifest is not None:
                cleanup_keys.append(manifest[0])
            if not all(_delete_verified(key) for key in cleanup_keys):
                stats["cleanup_failed"] += 1
                continue
            if not _delete_verified(metadata_key):
                stats["cleanup_failed"] += 1
                continue
            self._artifacts.pop(artifact_id, None)
            stats["artifacts_expired"] += 1

        for metadata_key in _sidecar_keys("datasets/", dataset=True):
            dataset_id = metadata_key.split("/")[1]
            row = _load_sidecar(metadata_key)
            if (
                not dataset_id
                or not isinstance(row, dict)
                or str(row.get("dataset_id") or "").strip() != dataset_id
            ):
                stats["cleanup_failed"] += 1
                continue
            object_key = str(row.get("object_key") or "").strip()
            if not _valid_dataset_object_key(dataset_id, object_key):
                stats["cleanup_failed"] += 1
                continue
            if not _is_expired(row):
                continue
            if dry_run:
                stats["datasets_expired"] += 1
                continue
            deletion_ok = _delete_verified(object_key)
            try:
                for representation_key in iter_storage_keys(
                    self._storage,
                    f"datasets/{dataset_id}/representations/",
                    page_size=STORAGE_CLEANUP_PAGE_SIZE,
                ):
                    deletion_ok = _delete_verified(representation_key) and deletion_ok
            except Exception:
                deletion_ok = False
            deletion_ok = (
                _delete_verified(f"_replay_owners/dataset/{dataset_id}.json") and deletion_ok
            )
            manifest = _replay_owner_manifest(row, kind="dataset")
            if manifest is not None:
                deletion_ok = _delete_verified(manifest[0]) and deletion_ok
            if not deletion_ok:
                stats["cleanup_failed"] += 1
                continue
            if not _delete_verified(metadata_key):
                stats["cleanup_failed"] += 1
                continue
            self._datasets.pop(dataset_id, None)
            stats["datasets_expired"] += 1

        return stats
