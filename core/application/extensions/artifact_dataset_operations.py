"""Dataset persistence, grants and representations."""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from typing import Any, Protocol

from core.application.extensions.artifact_plane_values import (
    DEFAULT_ARTIFACT_UPLOAD_FINALIZE_MAX_SIZE_BYTES,
    PREPARING_RESERVATION_TTL_SECONDS,
    DatasetHandle,
    _artifact_grant_url_is_supported,
    _ndjson_dataset_projection,
    _new_id,
    _replay_owner_manifest,
)
from core.application.extensions.artifact_retention import (
    _normalize_retention,
    _owner_retention_expiry,
)
from core.infrastructure.extension_runtime.object_storage import (
    APPLICATION_JSON_VALUE,
    APPLICATION_NDJSON_VALUE,
    ObjectStorageAdapter,
    _positive_int,
)


class _PersistOwnerManifest(Protocol):
    def __call__(self, row: dict[str, Any], *, kind: str) -> str | None: ...


class DatasetOperationsMixin:
    _storage: ObjectStorageAdapter
    _datasets: dict[str, dict[str, Any]]
    _persist_owner_manifest: _PersistOwnerManifest

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
        now = time.time()
        dataset_id = _new_id("dst")
        object_key = f"datasets/{dataset_id}/rows.json"
        metadata_key = f"datasets/{dataset_id}/meta.json"
        body = json.dumps(rows or [], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        reservation = {
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
            "size_bytes": 0,
            "sha256": "",
            "preview_rows": list((rows or [])[:10]),
            "persistence_status": "preparing",
            "preparation_expires_at": now + PREPARING_RESERVATION_TTL_SECONDS,
            "created_at": now,
            "updated_at": now,
        }
        # Persist a recovery pointer before the body. If the process or final
        # sidecar write fails, a restarted sweeper can still find the exact body.
        manifest = _replay_owner_manifest(reservation, kind="dataset")
        manifest_key = manifest[0] if manifest is not None else None
        try:
            self._storage.put(
                metadata_key,
                json.dumps(reservation, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
            manifest_key = self._persist_owner_manifest(reservation, kind="dataset")
            stored = self._storage.put(object_key, body, content_type=APPLICATION_JSON_VALUE)
            meta = dict(reservation)
            meta.update(
                {
                    "size_bytes": int(stored.get("size_bytes") or 0),
                    "sha256": str(stored.get("sha256") or "").strip(),
                    "persistence_status": "available",
                    "updated_at": time.time(),
                }
            )
            meta.pop("preparation_expires_at", None)
            self._storage.put(
                metadata_key,
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
        except Exception:
            body_absent = False
            try:
                deleted = bool(self._storage.delete(object_key))
                body_absent = deleted or self._storage.stat(object_key) is None
            except Exception:
                body_absent = False
            if body_absent:
                with contextlib.suppress(Exception):
                    self._storage.delete(metadata_key)
                if manifest_key:
                    with contextlib.suppress(Exception):
                        self._storage.delete(manifest_key)
            self._datasets.pop(dataset_id, None)
            raise
        self._datasets[dataset_id] = meta
        return DatasetHandle(dataset_id=dataset_id), dict(meta)

    def create_dataset_grant(
        self,
        *,
        dataset_id: str | None = None,
        owner_scope: dict[str, Any],
        run_id: str,
        step_id: str,
        schema: dict[str, Any] | None = None,
        content_type: str = APPLICATION_JSON_VALUE,
        retention_class: str = "run_window",
    ) -> tuple[DatasetHandle, dict[str, Any]]:
        """Create dataset metadata without writing rows yet.

        This is the dataset twin of ``create_artifact(body=None)`` for extension
        output grants. The extension receives a write grant for ``rows.json`` and
        the host finalizes metadata after upload.
        """
        retention = _normalize_retention(retention_class)
        now = time.time()
        dataset_id = str(dataset_id or "").strip() or _new_id("dst")
        if dataset_id in self._datasets:
            raise ValueError("dataset_id already exists")
        object_key = f"datasets/{dataset_id}/rows.json"
        resolved_content_type = str(content_type or APPLICATION_JSON_VALUE).strip()
        meta = {
            "dataset_id": dataset_id,
            "owner_scope": dict(owner_scope or {}),
            "run_id": str(run_id or "").strip(),
            "step_id": str(step_id or "").strip(),
            "retention_class": retention,
            "expires_at": _owner_retention_expiry(retention, now, owner_scope),
            "expired": False,
            "object_key": object_key,
            "content_type": resolved_content_type or APPLICATION_JSON_VALUE,
            "schema": dict(schema or {}),
            "row_count": 0,
            "size_bytes": 0,
            "sha256": "",
            "preview_rows": [],
            "created_at": now,
            "updated_at": now,
        }
        metadata_key = f"datasets/{dataset_id}/meta.json"
        manifest = _replay_owner_manifest(meta, kind="dataset")
        try:
            self._storage.put(
                metadata_key,
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
            self._persist_owner_manifest(meta, kind="dataset")
        except Exception:
            with contextlib.suppress(Exception):
                self._storage.delete(metadata_key)
            if manifest is not None:
                with contextlib.suppress(Exception):
                    self._storage.delete(manifest[0])
            raise
        self._datasets[dataset_id] = meta
        return DatasetHandle(dataset_id=dataset_id), dict(meta)

    def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        dataset_id = str(dataset_id or "").strip()
        row = self._datasets.get(dataset_id)
        if row is None:
            # Cache miss — attempt sidecar load so lookups survive process restarts.
            try:
                raw = self._storage.get(f"datasets/{dataset_id}/meta.json")
                if raw:
                    candidate = json.loads(raw)
                    if (
                        isinstance(candidate, dict)
                        and candidate.get("persistence_status") != "preparing"
                    ):
                        row = candidate
                        self._datasets[dataset_id] = row
            except Exception:
                pass
        return dict(row) if row else None

    def delete_dataset(self, dataset_id: str) -> bool:
        """Remove an unpublished dataset created during a failed operation."""
        dataset_id = str(dataset_id or "").strip()
        row = self.get_dataset(dataset_id)
        if not isinstance(row, dict):
            return False
        try:
            object_key = str(row.get("object_key") or "").strip()
            if object_key:
                self._storage.delete(object_key)
                if self._storage.stat(object_key) is not None:
                    return False
            representation_key = f"datasets/{dataset_id}/representations/rows.ndjson"
            self._storage.delete(representation_key)
            self._storage.delete(f"{representation_key}.json")
            if self._storage.stat(representation_key) is not None:
                return False
            if self._storage.stat(f"{representation_key}.json") is not None:
                return False
            claim_key = f"_replay_owners/dataset/{dataset_id}.json"
            self._storage.delete(claim_key)
            if self._storage.stat(claim_key) is not None:
                return False
            manifest = _replay_owner_manifest(row, kind="dataset")
            if manifest is not None:
                self._storage.delete(manifest[0])
                if self._storage.stat(manifest[0]) is not None:
                    return False
            metadata_key = f"datasets/{dataset_id}/meta.json"
            self._storage.delete(metadata_key)
            if self._storage.stat(metadata_key) is not None:
                return False
        except Exception:
            return False
        self._datasets.pop(dataset_id, None)
        return True

    def dataset_presigned_url(self, dataset_id: str, *, method: str, ttl_seconds: int = 600) -> str:
        row = self.get_dataset(dataset_id)
        if not row:
            return ""
        safe_method = str(method or "GET").strip().upper()
        object_key = str(row.get("object_key") or "").strip()
        if not object_key:
            return ""
        unavailable = (
            str(row.get("upload_status") or "").strip().lower() == "rejected"
            or str(row.get("upload_error") or "").strip()
            or not str(row.get("sha256") or "").strip()
        )
        if safe_method == "GET" and unavailable:
            return ""
        if safe_method == "PUT":
            if (
                str(row.get("upload_status") or "").strip().lower() == "rejected"
                or str(row.get("upload_error") or "").strip()
                or str(row.get("sha256") or "").strip()
            ):
                return ""
            stat_fn = getattr(self._storage, "stat", None)
            if callable(stat_fn):
                stat = stat_fn(object_key)
                if isinstance(stat, dict):
                    return ""
        url = self._storage.presign(
            object_key,
            method=safe_method,
            ttl_seconds=ttl_seconds,
        )
        if not _artifact_grant_url_is_supported(url):
            return ""
        return url

    def materialize_dataset_representation(
        self,
        dataset_id: str,
        *,
        content_type: str,
        max_size_bytes: int,
    ) -> dict[str, Any]:
        """Materialize a bounded alternate encoding without changing the dataset."""
        row = self.get_dataset(dataset_id)
        if not row:
            raise ValueError("dataset representation source is unavailable")
        from core.infrastructure.extension_runtime.dataset_representations import (
            materialize_dataset_representation,
        )

        return materialize_dataset_representation(
            self._storage,
            row,
            content_type=content_type,
            max_size_bytes=max_size_bytes,
        )

    def dataset_representation_presigned_url(
        self,
        dataset_id: str,
        *,
        object_key: str,
        method: str = "GET",
        ttl_seconds: int = 600,
    ) -> str:
        """Presign a derived representation bound to the logical dataset."""
        normalized_id = str(dataset_id or "").strip()
        normalized_key = str(object_key or "").strip()
        if (
            not self.get_dataset(normalized_id)
            or str(method or "GET").strip().upper() != "GET"
            or normalized_key != f"datasets/{normalized_id}/representations/rows.ndjson"
            or not isinstance(self._storage.stat(normalized_key), dict)
        ):
            return ""
        url = self._storage.presign(
            normalized_key,
            method="GET",
            ttl_seconds=ttl_seconds,
        )
        if not _artifact_grant_url_is_supported(url):
            return ""
        return url

    def dataset_upload_stat(self, dataset_id: str) -> dict[str, Any] | None:
        row = self.get_dataset(dataset_id)
        if not row:
            return None
        object_key = str(row.get("object_key") or "").strip()
        if not object_key:
            return None
        stat_fn = getattr(self._storage, "stat", None)
        if not callable(stat_fn):
            return None
        stat = stat_fn(object_key)
        if not isinstance(stat, dict):
            return None
        return dict(stat)

    def finalize_dataset_upload(
        self,
        dataset_id: str,
        *,
        content_type: str | None = None,
        max_size_bytes: int | None = None,
    ) -> dict[str, Any] | None:
        """Refresh dataset metadata after a direct object-store upload."""
        row = self.get_dataset(dataset_id)
        if not row:
            return None
        object_key = str(row.get("object_key") or "").strip()
        if not object_key:
            return None
        max_size = _positive_int(max_size_bytes) or DEFAULT_ARTIFACT_UPLOAD_FINALIZE_MAX_SIZE_BYTES
        stat_fn = getattr(self._storage, "stat", None)
        if not callable(stat_fn):
            raise RuntimeError(
                "Dataset storage adapter must support stat() before upload finalization"
            )
        stat = stat_fn(object_key)
        if stat is None:
            return None
        stat_size = _positive_int(stat.get("size_bytes"))
        if stat_size is None:
            return None
        if stat_size > max_size:
            return self._reject_dataset_upload(
                row,
                dataset_id=dataset_id,
                object_key=object_key,
                size_bytes=stat_size,
                error_code="dataset_size_limit_exceeded",
            )
        body = self._storage.get(object_key)
        if body is None:
            return None
        payload = bytes(body)
        if len(payload) > max_size:
            return self._reject_dataset_upload(
                row,
                dataset_id=dataset_id,
                object_key=object_key,
                size_bytes=len(payload),
                error_code="dataset_size_limit_exceeded",
            )
        resolved_content_type = (
            str(content_type or row.get("content_type") or APPLICATION_JSON_VALUE).strip()
            or APPLICATION_JSON_VALUE
        )
        preview_rows: list[Any] = []
        row_count = 0
        inferred_schema: dict[str, Any] | None = None
        if resolved_content_type == APPLICATION_NDJSON_VALUE:
            try:
                row_count, preview_rows, inferred_schema = _ndjson_dataset_projection(payload)
            except ValueError:
                return self._reject_dataset_upload(
                    row,
                    dataset_id=dataset_id,
                    object_key=object_key,
                    size_bytes=len(payload),
                    error_code="dataset_content_invalid",
                )
        else:
            with contextlib.suppress(Exception):
                parsed = json.loads(payload.decode("utf-8"))
                if isinstance(parsed, list):
                    row_count = len(parsed)
                    preview_rows = list(parsed[:10])
        updated = dict(row)
        updated["content_type"] = resolved_content_type
        updated["size_bytes"] = len(payload)
        updated["sha256"] = hashlib.sha256(payload).hexdigest()
        updated["row_count"] = row_count
        updated["preview_rows"] = preview_rows
        if inferred_schema is not None:
            updated["schema"] = inferred_schema
        updated["updated_at"] = time.time()
        self._datasets[str(updated.get("dataset_id") or dataset_id)] = updated
        with contextlib.suppress(Exception):
            self._storage.put(
                f"datasets/{dataset_id}/meta.json",
                json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
        return dict(updated)

    def _reject_dataset_upload(
        self,
        row: dict[str, Any],
        *,
        dataset_id: str,
        object_key: str,
        size_bytes: int,
        error_code: str,
    ) -> dict[str, Any]:
        deleted = self._storage.delete(object_key)
        updated = dict(row)
        updated["size_bytes"] = int(size_bytes)
        updated["sha256"] = ""
        updated["row_count"] = 0
        updated["preview_rows"] = []
        updated["upload_status"] = "rejected"
        updated["upload_error"] = error_code
        updated["object_deleted"] = bool(deleted)
        updated["updated_at"] = time.time()
        self._datasets[str(updated.get("dataset_id") or dataset_id)] = updated
        with contextlib.suppress(Exception):
            self._storage.put(
                f"datasets/{dataset_id}/meta.json",
                json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
        return dict(updated)

    def failed_items_to_dataset(
        self,
        *,
        owner_scope: dict[str, Any],
        run_id: str,
        step_id: str,
        failed_items: list[dict[str, Any]],
    ) -> DatasetHandle | None:
        if not failed_items:
            return None
        handle, _meta = self.create_dataset(
            owner_scope=owner_scope,
            run_id=run_id,
            step_id=step_id,
            schema={"columns": ["index", "item", "error"]},
            rows=failed_items,
            retention_class="run_window",
        )
        return handle
