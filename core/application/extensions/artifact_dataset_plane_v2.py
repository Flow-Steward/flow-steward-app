"""Artifact persistence and replay ownership composition."""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import urlparse

from core.application.extensions.artifact_dataset_operations import DatasetOperationsMixin
from core.application.extensions.artifact_plane_values import (
    DEFAULT_ARTIFACT_UPLOAD_FINALIZE_MAX_SIZE_BYTES,
    PREPARING_RESERVATION_TTL_SECONDS,
    ArtifactHandle,
    _artifact_grant_url_is_supported,
    _new_id,
    _preview_text,
    _replay_owner_manifest,
    artifact_metadata_key,
)
from core.application.extensions.artifact_retention import (
    ArtifactRetentionMixin,
    _normalize_retention,
    _owner_retention_expiry,
)
from core.infrastructure.extension_runtime.object_storage import (
    _MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER,
    APPLICATION_JSON_VALUE,
    OCTET_STREAM_VALUE,
    InMemoryObjectStorage,
    ObjectStorageAdapter,
    _opened_object_stream,
    _positive_int,
    _safe_artifact_filename,
)


class ArtifactDatasetPlane(DatasetOperationsMixin, ArtifactRetentionMixin):
    """Handle-based metadata plane. Blob bodies stay in object storage."""

    def __init__(self, *, storage: ObjectStorageAdapter | None = None):
        self._storage: ObjectStorageAdapter = storage or InMemoryObjectStorage()
        self._artifacts: dict[str, dict[str, Any]] = {}
        self._datasets: dict[str, dict[str, Any]] = {}
        self._ownership_lock = threading.RLock()

    def _persist_owner_manifest(self, row: dict[str, Any], *, kind: str) -> str | None:
        manifest = _replay_owner_manifest(row, kind=kind)
        if manifest is None:
            return None
        key, payload = manifest
        self._storage.put(
            key,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            content_type=APPLICATION_JSON_VALUE,
        )
        return key

    def create_artifact(
        self,
        *,
        artifact_id: str | None = None,
        owner_scope: dict[str, Any],
        run_id: str,
        step_id: str,
        filename: str,
        content_type: str,
        retention_class: str = "run_window",
        body: bytes | None = None,
    ) -> tuple[ArtifactHandle, dict[str, Any]]:
        retention = _normalize_retention(retention_class)
        now = time.time()
        artifact_id = str(artifact_id or "").strip() or _new_id("art")
        if artifact_id in self._artifacts:
            raise ValueError("artifact_id already exists")
        safe_filename = _safe_artifact_filename(filename)
        object_key = f"artifacts/{artifact_id}/{safe_filename}"

        meta = {
            "artifact_id": artifact_id,
            "owner_scope": dict(owner_scope or {}),
            "run_id": str(run_id or "").strip(),
            "step_id": str(step_id or "").strip(),
            "filename": safe_filename,
            "content_type": str(content_type or OCTET_STREAM_VALUE).strip(),
            "retention_class": retention,
            "expires_at": _owner_retention_expiry(retention, now, owner_scope),
            "expired": False,
            "object_key": object_key,
            "size_bytes": 0,
            "sha256": "",
            "preview_text": "",
            "persistence_status": "preparing",
            "preparation_expires_at": now + PREPARING_RESERVATION_TTL_SECONDS,
            "created_at": now,
            "updated_at": now,
        }
        manifest = _replay_owner_manifest(meta, kind="artifact")
        manifest_key = manifest[0] if manifest is not None else None
        try:
            # Persist cleanup authority before the body side effect.
            self._storage.put(
                artifact_metadata_key(artifact_id),
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
            manifest_key = self._persist_owner_manifest(meta, kind="artifact")
            if body is not None:
                resolved_content_type = (
                    str(meta.get("content_type") or OCTET_STREAM_VALUE).strip()
                    or OCTET_STREAM_VALUE
                )
                stored = self._storage.put(
                    object_key,
                    bytes(body),
                    content_type=resolved_content_type,
                )
                meta["size_bytes"] = int(stored.get("size_bytes") or 0)
                meta["sha256"] = str(stored.get("sha256") or "").strip()
                meta["preview_text"] = _preview_text(body)
                meta["persistence_status"] = "available"
                meta.pop("preparation_expires_at", None)
                meta["updated_at"] = time.time()
                self._storage.put(
                    artifact_metadata_key(artifact_id),
                    json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    content_type=APPLICATION_JSON_VALUE,
                )
        except Exception:
            body_absent = body is None
            if body is not None:
                try:
                    deleted = bool(self._storage.delete(object_key))
                    body_absent = deleted or self._storage.stat(object_key) is None
                except Exception:
                    body_absent = False
            if body_absent:
                with contextlib.suppress(Exception):
                    self._storage.delete(artifact_metadata_key(artifact_id))
                if manifest_key:
                    with contextlib.suppress(Exception):
                        self._storage.delete(manifest_key)
            raise
        self._artifacts[artifact_id] = meta
        return ArtifactHandle(artifact_id=artifact_id), dict(meta)

    def create_artifact_from_stream(
        self,
        *,
        artifact_id: str | None = None,
        owner_scope: dict[str, Any],
        run_id: str,
        step_id: str,
        filename: str,
        content_type: str,
        chunks: Iterable[bytes],
        max_size_bytes: int,
        retention_class: str = "run_window",
        allow_in_memory: bool = True,
    ) -> tuple[ArtifactHandle, dict[str, Any]]:
        if not allow_in_memory and isinstance(self._storage, InMemoryObjectStorage):
            raise RuntimeError("public_attachment_memory_storage_unsupported")
        retention = _normalize_retention(retention_class)
        now = time.time()
        artifact_id = str(artifact_id or "").strip() or _new_id("art")
        if artifact_id in self._artifacts:
            raise ValueError("artifact_id already exists")
        safe_filename = _safe_artifact_filename(filename)
        object_key = f"artifacts/{artifact_id}/{safe_filename}"
        resolved_content_type = (
            str(content_type or OCTET_STREAM_VALUE).strip() or OCTET_STREAM_VALUE
        )
        meta = {
            "artifact_id": artifact_id,
            "owner_scope": dict(owner_scope or {}),
            "run_id": str(run_id or "").strip(),
            "step_id": str(step_id or "").strip(),
            "filename": safe_filename,
            "content_type": resolved_content_type,
            "retention_class": retention,
            "expires_at": _owner_retention_expiry(retention, now, owner_scope),
            "expired": False,
            "object_key": object_key,
            "size_bytes": 0,
            "sha256": "",
            "preview_text": "",
            "persistence_status": "preparing",
            "preparation_expires_at": now + PREPARING_RESERVATION_TTL_SECONDS,
            "created_at": now,
            "updated_at": now,
        }
        manifest = _replay_owner_manifest(meta, kind="artifact")
        manifest_key = manifest[0] if manifest is not None else None
        try:
            # Persist the cleanup pointer before the object-store side effect.
            # A process crash during put_stream can then recover both the final
            # object and any S3 multipart upload by its exact object key.
            self._storage.put(
                artifact_metadata_key(artifact_id),
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
            manifest_key = self._persist_owner_manifest(meta, kind="artifact")
            stored = self._storage.put_stream(
                object_key,
                chunks,
                content_type=resolved_content_type,
                max_size_bytes=max_size_bytes,
            )
            stored_size = int(stored.get("size_bytes") or 0)
            stored_sha = str(stored.get("sha256") or "").strip()
            stat_fn = getattr(self._storage, "stat", None)
            if not callable(stat_fn):
                raise RuntimeError("Artifact storage adapter must support stat()")
            stat = stat_fn(object_key)
            stat_size = stat.get("size_bytes") if isinstance(stat, dict) else None
            if (
                not isinstance(stat, dict)
                or int(stat_size if stat_size is not None else -1) != stored_size
            ):
                raise RuntimeError("artifact_integrity_error")
            updated = dict(meta)
            updated["size_bytes"] = stored_size
            updated["sha256"] = stored_sha
            updated["persistence_status"] = "available"
            updated.pop("preparation_expires_at", None)
            updated["updated_at"] = time.time()
            self._storage.put(
                artifact_metadata_key(artifact_id),
                json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
            self._artifacts[artifact_id] = updated
            return ArtifactHandle(artifact_id=artifact_id), dict(updated)
        except Exception:
            body_absent = False
            try:
                abort_fn = getattr(self._storage, "abort_incomplete_uploads", None)
                if callable(abort_fn):
                    abort_fn(object_key)
                deleted = bool(self._storage.delete(object_key))
                stat_fn = getattr(self._storage, "stat", None)
                body_absent = deleted or (callable(stat_fn) and stat_fn(object_key) is None)
            except Exception:
                body_absent = False
            # The sidecar is the durable recovery pointer. Never remove it while
            # an attachment body (or an incomplete multipart upload) may remain.
            if body_absent:
                with contextlib.suppress(Exception):
                    self._storage.delete(artifact_metadata_key(artifact_id))
                if manifest_key:
                    with contextlib.suppress(Exception):
                        self._storage.delete(manifest_key)
            self._artifacts.pop(artifact_id, None)
            raise

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        artifact_id = str(artifact_id or "").strip()
        row = self._artifacts.get(artifact_id)
        if row is None:
            try:
                raw = self._storage.get(artifact_metadata_key(artifact_id))
                candidate = json.loads(raw) if raw else None
            except Exception:
                candidate = None
            if (
                isinstance(candidate, dict)
                and str(candidate.get("artifact_id") or "").strip() == artifact_id
                and str(candidate.get("object_key") or "").startswith(f"artifacts/{artifact_id}/")
            ):
                row = candidate
                self._artifacts[artifact_id] = row
        return dict(row) if row else None

    def transfer_child_artifact_to_parent(
        self,
        artifact_id: str,
        *,
        project_id: str,
        child_job_id: str,
        parent_job_id: str,
    ) -> dict[str, Any]:
        """Adopt a completed child artifact without copying or reading its body.

        The parent becomes the retention owner while the durable child remains
        recorded as the producer. Repeating the same transfer is idempotent;
        cross-project or cross-job adoption fails closed.
        """

        clean_artifact_id = str(artifact_id or "").strip()
        clean_project_id = str(project_id or "").strip()
        clean_child_job_id = str(child_job_id or "").strip()
        clean_parent_job_id = str(parent_job_id or "").strip()
        if not all(
            (
                clean_artifact_id,
                clean_project_id,
                clean_child_job_id,
                clean_parent_job_id,
            )
        ):
            raise ValueError("artifact transfer identity is required")
        if clean_child_job_id == clean_parent_job_id:
            raise ValueError("child and parent jobs must be different")

        with self._ownership_lock:
            row = self.get_artifact(clean_artifact_id)
            if not isinstance(row, dict):
                raise ValueError("child artifact does not exist")
            owner = row.get("owner_scope")
            owner = dict(owner) if isinstance(owner, dict) else {}
            existing_project_id = str(owner.get("project_id") or "").strip()
            existing_job_id = str(owner.get("job_id") or "").strip()
            existing_producer_job_id = str(row.get("producer_job_id") or "").strip()

            if existing_project_id != clean_project_id:
                raise ValueError("child artifact belongs to another project")
            if existing_job_id == clean_parent_job_id:
                if existing_producer_job_id != clean_child_job_id:
                    raise ValueError("child artifact belongs to another job")
                return dict(row)
            if existing_job_id != clean_child_job_id:
                raise ValueError("child artifact belongs to another job")
            if existing_producer_job_id not in {"", clean_child_job_id}:
                raise ValueError("child artifact belongs to another producer")

            updated_owner = dict(owner)
            updated_owner["project_id"] = clean_project_id
            updated_owner["job_id"] = clean_parent_job_id
            updated = dict(row)
            updated["owner_scope"] = updated_owner
            updated["producer_job_id"] = clean_child_job_id
            updated["producer_run_id"] = str(row.get("run_id") or clean_child_job_id).strip()
            updated["run_id"] = clean_parent_job_id
            updated["updated_at"] = time.time()

            old_manifest = _replay_owner_manifest(row, kind="artifact")
            new_manifest = _replay_owner_manifest(updated, kind="artifact")
            if old_manifest is None or new_manifest is None:
                raise ValueError("child artifact ownership metadata is incomplete")
            old_manifest_key = old_manifest[0]
            new_manifest_key, new_manifest_payload = new_manifest
            metadata_key = artifact_metadata_key(clean_artifact_id)
            encoded_updated = json.dumps(
                updated,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

            self._storage.put(
                new_manifest_key,
                json.dumps(
                    new_manifest_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
            try:
                self._storage.put(
                    metadata_key,
                    encoded_updated,
                    content_type=APPLICATION_JSON_VALUE,
                )
            except Exception:
                with contextlib.suppress(Exception):
                    self._storage.delete(new_manifest_key)
                raise

            try:
                self._storage.delete(old_manifest_key)
                if self._storage.stat(old_manifest_key) is not None:
                    raise RuntimeError("child artifact owner manifest could not be removed")
            except Exception as exc:
                rollback_ok = True
                try:
                    self._storage.put(
                        metadata_key,
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8"),
                        content_type=APPLICATION_JSON_VALUE,
                    )
                    self._storage.delete(new_manifest_key)
                    rollback_ok = self._storage.stat(new_manifest_key) is None
                except Exception:
                    rollback_ok = False
                if not rollback_ok:
                    raise RuntimeError("child artifact ownership transfer rollback failed") from exc
                raise RuntimeError("child artifact ownership transfer failed") from exc

            self._artifacts[clean_artifact_id] = updated
            return dict(updated)

    def read_artifact_bytes(
        self,
        artifact_id: str,
        *,
        max_size_bytes: int,
    ) -> bytes | None:
        """Read one artifact body through an explicit caller-owned size bound."""
        max_size = _positive_int(max_size_bytes)
        if max_size is None:
            raise ValueError(_MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER)
        row = self.get_artifact(artifact_id)
        if not isinstance(row, dict):
            return None
        declared_size = int(row.get("size_bytes") or 0)
        if declared_size > max_size:
            raise ValueError("artifact body exceeds max_size_bytes")
        object_key = str(row.get("object_key") or "").strip()
        if not object_key:
            return None
        body = self._storage.get(object_key)
        if body is None:
            return None
        payload = bytes(body)
        if len(payload) > max_size:
            raise ValueError("artifact body exceeds max_size_bytes")
        return payload

    def delete_artifact(self, artifact_id: str) -> bool:
        """Remove an unpublished artifact created during a failed operation."""
        artifact_id = str(artifact_id or "").strip()
        row = self.get_artifact(artifact_id)
        if not isinstance(row, dict):
            return False
        try:
            object_key = str(row.get("object_key") or "")
            if object_key:
                abort_fn = getattr(self._storage, "abort_incomplete_uploads", None)
                if callable(abort_fn):
                    abort_fn(object_key)
                self._storage.delete(object_key)
                if self._storage.stat(object_key) is not None:
                    return False
            claim_key = f"_replay_owners/artifact/{artifact_id}.json"
            self._storage.delete(claim_key)
            if self._storage.stat(claim_key) is not None:
                return False
            manifest = _replay_owner_manifest(row, kind="artifact")
            if manifest is not None:
                self._storage.delete(manifest[0])
                if self._storage.stat(manifest[0]) is not None:
                    return False
            metadata_key = artifact_metadata_key(artifact_id)
            self._storage.delete(metadata_key)
            if self._storage.stat(metadata_key) is not None:
                return False
        except Exception:
            return False
        self._artifacts.pop(artifact_id, None)
        return True

    def bind_replay_owner(
        self,
        *,
        artifact_id: str | None = None,
        dataset_id: str | None = None,
        project_id: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Bind an unowned pre-launch handle to its immutable replay owner once."""
        clean_artifact_id = str(artifact_id or "").strip()
        clean_dataset_id = str(dataset_id or "").strip()
        clean_project_id = str(project_id or "").strip()
        clean_job_id = str(job_id or "").strip()
        if bool(clean_artifact_id) == bool(clean_dataset_id):
            raise ValueError("exactly one replay handle is required")
        if not clean_project_id or not clean_job_id:
            raise ValueError("replay owner project_id and job_id are required")

        kind = "artifact" if clean_artifact_id else "dataset"
        object_id = clean_artifact_id or clean_dataset_id
        row = self.get_artifact(object_id) if kind == "artifact" else self.get_dataset(object_id)
        if not isinstance(row, dict):
            raise ValueError("replay handle does not exist")
        owner = row.get("owner_scope")
        owner = dict(owner) if isinstance(owner, dict) else {}
        existing_project_id = str(owner.get("project_id") or "").strip()
        existing_job_id = str(owner.get("job_id") or "").strip()
        if existing_project_id not in {"", clean_project_id}:
            raise ValueError("replay handle belongs to another project")
        if existing_job_id not in {"", clean_job_id}:
            raise ValueError("replay handle belongs to another job")
        if existing_project_id == clean_project_id and existing_job_id == clean_job_id:
            return dict(row)

        owner["project_id"] = clean_project_id
        owner["job_id"] = clean_job_id
        updated = dict(row)
        updated["owner_scope"] = owner
        updated["updated_at"] = time.time()
        claim_key = f"_replay_owners/{kind}/{object_id}.json"
        claim = json.dumps(
            {
                "kind": kind,
                "object_id": object_id,
                "project_id": clean_project_id,
                "job_id": clean_job_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        put_if_absent = getattr(self._storage, "put_if_absent", None)
        if not callable(put_if_absent):
            raise RuntimeError("artifact storage must support atomic replay owner claims")
        if not put_if_absent(claim_key, claim, content_type=APPLICATION_JSON_VALUE):
            try:
                existing_claim = self._storage.get(claim_key)
            except Exception as exc:
                raise ValueError("replay handle ownership is already claimed") from exc
            if existing_claim != claim:
                raise ValueError("replay handle ownership is already claimed")
        self._persist_owner_manifest(updated, kind=kind)
        metadata_key = (
            artifact_metadata_key(object_id)
            if kind == "artifact"
            else f"datasets/{object_id}/meta.json"
        )
        self._storage.put(
            metadata_key,
            json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            content_type=APPLICATION_JSON_VALUE,
        )
        if kind == "artifact":
            self._artifacts[object_id] = updated
        else:
            self._datasets[object_id] = updated
        return dict(updated)

    def cleanup_output_grant_placeholder_status(self, object_id: str, *, kind: str) -> str:
        """Remove a minted output placeholder without conflating I/O errors with absence."""
        normalized_kind = str(kind or "").strip().lower()
        normalized_id = str(object_id or "").strip()
        if normalized_kind not in {"artifact", "dataset"} or not normalized_id:
            return "unknown"

        collection = "artifacts" if normalized_kind == "artifact" else "datasets"
        cache = self._artifacts if normalized_kind == "artifact" else self._datasets
        metadata_key = (
            artifact_metadata_key(normalized_id)
            if normalized_kind == "artifact"
            else f"datasets/{normalized_id}/meta.json"
        )
        row = cache.get(normalized_id)
        if row is None:
            try:
                raw = self._storage.get(metadata_key)
            except Exception:
                return "unknown"
            if raw is None:
                # Output grants persist this recovery pointer before exposing a
                # writable body. Cleanup removes it only after verifying the
                # body absent, so a definitive miss proves the grant is absent.
                return "verified_absent"
            try:
                candidate = json.loads(raw)
            except (TypeError, ValueError):
                return "unknown"
            if not isinstance(candidate, dict):
                return "unknown"
            row = candidate

        id_field = f"{normalized_kind}_id"
        object_key = str(row.get("object_key") or "").strip()
        expected_prefix = f"{collection}/{normalized_id}/"
        if str(row.get(id_field) or "").strip() != normalized_id or not object_key.startswith(
            expected_prefix
        ):
            return "unknown"
        if normalized_kind == "dataset" and object_key != f"datasets/{normalized_id}/rows.json":
            return "unknown"

        try:
            if normalized_kind == "artifact":
                abort_fn = getattr(self._storage, "abort_incomplete_uploads", None)
                if callable(abort_fn):
                    abort_fn(object_key)
            self._storage.delete(object_key)
            if self._storage.stat(object_key) is not None:
                return "unknown"
            manifest = _replay_owner_manifest(row, kind=normalized_kind)
            if manifest is not None:
                self._storage.delete(manifest[0])
                if self._storage.stat(manifest[0]) is not None:
                    return "unknown"
            self._storage.delete(metadata_key)
            if self._storage.stat(metadata_key) is not None:
                return "unknown"
        except Exception:
            return "unknown"
        cache.pop(normalized_id, None)
        return "removed"

    def artifact_presigned_url(
        self, artifact_id: str, *, method: str, ttl_seconds: int = 600
    ) -> str:
        row = self.get_artifact(artifact_id)
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

    def artifact_public_download_url(
        self,
        artifact_id: str,
        *,
        method: str = "GET",
        filename: str,
        ttl_seconds: int = 600,
    ) -> str:
        """Return a direct browser download grant for one persisted artifact."""

        if str(method or "GET").strip().upper() != "GET":
            return ""
        row = self.get_artifact(artifact_id)
        if not row:
            return ""
        object_key = str(row.get("object_key") or "").strip()
        unavailable = (
            not object_key
            or str(row.get("upload_status") or "").strip().lower() == "rejected"
            or str(row.get("upload_error") or "").strip()
            or not str(row.get("sha256") or "").strip()
        )
        if unavailable:
            return ""
        presign = getattr(self._storage, "presign_public_download", None)
        if not callable(presign):
            return ""
        url = str(
            presign(
                object_key,
                filename=str(filename or row.get("filename") or "artifact.bin"),
                ttl_seconds=ttl_seconds,
            )
            or ""
        ).strip()
        return url if str(urlparse(url).scheme or "").lower() in {"http", "https"} else ""

    def write_artifact_content(
        self,
        artifact_id: str,
        *,
        body: bytes,
        content_type: str = "",
    ) -> dict[str, Any] | None:
        """Store an extension's upload for a backend that could not presign one.

        AC17-R123. When storage can hand out a URL the extension writes straight to
        it and the platform only finalizes afterwards. When it cannot, the bytes
        arrive here instead, and the sequence has to stay the same one: put the object
        under the row's own key, then run the existing finalization so the checksum,
        size and status are computed by the code that already owns that question.

        A body is written once. The grant that authorized this is single-use on the
        extension side, but that is the extension's own bookkeeping; refusing a second
        write here is what makes it true for the platform, and it costs one read of a
        row we have already loaded.
        """
        row = self.get_artifact(artifact_id)
        if not row:
            return None
        object_key = str(row.get("object_key") or "").strip()
        if not object_key:
            return None
        if str(row.get("sha256") or "").strip():
            raise ValueError("Artifact bodies are immutable once written")
        resolved_type = (
            str(content_type or "").strip()
            or str(row.get("content_type") or "").strip()
            or "application/octet-stream"
        )
        self._storage.put(object_key, body, content_type=resolved_type)
        return self.finalize_artifact_upload(artifact_id, content_type=resolved_type)

    def artifact_local_stream(
        self,
        artifact_id: str,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> tuple[Iterator[bytes], int] | None:
        """Stream one persisted artifact when this process has to serve it itself.

        An authorized download comes back through the API whenever nobody can
        hand the browser a URL it is able to fetch. That is not a property of the
        backend: ``local_fs`` never has such a URL, and object storage has one
        only when the deployment named a public endpoint for it (AC17-R130).
        Deciding that is the caller's job — it asks for a URL first and comes here
        when none came back — so the only question answered here is whether this
        object can be read at all. ``None`` means it cannot: an incomplete row, a
        storage backend that cannot stream, or an object that is no longer there.
        The caller never sees the object key or a filesystem path.

        The object is opened here, before any response is started, so a missing
        object becomes an error rather than an empty or truncated ``200``.

        Returns the chunks together with the size those chunks will actually
        add up to, so a caller that declares a length declares the one it is
        about to send rather than the one the metadata row remembers.
        """

        stream_fn = getattr(self._storage, "stream", None)
        stat_fn = getattr(self._storage, "stat", None)
        if not callable(stream_fn) or not callable(stat_fn):
            return None
        row = self.get_artifact(artifact_id)
        if not row:
            return None
        object_key = str(row.get("object_key") or "").strip()
        unavailable = (
            not object_key
            or str(row.get("upload_status") or "").strip().lower() == "rejected"
            or str(row.get("upload_error") or "").strip()
            or not str(row.get("sha256") or "").strip()
        )
        if unavailable:
            return None
        stat = stat_fn(object_key)
        if not isinstance(stat, dict):
            return None
        size_bytes = max(0, int(stat.get("size_bytes") or 0))
        chunks = _opened_object_stream(
            stream_fn(object_key, chunk_size=max(1, int(chunk_size or 1))),
            expected_bytes=size_bytes,
        )
        return None if chunks is None else (chunks, size_bytes)

    def artifact_upload_stat(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.get_artifact(artifact_id)
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

    def finalize_artifact_upload(
        self,
        artifact_id: str,
        *,
        content_type: str | None = None,
        max_size_bytes: int | None = None,
    ) -> dict[str, Any] | None:
        """Refresh artifact metadata after a direct object-store upload."""
        row = self.get_artifact(artifact_id)
        if not row:
            return None
        object_key = str(row.get("object_key") or "").strip()
        if not object_key:
            return None
        max_size = _positive_int(max_size_bytes) or DEFAULT_ARTIFACT_UPLOAD_FINALIZE_MAX_SIZE_BYTES
        stat_fn = getattr(self._storage, "stat", None)
        if not callable(stat_fn):
            raise RuntimeError(
                "Artifact storage adapter must support stat() before upload finalization"
            )
        stat = stat_fn(object_key)
        if stat is None:
            return None
        stat_size = _positive_int(stat.get("size_bytes"))
        if stat_size is None:
            return None
        if stat_size > max_size:
            return self._reject_artifact_upload(
                row,
                artifact_id=artifact_id,
                object_key=object_key,
                size_bytes=stat_size,
                error_code="artifact_size_limit_exceeded",
            )
        body = self._storage.get(object_key)
        if body is None:
            return None
        payload = bytes(body)
        if len(payload) > max_size:
            return self._reject_artifact_upload(
                row,
                artifact_id=artifact_id,
                object_key=object_key,
                size_bytes=len(payload),
                error_code="artifact_size_limit_exceeded",
            )
        updated = dict(row)
        updated["content_type"] = (
            str(content_type or updated.get("content_type") or OCTET_STREAM_VALUE).strip()
            or OCTET_STREAM_VALUE
        )
        updated["size_bytes"] = len(payload)
        updated["sha256"] = hashlib.sha256(payload).hexdigest()
        updated["preview_text"] = _preview_text(payload)
        updated["persistence_status"] = "available"
        updated.pop("preparation_expires_at", None)
        updated["updated_at"] = time.time()
        self._storage.put(
            f"artifacts/_metadata/{artifact_id}.json",
            json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            content_type=APPLICATION_JSON_VALUE,
        )
        self._artifacts[str(updated.get("artifact_id") or artifact_id)] = updated
        return dict(updated)

    def _reject_artifact_upload(
        self,
        row: dict[str, Any],
        *,
        artifact_id: str,
        object_key: str,
        size_bytes: int,
        error_code: str,
    ) -> dict[str, Any]:
        deleted = self._storage.delete(object_key)
        updated = dict(row)
        updated["size_bytes"] = int(size_bytes)
        updated["sha256"] = ""
        updated["preview_text"] = ""
        updated["upload_status"] = "rejected"
        updated["upload_error"] = error_code
        updated["object_deleted"] = bool(deleted)
        updated["updated_at"] = time.time()
        self._artifacts[str(updated.get("artifact_id") or artifact_id)] = updated
        with contextlib.suppress(Exception):
            self._storage.put(
                f"artifacts/_metadata/{artifact_id}.json",
                json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                content_type=APPLICATION_JSON_VALUE,
            )
        return dict(updated)
