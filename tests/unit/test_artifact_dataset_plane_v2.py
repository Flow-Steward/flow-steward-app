from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import core.application.extensions.artifact_plane_values as plane_values_module
import core.application.extensions.artifact_storage as local_storage_module
import core.infrastructure.extension_runtime.object_storage as object_storage_module
from core.application.extensions import artifact_dataset_operations as dataset_operations_module
from core.application.extensions import artifact_dataset_plane_v2 as artifact_plane_module
from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.extensions.artifact_plane_values import (
    FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV,
    replay_owner_manifest_key,
)
from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
from core.application.extensions.bulk_execution import execute_bulk_operation
from core.infrastructure.extension_runtime.object_storage import (
    InMemoryObjectStorage,
    S3ObjectStorageAdapter,
)
from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.artifacts import (
    S3ObjectStorageAdapter as PublicSdkS3ObjectStorageAdapter,
)
from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage


def test_artifact_body_is_stored_outside_metadata_and_exposes_handle() -> None:
    plane = ArtifactDatasetPlane()
    handle, meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.txt",
        content_type="text/plain",
        retention_class="ephemeral",
        body=b"hello world",
    )
    assert handle.artifact_id.startswith("art_")
    assert meta["size_bytes"] == 11
    assert "hello world" in meta["preview_text"]
    fetched = plane.get_artifact(handle.artifact_id)
    assert fetched is not None
    assert fetched["object_key"].startswith("artifacts/")
    assert fetched["retention_class"] == "ephemeral"
    assert fetched["expires_at"] is not None


def test_runtime_storage_factory_passes_browser_reachable_s3_endpoint() -> None:
    captured: dict[str, object] = {}
    adapter = object()

    result = build_artifact_storage(
        create_storage_adapter=lambda config: (captured.update(config), adapter)[1],
        artifact_plane_factory=lambda *, storage: storage,
        environ={
            "ARTIFACT_STORAGE_BACKEND": "s3",
            "ARTIFACT_S3_ENDPOINT_URL": "http://seaweedfs:8333",
            "ARTIFACT_S3_PUBLIC_ENDPOINT_URL": "https://objects.example.test",
            "ARTIFACT_S3_BUCKET": "artifacts",
        },
    )

    assert result is adapter
    assert captured["endpoint_url"] == "http://seaweedfs:8333"
    assert captured["public_endpoint_url"] == "https://objects.example.test"


def test_read_artifact_bytes_is_explicitly_size_bounded() -> None:
    plane = ArtifactDatasetPlane()
    handle, _ = plane.create_artifact(
        owner_scope={"project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.txt",
        content_type="text/plain",
        body=b"hello world",
    )

    assert plane.read_artifact_bytes(handle.artifact_id, max_size_bytes=11) == b"hello world"
    with pytest.raises(ValueError, match="max_size_bytes"):
        plane.read_artifact_bytes(handle.artifact_id, max_size_bytes=10)


def test_read_artifact_bytes_returns_none_for_missing_body() -> None:
    plane = ArtifactDatasetPlane()

    assert plane.read_artifact_bytes("art_missing", max_size_bytes=10) is None


def test_artifact_metadata_cannot_overwrite_meta_json_attachment() -> None:
    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="meta.json",
        content_type="application/json",
        retention_class="ephemeral",
        body=b'{"invoice":"INV-42"}',
    )

    assert storage.get(str(meta["object_key"])) == b'{"invoice":"INV-42"}'
    assert storage.get(f"artifacts/_metadata/{handle.artifact_id}.json") is not None


def test_pre_1_0_artifact_sidecar_is_not_loaded_after_restart() -> None:
    storage = InMemoryObjectStorage()
    original = ArtifactDatasetPlane(storage=storage)
    handle, meta = original.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.txt",
        content_type="text/plain",
        retention_class="ephemeral",
        body=b"legacy body",
    )
    storage.delete(f"artifacts/_metadata/{handle.artifact_id}.json")
    storage.put(
        f"artifacts/{handle.artifact_id}/meta.json",
        json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )

    restarted = ArtifactDatasetPlane(storage=storage)

    assert restarted.get_artifact(handle.artifact_id) is None
    assert storage.get(f"artifacts/_metadata/{handle.artifact_id}.json") is None


def test_delete_artifact_reports_storage_failure_and_keeps_cached_metadata() -> None:
    class NonDeletingStorage(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.read_keys: list[str] = []

        def delete(self, key: str) -> bool:
            return False

        def get(self, key: str):
            self.read_keys.append(key)
            return super().get(key)

    storage = NonDeletingStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="invoice.pdf",
        content_type="application/pdf",
        retention_class="ephemeral",
        body=b"invoice body",
    )

    assert plane.delete_artifact(handle.artifact_id) is False
    assert plane.get_artifact(handle.artifact_id) is not None
    assert str(meta["object_key"]) not in storage.read_keys
    assert storage.get(str(meta["object_key"])) == b"invoice body"
    assert storage.get(f"artifacts/_metadata/{handle.artifact_id}.json") is not None

    restarted = ArtifactDatasetPlane(storage=storage)
    assert restarted.get_artifact(handle.artifact_id)["object_key"] == meta["object_key"]


def test_delete_artifact_retries_cleanup_after_body_was_already_deleted() -> None:
    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="invoice.pdf",
        content_type="application/pdf",
        retention_class="ephemeral",
        body=b"invoice body",
    )

    assert storage.delete(str(meta["object_key"])) is True
    assert plane.delete_artifact(handle.artifact_id) is True
    assert storage.get(f"artifacts/_metadata/{handle.artifact_id}.json") is None


def test_delete_artifact_keeps_cache_when_a_metadata_sidecar_remains() -> None:
    class FailCanonicalMetadataDeleteStorage(InMemoryObjectStorage):
        def delete(self, key: str) -> bool:
            if str(key).startswith("artifacts/_metadata/"):
                return False
            return super().delete(key)

    storage = FailCanonicalMetadataDeleteStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, _ = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="invoice.pdf",
        content_type="application/pdf",
        retention_class="ephemeral",
        body=b"invoice body",
    )

    assert plane.delete_artifact(handle.artifact_id) is False
    assert plane.get_artifact(handle.artifact_id) is not None
    assert storage.stat(f"artifacts/_metadata/{handle.artifact_id}.json") is not None

    restarted = ArtifactDatasetPlane(storage=storage)
    assert restarted.get_artifact(handle.artifact_id) is not None


def test_create_artifact_from_stream_uses_writer_checksum_without_body_get() -> None:
    class NoBodyReadStorage(InMemoryObjectStorage):
        def get(self, key: str):
            if str(key).endswith("/meta.json"):
                return super().get(key)
            raise AssertionError("stream finalization must not read artifact body")

    plane = ArtifactDatasetPlane(storage=NoBodyReadStorage())
    handle, meta = plane.create_artifact_from_stream(
        owner_scope={"project_id": 7},
        run_id="run_1",
        step_id="download",
        filename="download.bin",
        content_type="application/octet-stream",
        chunks=[b"abc", b"123"],
        max_size_bytes=10,
    )

    assert handle.artifact_id.startswith("art_")
    assert meta["size_bytes"] == 6
    assert meta["sha256"] == hashlib.sha256(b"abc123").hexdigest()


def test_create_artifact_keeps_preparing_sidecar_when_finalize_and_rollback_fail(
    monkeypatch,
) -> None:
    class FailingFinalSidecarStorage(InMemoryObjectStorage):
        metadata_writes = 0
        refuse_body_delete = True

        def put(self, key: str, body: bytes, *, content_type: str):
            if key == "artifacts/_metadata/art_interrupted.json":
                self.metadata_writes += 1
                if self.metadata_writes == 2:
                    raise RuntimeError("final sidecar unavailable")
            return super().put(key, body, content_type=content_type)

        def delete(self, key: str) -> bool:
            if self.refuse_body_delete and key == "artifacts/art_interrupted/input.txt":
                return False
            return super().delete(key)

    monkeypatch.setattr(artifact_plane_module, "_new_id", lambda _prefix: "art_interrupted")
    storage = FailingFinalSidecarStorage()

    _raises_callable_259_1 = ArtifactDatasetPlane(storage=storage).create_artifact
    with pytest.raises(RuntimeError, match="final sidecar unavailable"):
        _raises_callable_259_1(
            owner_scope={"project_id": "project-1", "job_id": "job-1"},
            run_id="job-1",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            retention_class="persistent",
            body=b"input",
        )

    reservation = json.loads(storage.get("artifacts/_metadata/art_interrupted.json"))
    manifest_key = plane_values_module.replay_owner_manifest_key(
        project_id="project-1",
        job_id="job-1",
        kind="artifact",
        object_id="art_interrupted",
    )
    assert reservation["persistence_status"] == "preparing"
    assert reservation["object_key"] == "artifacts/art_interrupted/input.txt"
    assert storage.stat(reservation["object_key"]) is not None
    assert storage.stat(manifest_key) is not None

    storage.refuse_body_delete = False
    cleanup = ArtifactDatasetPlane(storage=storage).cleanup_expired(
        now=float(reservation["preparation_expires_at"]) + 1
    )
    assert cleanup == {
        "artifacts_expired": 1,
        "datasets_expired": 0,
        "cleanup_failed": 0,
    }
    assert storage.stat(reservation["object_key"]) is None
    assert storage.stat("artifacts/_metadata/art_interrupted.json") is None
    assert storage.stat(manifest_key) is None


def test_create_artifact_from_stream_persists_expiring_preparing_sidecar(
    monkeypatch,
) -> None:
    class FailingFinalSidecarStorage(InMemoryObjectStorage):
        metadata_writes = 0
        refuse_body_delete = True

        def put(self, key: str, body: bytes, *, content_type: str):
            if key == "artifacts/_metadata/art_stream_interrupted.json":
                self.metadata_writes += 1
                if self.metadata_writes == 2:
                    raise RuntimeError("final sidecar unavailable")
            return super().put(key, body, content_type=content_type)

        def delete(self, key: str) -> bool:
            if self.refuse_body_delete and key == "artifacts/art_stream_interrupted/input.txt":
                return False
            return super().delete(key)

    monkeypatch.setattr(
        artifact_plane_module,
        "_new_id",
        lambda _prefix: "art_stream_interrupted",
    )
    storage = FailingFinalSidecarStorage()

    _raises_callable_322_1 = ArtifactDatasetPlane(storage=storage).create_artifact_from_stream
    with pytest.raises(RuntimeError, match="final sidecar unavailable"):
        _raises_callable_322_1(
            owner_scope={"project_id": "project-1", "job_id": "job-1"},
            run_id="job-1",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            retention_class="persistent",
            chunks=[b"input"],
            max_size_bytes=10,
        )

    reservation = json.loads(storage.get("artifacts/_metadata/art_stream_interrupted.json"))
    manifest_key = plane_values_module.replay_owner_manifest_key(
        project_id="project-1",
        job_id="job-1",
        kind="artifact",
        object_id="art_stream_interrupted",
    )
    assert reservation["persistence_status"] == "preparing"
    assert isinstance(reservation["preparation_expires_at"], float)
    assert storage.stat(reservation["object_key"]) is not None
    assert storage.stat(manifest_key) is not None

    storage.refuse_body_delete = False
    cleanup = ArtifactDatasetPlane(storage=storage).cleanup_expired(
        now=float(reservation["preparation_expires_at"]) + 1
    )
    assert cleanup["artifacts_expired"] == 1
    assert cleanup["cleanup_failed"] == 0
    assert storage.stat(manifest_key) is None


def test_stream_sidecar_is_durable_before_object_upload_starts() -> None:
    class OrderedStorage(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[tuple[str, str]] = []

        def put(self, key, body, *, content_type):
            self.events.append(("put", str(key)))
            return super().put(key, body, content_type=content_type)

        def put_stream(self, key, chunks, *, content_type, max_size_bytes):
            self.events.append(("put_stream", str(key)))
            return super().put_stream(
                key,
                chunks,
                content_type=content_type,
                max_size_bytes=max_size_bytes,
            )

    storage = OrderedStorage()
    plane = ArtifactDatasetPlane(storage=storage)

    plane.create_artifact_from_stream(
        artifact_id="art_reserved",
        owner_scope={"project_id": 7},
        run_id="run_1",
        step_id="upload",
        filename="invoice.pdf",
        content_type="application/pdf",
        chunks=[b"invoice"],
        max_size_bytes=10,
    )

    assert storage.events[0] == ("put", "artifacts/_metadata/art_reserved.json")
    assert storage.events[1] == ("put_stream", "artifacts/art_reserved/invoice.pdf")


def test_s3_cleanup_aborts_only_incomplete_uploads_for_exact_object_key() -> None:
    adapter = object.__new__(S3ObjectStorageAdapter)
    adapter._bucket = "artifacts"
    adapter._prefix = "tenant-a"
    adapter._client = MagicMock()
    adapter._client.list_multipart_uploads.return_value = {
        "Uploads": [
            {"Key": "tenant-a/artifacts/art_1/invoice.pdf", "UploadId": "upload-1"},
            {"Key": "tenant-a/artifacts/art_10/invoice.pdf", "UploadId": "upload-other"},
        ]
    }

    aborted = adapter.abort_incomplete_uploads("artifacts/art_1/invoice.pdf")

    assert aborted == 1
    adapter._client.abort_multipart_upload.assert_called_once_with(
        Bucket="artifacts",
        Key="tenant-a/artifacts/art_1/invoice.pdf",
        UploadId="upload-1",
    )


def test_create_artifact_from_stream_uses_caller_reserved_identity() -> None:
    plane = ArtifactDatasetPlane(storage=InMemoryObjectStorage())

    handle, meta = plane.create_artifact_from_stream(
        artifact_id="art_reserved",
        owner_scope={"project_id": 7},
        run_id="run_1",
        step_id="upload",
        filename="invoice.pdf",
        content_type="application/pdf",
        chunks=[b"invoice"],
        max_size_bytes=10,
    )

    assert handle.artifact_id == "art_reserved"
    assert meta["object_key"] == "artifacts/art_reserved/invoice.pdf"


def test_create_artifact_from_stream_cleans_up_on_overflow() -> None:
    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)

    with pytest.raises(ValueError):
        plane.create_artifact_from_stream(
            owner_scope={"project_id": 7},
            run_id="run_1",
            step_id="download",
            filename="download.bin",
            content_type="application/octet-stream",
            chunks=[b"abc", b"123"],
            max_size_bytes=5,
        )

    assert plane.get_artifact("art_missing") is None
    assert not storage._objects


def test_stream_failure_keeps_metadata_pointer_when_body_delete_is_refused() -> None:
    class FailedFinalizeStorage(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.body_key = ""
            self.aborted_keys: list[str] = []

        def put_stream(self, key, chunks, *, content_type, max_size_bytes=None):
            self.body_key = str(key)
            super().put_stream(
                key,
                chunks,
                content_type=content_type,
                max_size_bytes=max_size_bytes,
            )
            raise RuntimeError("finalize failed")

        def delete(self, key: str) -> bool:
            if str(key) == self.body_key:
                return False
            return super().delete(key)

        def abort_incomplete_uploads(self, key: str) -> int:
            self.aborted_keys.append(str(key))
            return 0

    storage = FailedFinalizeStorage()
    plane = ArtifactDatasetPlane(storage=storage)

    with pytest.raises(RuntimeError, match="finalize failed"):
        plane.create_artifact_from_stream(
            artifact_id="art_recoverable",
            owner_scope={"project_id": 7},
            run_id="run_1",
            step_id="upload",
            filename="invoice.pdf",
            content_type="application/pdf",
            chunks=[b"invoice"],
            max_size_bytes=10,
        )

    assert storage.stat("artifacts/art_recoverable/invoice.pdf") is not None
    assert storage.stat("artifacts/_metadata/art_recoverable.json") is not None
    assert storage.aborted_keys == ["artifacts/art_recoverable/invoice.pdf"]
    restarted = ArtifactDatasetPlane(storage=storage)
    assert restarted.get_artifact("art_recoverable") is not None


def test_get_artifact_exposes_owner_scope_for_ownership_checks() -> None:
    # FS-DEEP-CAND-023/024: presigned-URL ownership enforcement resolves an
    # artifact's owner_scope via get_artifact; ensure it is recorded and
    # retrievable, and that unknown ids return None (fail-closed input).
    plane = ArtifactDatasetPlane()
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_owner", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.txt",
        content_type="text/plain",
        body=b"data",
    )
    fetched = plane.get_artifact(handle.artifact_id)
    assert fetched is not None
    assert fetched["owner_scope"]["account_id"] == "acc_owner"
    assert fetched["owner_scope"]["project_id"] == 7
    assert plane.get_artifact("art_does_not_exist") is None


def test_transfer_child_artifact_to_parent_moves_only_metadata_and_retention_owner() -> None:
    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, metadata = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "child-1"},
        run_id="child-1",
        step_id="export",
        filename="catalog.csv",
        content_type="text/csv",
        body=b"sku,qty\nA,1\n",
    )
    object_key = str(metadata["object_key"])
    child_manifest_key = replay_owner_manifest_key(
        project_id="project-1",
        job_id="child-1",
        kind="artifact",
        object_id=handle.artifact_id,
    )
    parent_manifest_key = replay_owner_manifest_key(
        project_id="project-1",
        job_id="parent-1",
        kind="artifact",
        object_id=handle.artifact_id,
    )
    original_body = storage.get(object_key)

    transferred = plane.transfer_child_artifact_to_parent(
        handle.artifact_id,
        project_id="project-1",
        child_job_id="child-1",
        parent_job_id="parent-1",
    )

    assert transferred["owner_scope"]["job_id"] == "parent-1"
    assert transferred["producer_job_id"] == "child-1"
    assert transferred["run_id"] == "parent-1"
    assert storage.get(object_key) == original_body
    assert storage.stat(child_manifest_key) is None
    assert storage.stat(parent_manifest_key) is not None
    assert (
        plane.transfer_child_artifact_to_parent(
            handle.artifact_id,
            project_id="project-1",
            child_job_id="child-1",
            parent_job_id="parent-1",
        )
        == transferred
    )


@pytest.mark.parametrize(
    ("project_id", "child_job_id"),
    [("project-2", "child-1"), ("project-1", "child-2")],
)
def test_transfer_child_artifact_to_parent_rejects_foreign_owner(
    project_id: str,
    child_job_id: str,
) -> None:
    plane = ArtifactDatasetPlane()
    handle, _metadata = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "child-1"},
        run_id="child-1",
        step_id="export",
        filename="catalog.csv",
        content_type="text/csv",
        body=b"sku,qty\nA,1\n",
    )

    with pytest.raises(ValueError, match="belongs to another"):
        plane.transfer_child_artifact_to_parent(
            handle.artifact_id,
            project_id=project_id,
            child_job_id=child_job_id,
            parent_job_id="parent-1",
        )


def test_artifact_filename_rejects_path_segments() -> None:
    plane = ArtifactDatasetPlane()
    for filename in ("../escape.txt", "/absolute.txt", "nested/file.txt", "nested\\file.txt"):
        try:
            plane.create_artifact(
                owner_scope={"account_id": "acc_default", "project_id": 7},
                run_id="run_1",
                step_id="step_a",
                filename=filename,
                content_type="text/plain",
                retention_class="ephemeral",
                body=b"blocked",
            )
        except ValueError as exc:
            assert "filename" in str(exc).lower()
        else:
            raise AssertionError(f"unsafe filename was accepted: {filename}")


def test_artifact_retention_class_rejects_unknown_value() -> None:
    plane = ArtifactDatasetPlane()

    with pytest.raises(ValueError, match="retention_class"):
        plane.create_artifact(
            owner_scope={"account_id": "acc_default", "project_id": 7},
            run_id="run_1",
            step_id="step_a",
            filename="report.txt",
            content_type="text/plain",
            retention_class="forever",
            body=b"blocked",
        )


def test_local_storage_rejects_sibling_prefix_escape(tmp_path) -> None:
    root = tmp_path / "artifacts"
    storage = LocalFilesystemStorageAdapter(root_dir=str(root))

    try:
        storage.put(
            "artifacts/art_123/../../../artifacts_evil/pwn.txt",
            b"pwn",
            content_type="text/plain",
        )
    except ValueError as exc:
        assert "escapes root" in str(exc)
    else:
        raise AssertionError("sibling-prefix escape key was accepted")

    assert not (tmp_path / "artifacts_evil" / "pwn.txt").exists()


def test_local_storage_presigned_file_url_escapes_special_path_characters(
    tmp_path,
    monkeypatch,
) -> None:
    from urllib.parse import unquote, urlparse

    monkeypatch.setenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report #50%?.txt",
        content_type="text/plain",
        body=b"hello",
    )

    url = plane.artifact_presigned_url(handle.artifact_id, method="GET")
    parsed = urlparse(url)

    assert parsed.scheme == "file"
    assert Path(unquote(parsed.path)).name == "report #50%?.txt"
    assert parsed.query == "method=GET&ttl=600"


def test_artifact_public_download_uses_browser_presigner_with_filename() -> None:
    class BrowserDownloadStorage(InMemoryObjectStorage):
        def presign_public_download(self, key, *, filename, ttl_seconds=600):
            return f"https://objects.example.test/{key}?filename={filename}&ttl={ttl_seconds}"

    plane = ArtifactDatasetPlane(storage=BrowserDownloadStorage())
    handle, _meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="export",
        filename="supplier stock.csv",
        content_type="text/csv",
        body=b"sku,stock\nVINYL-1,2\n",
    )

    url = plane.artifact_public_download_url(
        handle.artifact_id,
        method="GET",
        filename="supplier stock.csv",
        ttl_seconds=600,
    )

    assert url.endswith("?filename=supplier stock.csv&ttl=600")


@pytest.mark.parametrize(
    "adapter_cls",
    (S3ObjectStorageAdapter, PublicSdkS3ObjectStorageAdapter),
)
def test_s3_public_download_presign_uses_public_client_and_attachment_disposition(
    adapter_cls,
) -> None:
    adapter = object.__new__(adapter_cls)
    adapter._bucket = "artifact-bucket"
    adapter._prefix = "tenant-a"
    adapter._presign_ttl = 900
    adapter._client = MagicMock()
    adapter._public_presign_client = MagicMock()
    adapter._public_endpoint_configured = True
    adapter._public_presign_client.generate_presigned_url.return_value = (
        "https://objects.example.test/signed"
    )

    url = adapter.presign_public_download(
        "artifacts/art-1/supplier stock.csv",
        filename="supplier stock.csv",
        ttl_seconds=600,
    )

    assert url == "https://objects.example.test/signed"
    adapter._client.generate_presigned_url.assert_not_called()
    adapter._public_presign_client.generate_presigned_url.assert_called_once_with(
        ClientMethod="get_object",
        Params={
            "Bucket": "artifact-bucket",
            "Key": "tenant-a/artifacts/art-1/supplier stock.csv",
            "ResponseContentDisposition": ("attachment; filename*=UTF-8''supplier%20stock.csv"),
        },
        ExpiresIn=600,
    )


@pytest.mark.parametrize(
    "adapter_cls",
    (S3ObjectStorageAdapter, PublicSdkS3ObjectStorageAdapter),
)
def test_s3_presigns_a_public_download_only_when_a_public_endpoint_was_named(
    adapter_cls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R130: signing with the internal client produces a URL no browser can follow.

    Built through the real constructor on purpose. The question here is what an empty
    ARTIFACT_S3_PUBLIC_ENDPOINT_URL means, and only __init__ answers it -- a test that
    sets the attributes by hand would pass whatever the constructor decided.
    """

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: MagicMock())

    bundled = adapter_cls(
        endpoint_url="http://seaweedfs:8333",
        public_endpoint_url="",
        bucket="flow-steward-artifacts",
        access_key="key",
        secret_key="secret",
    )
    published = adapter_cls(
        endpoint_url="http://seaweedfs:8333",
        public_endpoint_url="https://objects.example.test",
        bucket="flow-steward-artifacts",
        access_key="key",
        secret_key="secret",
    )

    assert bundled.presign_public_download("artifacts/a.csv", filename="a.csv") == ""
    bundled._client.generate_presigned_url.assert_not_called()

    assert published.presign_public_download("artifacts/a.csv", filename="a.csv")
    published._public_presign_client.generate_presigned_url.assert_called_once()


def test_finalize_artifact_upload_refreshes_metadata_after_presigned_write(tmp_path) -> None:
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="output.txt",
        content_type="text/plain",
        body=None,
    )

    assert plane.finalize_artifact_upload(handle.artifact_id) is None

    storage.put(str(meta["object_key"]), b"uploaded", content_type="text/plain")
    refreshed = plane.finalize_artifact_upload(handle.artifact_id)

    assert refreshed is not None
    assert refreshed["size_bytes"] == 8
    assert refreshed["sha256"] == hashlib.sha256(b"uploaded").hexdigest()
    assert refreshed["preview_text"] == "uploaded"
    cached = plane.get_artifact(handle.artifact_id)
    assert cached is not None
    assert cached["size_bytes"] == 8


def test_finalize_artifact_upload_rejects_oversize_and_removes_body(tmp_path) -> None:
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="output.txt",
        content_type="text/plain",
        body=None,
    )
    object_key = str(meta["object_key"])
    assert plane.artifact_presigned_url(handle.artifact_id, method="GET") == ""

    storage.put(object_key, b"0123456789", content_type="text/plain")
    refreshed = plane.finalize_artifact_upload(handle.artifact_id, max_size_bytes=4)

    assert refreshed is not None
    assert refreshed["size_bytes"] == 10
    assert refreshed["sha256"] == ""
    assert refreshed["preview_text"] == ""
    assert refreshed["upload_status"] == "rejected"
    assert refreshed["upload_error"] == "artifact_size_limit_exceeded"
    assert refreshed["object_deleted"] is True
    assert storage.get(object_key) is None
    assert plane.artifact_presigned_url(handle.artifact_id, method="GET") == ""


def test_finalize_artifact_upload_requires_storage_stat_before_body_read() -> None:
    class NoStatStorage:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.get_called = False

        def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, object]:
            _ = content_type
            self.objects[str(key)] = bytes(body)
            return {
                "key": key,
                "size_bytes": len(body),
                "sha256": hashlib.sha256(bytes(body)).hexdigest(),
            }

        def get(self, key: str) -> bytes | None:
            self.get_called = True
            return self.objects.get(str(key))

        def delete(self, key: str) -> bool:
            return self.objects.pop(str(key), None) is not None

        def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str:
            _ = ttl_seconds
            return f"https://storage.local/{key}?method={method}"

    storage = NoStatStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="output.txt",
        content_type="text/plain",
        body=None,
    )
    storage.put(str(meta["object_key"]), b"uploaded", content_type="text/plain")

    with pytest.raises(RuntimeError, match="support stat"):
        plane.finalize_artifact_upload(handle.artifact_id, max_size_bytes=4)

    assert storage.get_called is False


def test_finalize_artifact_upload_uses_default_cap_before_body_read(
    tmp_path,
    monkeypatch,
) -> None:
    from core.application.extensions import artifact_dataset_plane_v2 as plane_module

    monkeypatch.setattr(
        plane_module,
        "DEFAULT_ARTIFACT_UPLOAD_FINALIZE_MAX_SIZE_BYTES",
        4,
    )
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="output.txt",
        content_type="text/plain",
        body=None,
    )
    object_key = str(meta["object_key"])
    storage.put(object_key, b"0123456789", content_type="text/plain")

    refreshed = plane.finalize_artifact_upload(handle.artifact_id)

    assert refreshed is not None
    assert refreshed["size_bytes"] == 10
    assert refreshed["sha256"] == ""
    assert refreshed["upload_status"] == "rejected"
    assert refreshed["upload_error"] == "artifact_size_limit_exceeded"
    assert storage.get(object_key) is None


def test_local_fs_artifact_presigned_urls_require_trusted_override(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, raising=False)
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    finalized_handle, _finalized_meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="old-output.txt",
        content_type="text/plain",
        body=b"old-body",
    )
    empty_handle, empty_meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_b",
        filename="new-output.txt",
        content_type="text/plain",
        body=None,
    )

    assert plane.artifact_presigned_url(finalized_handle.artifact_id, method="GET") == ""
    assert plane.artifact_presigned_url(empty_handle.artifact_id, method="PUT") == ""

    monkeypatch.setenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")

    get_url = plane.artifact_presigned_url(finalized_handle.artifact_id, method="GET")
    assert get_url.startswith("file://")
    assert "method=GET" in get_url
    assert plane.artifact_presigned_url(finalized_handle.artifact_id, method="PUT") == ""
    empty_put_url = plane.artifact_presigned_url(empty_handle.artifact_id, method="PUT")
    assert empty_put_url.startswith("file://")
    assert "method=PUT" in empty_put_url

    storage.put(str(empty_meta["object_key"]), b"pending-body", content_type="text/plain")

    assert plane.artifact_presigned_url(empty_handle.artifact_id, method="PUT") == ""


def test_memory_artifact_presigned_urls_are_not_subprocess_grants() -> None:
    plane = ArtifactDatasetPlane(storage=InMemoryObjectStorage())
    finalized_handle, _finalized_meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="input.txt",
        content_type="text/plain",
        body=b"body",
    )
    empty_handle, _empty_meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_b",
        filename="output.txt",
        content_type="text/plain",
        body=None,
    )

    assert plane.artifact_presigned_url(finalized_handle.artifact_id, method="GET") == ""
    assert plane.artifact_presigned_url(empty_handle.artifact_id, method="PUT") == ""


def test_cleanup_prepared_extension_grants_preserves_by_reference_outputs(
    tmp_path,
    monkeypatch,
) -> None:
    from core.application.extensions import runtime_grants

    monkeypatch.setenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    owner_scope = {"account_id": "acc_alpha", "project_id": "project_alpha"}
    by_ref_artifact, _ = plane.create_artifact(
        owner_scope=owner_scope,
        run_id="run_1",
        step_id="caller",
        filename="caller-output.txt",
        content_type="text/plain",
        retention_class="persistent",
        body=None,
    )
    by_ref_dataset, _ = plane.create_dataset_grant(
        owner_scope=owner_scope,
        run_id="run_1",
        step_id="caller",
        schema={"columns": [{"name": "sku", "type": "text"}]},
        retention_class="persistent",
    )

    prepared = runtime_grants.prepare_extension_grants(
        plane=plane,
        scope=SimpleNamespace(
            account_id="acc_alpha",
            allowed_project_ids=frozenset({"project_alpha"}),
        ),
        audit=owner_scope,
        extension_id="supplier",
        artifact_outputs=[
            {
                "artifact_handle": f"artifact:{by_ref_artifact.artifact_id}",
                "binding_key": "caller_artifact",
            }
        ],
        dataset_outputs=[
            {
                "dataset_handle": f"dataset:{by_ref_dataset.dataset_id}",
                "binding_key": "caller_dataset",
            }
        ],
        artifact_output_specs=[
            {
                "binding_key": "minted_artifact",
                "filename": "minted.txt",
                "content_type": "text/plain",
                "retention_class": "persistent",
            }
        ],
        dataset_output_specs=[
            {
                "binding_key": "minted_dataset",
                "schema": {"columns": [{"name": "sku", "type": "text"}]},
                "retention_class": "persistent",
            }
        ],
    )

    assert len(prepared.artifact_output_ids) == 2
    assert len(prepared.dataset_output_ids) == 2
    assert len(prepared.minted_artifact_output_ids) == 1
    assert len(prepared.minted_dataset_output_ids) == 1
    assert by_ref_artifact.artifact_id not in prepared.minted_artifact_output_ids
    assert by_ref_dataset.dataset_id not in prepared.minted_dataset_output_ids

    removed = runtime_grants.cleanup_prepared_extension_grants(plane, prepared)

    assert removed == {"artifacts": 1, "datasets": 1}
    assert plane.get_artifact(by_ref_artifact.artifact_id) is not None
    assert plane.get_dataset(by_ref_dataset.dataset_id) is not None
    assert plane.get_artifact(prepared.minted_artifact_output_ids[0]) is None
    assert plane.get_dataset(prepared.minted_dataset_output_ids[0]) is None


def test_s3_storage_stat_uses_head_object_without_reading_body() -> None:
    boto3 = pytest.importorskip("boto3")
    mock_client = MagicMock()
    mock_client.head_bucket.return_value = {}
    mock_client.head_object.return_value = {"ContentLength": 123}

    with patch.object(boto3, "client", return_value=mock_client):
        adapter = S3ObjectStorageAdapter(
            bucket="artifact-bucket",
            endpoint_url="http://s3.internal:8333",
            access_key="key",
            secret_key="secret",
        )

    stat = adapter.stat("artifacts/art_1/output.txt")

    assert stat == {"key": "artifacts/art_1/output.txt", "size_bytes": 123}
    mock_client.head_object.assert_called_once_with(
        Bucket="artifact-bucket",
        Key="artifacts/art_1/output.txt",
    )
    mock_client.get_object.assert_not_called()


def test_s3_storage_returns_one_bounded_page_with_continuation() -> None:
    adapter = object.__new__(S3ObjectStorageAdapter)
    adapter._bucket = "artifacts"
    adapter._prefix = "tenant-a"
    adapter._client = MagicMock()
    adapter._client.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "tenant-a/datasets/dst_1/meta.json"},
            {"Key": "tenant-a/datasets/dst_2/meta.json"},
        ],
        "IsTruncated": True,
        "NextContinuationToken": "next-page",
    }

    keys, continuation = adapter.list_key_page(
        "datasets/",
        limit=2,
        continuation_token="current-page",
    )

    assert keys == ["datasets/dst_1/meta.json", "datasets/dst_2/meta.json"]
    assert continuation == "next-page"
    adapter._client.list_objects_v2.assert_called_once_with(
        Bucket="artifacts",
        Prefix="tenant-a/datasets/",
        MaxKeys=2,
        ContinuationToken="current-page",
    )


def test_s3_storage_iterator_carries_continuation_between_bounded_calls() -> None:
    adapter = object.__new__(S3ObjectStorageAdapter)
    adapter._bucket = "artifacts"
    adapter._prefix = "tenant-a"
    adapter._client = MagicMock()
    adapter._client.list_objects_v2.side_effect = [
        {
            "Contents": [{"Key": "tenant-a/artifacts/a/body"}],
            "IsTruncated": True,
            "NextContinuationToken": "page-2",
        },
        {
            "Contents": [{"Key": "tenant-a/artifacts/b/body"}],
            "IsTruncated": False,
        },
    ]

    keys = list(
        object_storage_module.iter_storage_keys(
            adapter,
            "artifacts/",
            page_size=1,
        )
    )

    assert keys == ["artifacts/a/body", "artifacts/b/body"]
    assert adapter._client.list_objects_v2.call_args_list[0].kwargs == {
        "Bucket": "artifacts",
        "Prefix": "tenant-a/artifacts/",
        "MaxKeys": 1,
    }
    assert adapter._client.list_objects_v2.call_args_list[1].kwargs["ContinuationToken"] == "page-2"


def test_s3_restart_stable_page_uses_start_after_and_bounded_max_keys() -> None:
    adapter = S3ObjectStorageAdapter.__new__(S3ObjectStorageAdapter)
    adapter._client = MagicMock()
    adapter._bucket = "artifacts"
    adapter._prefix = "tenant-a"
    adapter._client.list_objects_v2.return_value = {
        "Contents": [{"Key": "tenant-a/_replay_job_cleanup/0a/b/journal.json"}],
        "IsTruncated": True,
    }

    keys, next_key = adapter.list_key_page_after(
        "_replay_job_cleanup/0a/",
        limit=1,
        start_after="_replay_job_cleanup/0a/a/journal.json",
    )

    assert keys == ["_replay_job_cleanup/0a/b/journal.json"]
    assert next_key == "_replay_job_cleanup/0a/b/journal.json"
    adapter._client.list_objects_v2.assert_called_once_with(
        Bucket="artifacts",
        Prefix="tenant-a/_replay_job_cleanup/0a/",
        MaxKeys=1,
        StartAfter="tenant-a/_replay_job_cleanup/0a/a/journal.json",
    )


def test_inmemory_exact_prefix_page_does_not_visit_unrelated_inventory() -> None:
    class _CountingObjects(dict):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.visits = 0

        def __iter__(self):
            for key in super().__iter__():
                self.visits += 1
                yield key

    storage = InMemoryObjectStorage()
    for index in range(10_001):
        storage.put(
            f"unrelated/job-{index:05d}/meta.json",
            b"foreign",
            content_type="application/json",
        )
    storage.put(
        "_replay_job_manifests/project/job/artifact/owned.json",
        b"owned",
        content_type="application/json",
    )
    storage._objects = _CountingObjects(storage._objects)

    keys, continuation = storage.list_key_page(
        "_replay_job_manifests/project/job/",
        limit=1,
        continuation_token=None,
    )

    assert keys == ["_replay_job_manifests/project/job/artifact/owned.json"]
    assert continuation is None
    assert storage._objects.visits == 0


def test_inmemory_mutation_and_index_updates_are_atomic_during_pagination() -> None:
    storage = InMemoryObjectStorage()
    start = threading.Barrier(8)

    def _mutate(worker: int) -> set[str]:
        expected: set[str] = set()
        start.wait()
        for index in range(200):
            key = f"concurrent/{worker:02d}/{index:04d}.json"
            if index % 3 == 0:
                storage.put_stream(
                    key,
                    [b"value"],
                    content_type="application/json",
                    max_size_bytes=32,
                )
            elif index % 3 == 1:
                storage.put(key, b"value", content_type="application/json")
            else:
                assert storage.put_if_absent(key, b"value", content_type="application/json")
            expected.add(key)
            if index % 5 == 0:
                assert storage.delete(key)
                expected.remove(key)
            if index % 20 == 0:
                page, _token = storage.list_key_page("concurrent/", limit=31)
                assert page == sorted(page)
        return expected

    with ThreadPoolExecutor(max_workers=8) as executor:
        expected_sets = list(executor.map(_mutate, range(8)))

    assert list(
        object_storage_module.iter_storage_keys(storage, "concurrent/", page_size=37)
    ) == sorted(set().union(*expected_sets))


def test_inmemory_failed_stream_does_not_delete_existing_object_or_index_entry() -> None:
    storage = InMemoryObjectStorage()
    storage.put("owned/item.json", b"original", content_type="application/json")

    with pytest.raises(ValueError, match="max_size_bytes"):
        storage.put_stream(
            "owned/item.json",
            [b"replacement-too-large"],
            content_type="application/json",
            max_size_bytes=4,
        )

    assert storage.get("owned/item.json") == b"original"
    assert storage.list_key_page("owned/", limit=10) == (["owned/item.json"], None)


def test_local_filesystem_iterator_uses_one_traversal_across_all_pages(tmp_path) -> None:
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    for index in range(1_000):
        storage.put(
            f"owned/job/artifact/{index:04d}.json",
            b"owned",
            content_type="application/json",
        )

    with patch("os.scandir", wraps=os.scandir) as scandir:
        keys = list(object_storage_module.iter_storage_keys(storage, "owned/job/", page_size=128))

    assert len(keys) == 1_000
    assert len(set(keys)) == 1_000
    assert scandir.call_count >= 1


def test_local_filesystem_restart_stable_cleanup_page_survives_adapter_restart(tmp_path) -> None:
    prefix = "_replay_job_cleanup/0a/"
    first_key = f"{prefix}{'/'.join('0' * 62)}/journal.json"
    second_key = f"{prefix}{'/'.join(('0' * 61) + '1')}/journal.json"
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    storage.put(first_key, b"first", content_type="application/json")
    storage.put(second_key, b"second", content_type="application/json")

    first_page, first_cursor = storage.list_key_page_after(prefix, limit=1)
    storage.close()
    restarted = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    second_page, second_cursor = restarted.list_key_page_after(
        prefix, limit=1, start_after=first_cursor
    )

    assert first_page == [first_key]
    assert first_cursor == first_key
    assert second_page == [second_key]
    assert second_cursor is None
    restarted.close()


def test_local_filesystem_missing_exact_directory_never_scans_siblings(tmp_path) -> None:
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    storage.put("owners/existing/artifact.json", b"owned", content_type="application/json")

    with patch("os.walk", wraps=os.walk) as walk:
        page = storage.list_key_page(
            "owners/missing/",
            limit=128,
            continuation_token=None,
        )

    assert page == ([], None)
    walk.assert_not_called()


def test_local_filesystem_page_one_does_not_visit_or_retain_full_directory(tmp_path) -> None:
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    for index in range(10_000):
        storage.put(
            f"owned/job/{index:05d}.json",
            b"owned",
            content_type="application/json",
        )
    visited = 0
    real_scandir = os.scandir

    class _CountingScandir:
        def __init__(self, path):
            self._iterator = real_scandir(path)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal visited
            visited += 1
            return next(self._iterator)

        def close(self):
            self._iterator.close()

    with patch("os.scandir", side_effect=_CountingScandir):
        page, token = storage.list_key_page("owned/job/", limit=1)

    assert len(page) == 1
    assert token
    assert visited <= 2
    cursor = next(iter(storage._list_cursors.values()))
    assert len(cursor.frames) <= 2
    storage.close()


def test_local_filesystem_cursor_max_request_is_clamped_to_global_cap(tmp_path) -> None:
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path), list_cursor_max=999)
    assert storage._list_cursor_max == local_storage_module.LOCAL_STORAGE_CURSOR_MAX

    first_token = None
    for index in range(local_storage_module.LOCAL_STORAGE_CURSOR_MAX + 1):
        storage.put(f"owners/{index}/one", b"1", content_type="application/octet-stream")
        storage.put(f"owners/{index}/two", b"2", content_type="application/octet-stream")
        _page, token = storage.list_key_page(f"owners/{index}/", limit=1)
        first_token = first_token or token

    assert len(storage._list_cursors) == local_storage_module.LOCAL_STORAGE_CURSOR_MAX
    with pytest.raises(RuntimeError, match=r"invalid|expired"):
        storage.list_key_page("owners/0/", limit=1, continuation_token=first_token)
    storage.close()


def test_local_filesystem_continuation_is_adapter_and_prefix_bound(tmp_path) -> None:
    first = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    first.put("owners/a/one.json", b"1", content_type="application/json")
    first.put("owners/a/two.json", b"2", content_type="application/json")
    first.put("owners/b/one.json", b"1", content_type="application/json")
    _keys, token = first.list_key_page("owners/a/", limit=1)
    assert token

    with pytest.raises(RuntimeError, match="prefix"):
        first.list_key_page("owners/b/", limit=1, continuation_token=token)
    _raises_callable_1256_1 = LocalFilesystemStorageAdapter(root_dir=str(tmp_path)).list_key_page
    with pytest.raises(RuntimeError, match=r"invalid|expired"):
        _raises_callable_1256_1("owners/a/", limit=1, continuation_token=token)


def test_local_filesystem_cursor_ttl_lru_and_close_are_bounded(tmp_path, monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(local_storage_module.time, "monotonic", lambda: clock[0])
    storage = LocalFilesystemStorageAdapter(
        root_dir=str(tmp_path),
        list_cursor_ttl_seconds=2,
        list_cursor_max=2,
    )
    for owner in ("a", "b", "c"):
        storage.put(f"owners/{owner}/one.json", b"1", content_type="application/json")
        storage.put(f"owners/{owner}/two.json", b"2", content_type="application/json")

    _a_page, a_token = storage.list_key_page("owners/a/", limit=1)
    _b_page, b_token = storage.list_key_page("owners/b/", limit=1)
    _c_page, c_token = storage.list_key_page("owners/c/", limit=1)
    assert a_token
    assert b_token
    assert c_token
    assert len(storage._list_cursors) == 2
    with pytest.raises(RuntimeError, match=r"invalid|expired"):
        storage.list_key_page("owners/a/", limit=1, continuation_token=a_token)

    clock[0] += 3
    with pytest.raises(RuntimeError, match=r"invalid|expired"):
        storage.list_key_page("owners/b/", limit=1, continuation_token=b_token)
    assert len(storage._list_cursors) == 0

    _fresh_page, fresh_token = storage.list_key_page("owners/c/", limit=1)
    assert fresh_token
    storage.close()
    assert len(storage._list_cursors) == 0
    with pytest.raises(RuntimeError, match="closed"):
        storage.list_key_page("owners/c/", limit=1, continuation_token=fresh_token)


def test_local_filesystem_concurrent_prefix_cursors_complete_independently(tmp_path) -> None:
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    for owner in ("a", "b"):
        for index in range(300):
            storage.put(
                f"owners/{owner}/{index:04d}.json",
                owner.encode(),
                content_type="application/json",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda owner: list(
                    object_storage_module.iter_storage_keys(
                        storage,
                        f"owners/{owner}/",
                        page_size=31,
                    )
                ),
                ("a", "b"),
            )
        )

    assert sorted(results[0]) == [f"owners/a/{index:04d}.json" for index in range(300)]
    assert sorted(results[1]) == [f"owners/b/{index:04d}.json" for index in range(300)]
    assert len(storage._list_cursors) == 0


def test_storage_iterator_fails_closed_after_hard_page_cap(monkeypatch) -> None:
    class _CyclingStorage:
        def list_key_page(self, _prefix: str, *, limit: int, continuation_token=None):
            del limit
            next_token = "page-b" if continuation_token != "page-b" else "page-a"
            return ["owned/item.json"], next_token

    monkeypatch.setattr(object_storage_module, "STORAGE_ITERATION_MAX_PAGES", 3)

    _raises_input_1335_1 = object_storage_module.iter_storage_keys(
        _CyclingStorage(), "owned/", page_size=1
    )
    with pytest.raises(RuntimeError, match="page limit"):
        list(_raises_input_1335_1)


def test_storage_iterator_page_cap_covers_more_than_1_5m_keys_at_cleanup_page_size() -> None:
    required_pages = (1_500_000 // object_storage_module.STORAGE_CLEANUP_PAGE_SIZE) + 2

    assert required_pages < object_storage_module.STORAGE_ITERATION_MAX_PAGES


@pytest.mark.parametrize(
    "adapter_class",
    (S3ObjectStorageAdapter, PublicSdkS3ObjectStorageAdapter),
)
def test_s3_storage_uses_sigv4_for_credentialed_presigned_urls(adapter_class) -> None:
    boto3 = pytest.importorskip("boto3")
    mock_client = MagicMock()
    mock_client.head_bucket.return_value = {}

    with patch.object(boto3, "client", return_value=mock_client) as client:
        adapter_class(
            bucket="artifact-bucket",
            endpoint_url="http://s3.internal:8333",
            access_key="key",
            secret_key="secret",
        )

    assert client.call_args.kwargs["config"].signature_version == "s3v4"


def test_s3_put_stream_uses_legal_parts_and_completes() -> None:
    boto3 = pytest.importorskip("boto3")
    mock_client = MagicMock()
    mock_client.head_bucket.return_value = {}
    mock_client.create_multipart_upload.return_value = {"UploadId": "upload-1"}
    mock_client.upload_part.side_effect = [
        {"ETag": "etag-1"},
        {"ETag": "etag-2"},
    ]
    body = b"a" * (6 * 1024 * 1024)

    with patch.object(boto3, "client", return_value=mock_client):
        adapter = S3ObjectStorageAdapter(bucket="artifact-bucket")

    result = adapter.put_stream(
        "artifacts/art_1/output.bin",
        [body],
        content_type="application/octet-stream",
        max_size_bytes=len(body),
    )

    assert result["size_bytes"] == len(body)
    assert mock_client.upload_part.call_count == 2
    part_sizes = [len(call.kwargs["Body"]) for call in mock_client.upload_part.call_args_list]
    assert part_sizes == [5 * 1024 * 1024, 1 * 1024 * 1024]
    mock_client.complete_multipart_upload.assert_called_once()
    mock_client.abort_multipart_upload.assert_not_called()


def test_s3_put_stream_aborts_on_overflow() -> None:
    boto3 = pytest.importorskip("boto3")
    mock_client = MagicMock()
    mock_client.head_bucket.return_value = {}
    mock_client.create_multipart_upload.return_value = {"UploadId": "upload-1"}

    with patch.object(boto3, "client", return_value=mock_client):
        adapter = S3ObjectStorageAdapter(bucket="artifact-bucket")

    with pytest.raises(ValueError):
        adapter.put_stream(
            "artifacts/art_1/output.bin",
            [b"abcdef"],
            content_type="application/octet-stream",
            max_size_bytes=5,
        )

    mock_client.abort_multipart_upload.assert_called_once()
    mock_client.complete_multipart_upload.assert_not_called()


def test_s3_put_stream_empty_body_creates_zero_byte_object() -> None:
    boto3 = pytest.importorskip("boto3")
    mock_client = MagicMock()
    mock_client.head_bucket.return_value = {}
    mock_client.create_multipart_upload.return_value = {"UploadId": "upload-1"}

    with patch.object(boto3, "client", return_value=mock_client):
        adapter = S3ObjectStorageAdapter(bucket="artifact-bucket")

    result = adapter.put_stream(
        "artifacts/art_1/empty.bin",
        [],
        content_type="application/octet-stream",
        max_size_bytes=5,
    )

    assert result["size_bytes"] == 0
    mock_client.abort_multipart_upload.assert_called_once()
    mock_client.put_object.assert_called_once()
    assert mock_client.put_object.call_args.kwargs["Body"] == b""


def test_local_storage_put_if_absent_preserves_first_writer(tmp_path) -> None:
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "objects"))

    assert storage.put_if_absent("claims/owner.json", b"first", content_type="application/json")
    assert not storage.put_if_absent(
        "claims/owner.json", b"second", content_type="application/json"
    )
    assert storage.get("claims/owner.json") == b"first"


def test_s3_storage_put_if_absent_maps_precondition_failure_to_lost_claim() -> None:
    class PreconditionFailed(RuntimeError):
        def __init__(self) -> None:
            self.response = {
                "Error": {"Code": "PreconditionFailed"},
                "ResponseMetadata": {"HTTPStatusCode": 412},
            }

    adapter = object.__new__(S3ObjectStorageAdapter)
    adapter._bucket = "artifacts"
    adapter._prefix = "tenant-a"
    adapter._client = MagicMock()
    adapter._client.put_object.side_effect = [{}, PreconditionFailed()]

    assert adapter.put_if_absent("claims/owner.json", b"first", content_type="application/json")
    assert not adapter.put_if_absent(
        "claims/owner.json", b"second", content_type="application/json"
    )
    assert adapter._client.put_object.call_args.kwargs["IfNoneMatch"] == "*"


def test_s3_storage_put_if_absent_retries_conditional_conflict() -> None:
    class ConditionalConflict(RuntimeError):
        def __init__(self) -> None:
            self.response = {
                "Error": {"Code": "ConditionalRequestConflict"},
                "ResponseMetadata": {"HTTPStatusCode": 409},
            }

    adapter = object.__new__(S3ObjectStorageAdapter)
    adapter._bucket = "artifacts"
    adapter._prefix = "tenant-a"
    adapter._client = MagicMock()
    adapter._client.put_object.side_effect = [ConditionalConflict(), {}]

    assert adapter.put_if_absent("claims/owner.json", b"first", content_type="application/json")
    assert adapter._client.put_object.call_count == 2


def test_public_attachment_stream_rejects_memory_storage() -> None:
    plane = ArtifactDatasetPlane()

    with pytest.raises(RuntimeError, match="public_attachment_memory_storage_unsupported"):
        plane.create_artifact_from_stream(
            owner_scope={},
            run_id="run-1",
            step_id="manual_enqueue",
            filename="invoice.pdf",
            content_type="application/pdf",
            chunks=[b"invoice"],
            max_size_bytes=7,
            allow_in_memory=False,
        )


def test_dataset_handle_and_cleanup_retention() -> None:
    plane = ArtifactDatasetPlane()
    handle, meta = plane.create_dataset(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_2",
        step_id="step_b",
        schema={"columns": ["id"]},
        rows=[{"id": 1}, {"id": 2}],
        retention_class="run_window",
    )
    assert handle.dataset_id.startswith("dst_")
    assert meta["row_count"] == 2
    assert len(meta["preview_rows"]) == 2

    cleanup = plane.cleanup_expired(now=meta["expires_at"] + 1)
    assert cleanup["datasets_expired"] == 1
    assert plane.get_dataset(handle.dataset_id) is None


def test_job_owned_run_window_artifacts_wait_for_job_retention_cleanup() -> None:
    plane = ArtifactDatasetPlane()
    artifact, artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="download",
        filename="stock.csv",
        content_type="text/csv",
        retention_class="run_window",
        body=b"sku,qty\nA,1\n",
    )
    dataset, dataset_meta = plane.create_dataset(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="parse",
        retention_class="run_window",
        rows=[{"sku": "A", "qty": 1}],
    )

    cleanup = plane.cleanup_expired(now=10**12)

    assert artifact_meta["expires_at"] is None
    assert dataset_meta["expires_at"] is None
    assert cleanup == {
        "artifacts_expired": 0,
        "datasets_expired": 0,
        "cleanup_failed": 0,
    }
    assert plane.get_artifact(artifact.artifact_id) is not None
    assert plane.get_dataset(dataset.dataset_id) is not None


def test_create_dataset_rolls_back_body_and_cache_when_sidecar_write_fails(monkeypatch) -> None:
    class FailingSidecarStorage(InMemoryObjectStorage):
        def put(self, key: str, body: bytes, *, content_type: str):
            if key == "datasets/dst_failed/meta.json":
                raise RuntimeError("sidecar unavailable")
            return super().put(key, body, content_type=content_type)

    monkeypatch.setattr(dataset_operations_module, "_new_id", lambda _prefix: "dst_failed")
    storage = FailingSidecarStorage()
    plane = ArtifactDatasetPlane(storage=storage)

    with pytest.raises(RuntimeError, match="sidecar unavailable"):
        plane.create_dataset(
            owner_scope={"project_id": "project-1"},
            run_id="run-1",
            step_id="step-1",
            rows=[{"id": 1}],
        )

    assert storage.stat("datasets/dst_failed/rows.json") is None
    assert "dst_failed" not in plane._datasets
    assert ArtifactDatasetPlane(storage=storage).get_dataset("dst_failed") is None


def test_create_dataset_keeps_durable_reservation_when_final_sidecar_and_rollback_fail(
    monkeypatch,
) -> None:
    class FailingFinalSidecarStorage(InMemoryObjectStorage):
        metadata_writes = 0
        refuse_body_delete = True

        def put(self, key: str, body: bytes, *, content_type: str):
            if key == "datasets/dst_interrupted/meta.json":
                self.metadata_writes += 1
                if self.metadata_writes == 2:
                    raise RuntimeError("final sidecar unavailable")
            return super().put(key, body, content_type=content_type)

        def delete(self, key: str) -> bool:
            if self.refuse_body_delete and key == "datasets/dst_interrupted/rows.json":
                return False
            return super().delete(key)

    monkeypatch.setattr(dataset_operations_module, "_new_id", lambda _prefix: "dst_interrupted")
    storage = FailingFinalSidecarStorage()
    plane = ArtifactDatasetPlane(storage=storage)

    with pytest.raises(RuntimeError, match="final sidecar unavailable"):
        plane.create_dataset(
            owner_scope={"project_id": "project-1", "job_id": "job-1"},
            run_id="job-1",
            step_id="step-1",
            rows=[{"id": 1}],
        )

    reservation_raw = storage.get("datasets/dst_interrupted/meta.json")
    assert reservation_raw is not None
    reservation = json.loads(reservation_raw)
    manifest_key = plane_values_module.replay_owner_manifest_key(
        project_id="project-1",
        job_id="job-1",
        kind="dataset",
        object_id="dst_interrupted",
    )
    assert reservation["persistence_status"] == "preparing"
    assert reservation["object_key"] == "datasets/dst_interrupted/rows.json"
    assert storage.stat("datasets/dst_interrupted/rows.json") is not None
    assert storage.stat(manifest_key) is not None
    assert "dst_interrupted" not in plane._datasets

    storage.refuse_body_delete = False
    restarted = ArtifactDatasetPlane(storage=storage)
    cleanup = restarted.cleanup_expired(now=reservation["preparation_expires_at"] + 1)

    assert cleanup == {
        "artifacts_expired": 0,
        "datasets_expired": 1,
        "cleanup_failed": 0,
    }
    assert storage.stat("datasets/dst_interrupted/rows.json") is None
    assert storage.stat("datasets/dst_interrupted/meta.json") is None
    assert storage.stat(manifest_key) is None


def test_cleanup_expired_scans_durable_sidecars_after_restart() -> None:
    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1"},
        run_id="prelaunch",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        retention_class="ephemeral",
        body=b"input",
    )
    dataset, dataset_meta = plane.create_dataset(
        owner_scope={"project_id": "project-1"},
        run_id="prelaunch",
        step_id="upload",
        retention_class="ephemeral",
        rows=[{"id": 1}],
    )
    plane.bind_replay_owner(
        artifact_id=artifact.artifact_id,
        project_id="project-1",
        job_id="job-1",
    )
    plane.bind_replay_owner(
        dataset_id=dataset.dataset_id,
        project_id="project-1",
        job_id="job-1",
    )
    representation_key = f"datasets/{dataset.dataset_id}/representations/rows.ndjson"
    storage.put(representation_key, b'{"id":1}\n', content_type="application/x-ndjson")
    storage.put(
        f"{representation_key}.json",
        b'{"content_type":"application/x-ndjson"}',
        content_type="application/json",
    )

    restarted = ArtifactDatasetPlane(storage=storage)
    cleanup = restarted.cleanup_expired(
        now=max(float(artifact_meta["expires_at"]), float(dataset_meta["expires_at"])) + 1
    )

    assert cleanup == {
        "artifacts_expired": 1,
        "datasets_expired": 1,
        "cleanup_failed": 0,
    }
    assert restarted.get_artifact(artifact.artifact_id) is None
    assert restarted.get_dataset(dataset.dataset_id) is None
    assert storage.stat(f"_replay_owners/artifact/{artifact.artifact_id}.json") is None
    assert storage.stat(f"_replay_owners/dataset/{dataset.dataset_id}.json") is None
    assert storage.stat(representation_key) is None
    assert storage.stat(f"{representation_key}.json") is None


def test_cleanup_expired_consumes_bounded_pages_after_restart() -> None:
    class BoundedStorage(InMemoryObjectStorage):
        requested_limits: list[int]

        def __init__(self) -> None:
            super().__init__()
            self.requested_limits = []

        def list_keys(self, _prefix: str):
            raise AssertionError("unbounded enumeration must not be used")

        def list_key_page(
            self,
            prefix: str,
            *,
            limit: int,
            continuation_token: str | None = None,
        ):
            self.requested_limits.append(limit)
            return super().list_key_page(
                prefix,
                limit=limit,
                continuation_token=continuation_token,
            )

    storage = BoundedStorage()
    creator = ArtifactDatasetPlane(storage=storage)
    for index in range(300):
        creator.create_artifact(
            artifact_id=f"art_foreign_{index:04d}",
            owner_scope={"project_id": "foreign"},
            run_id="foreign",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            retention_class="persistent",
            body=b"foreign",
        )
    target, target_meta = creator.create_artifact(
        artifact_id="art_expiring",
        owner_scope={"project_id": "project-1"},
        run_id="run-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        retention_class="ephemeral",
        body=b"target",
    )

    cleanup = ArtifactDatasetPlane(storage=storage).cleanup_expired(
        now=float(target_meta["expires_at"]) + 1
    )

    assert cleanup == {
        "artifacts_expired": 1,
        "datasets_expired": 0,
        "cleanup_failed": 0,
    }
    assert len(storage.requested_limits) >= 3
    assert max(storage.requested_limits) <= 128
    assert storage.stat(str(target_meta["object_key"])) is None
    assert storage.stat(f"artifacts/_metadata/{target.artifact_id}.json") is None


def test_cleanup_expired_keeps_sidecar_when_body_delete_is_refused() -> None:
    class RefusingStorage(InMemoryObjectStorage):
        refused_key = ""

        def delete(self, key: str) -> bool:
            if key == self.refused_key:
                return False
            return super().delete(key)

    storage = RefusingStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        retention_class="ephemeral",
        body=b"input",
    )
    storage.refused_key = str(meta["object_key"])

    cleanup = ArtifactDatasetPlane(storage=storage).cleanup_expired(
        now=float(meta["expires_at"]) + 1
    )

    assert cleanup == {
        "artifacts_expired": 0,
        "datasets_expired": 0,
        "cleanup_failed": 1,
    }
    assert storage.stat(str(meta["object_key"])) is not None
    assert storage.stat(f"artifacts/_metadata/{artifact.artifact_id}.json") is not None


def test_bind_replay_owner_allows_only_one_concurrent_job() -> None:
    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, _ = plane.create_dataset(
        owner_scope={"project_id": "project-1"},
        run_id="prelaunch",
        step_id="upload",
        rows=[{"id": 1}],
    )
    barrier = threading.Barrier(2)
    original_get_dataset = plane.get_dataset

    def synchronized_get_dataset(dataset_id: str):
        row = original_get_dataset(dataset_id)
        barrier.wait(timeout=5)
        return row

    plane.get_dataset = synchronized_get_dataset

    def bind(job_id: str):
        try:
            row = plane.bind_replay_owner(
                dataset_id=handle.dataset_id,
                project_id="project-1",
                job_id=job_id,
            )
            return "ok", job_id, row
        except ValueError as exc:
            return "error", job_id, str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(bind, ["job-1", "job-2"]))
    plane.get_dataset = original_get_dataset

    winners = [result for result in results if result[0] == "ok"]
    losers = [result for result in results if result[0] == "error"]
    assert len(winners) == 1
    assert len(losers) == 1
    assert original_get_dataset(handle.dataset_id)["owner_scope"]["job_id"] == winners[0][1]


def test_bind_replay_owner_claim_survives_metadata_write_failure() -> None:
    class FailingMetadataStorage(InMemoryObjectStorage):
        fail_metadata = False

        def put(self, key: str, body: bytes, *, content_type: str):
            if self.fail_metadata and key.endswith("/meta.json"):
                raise RuntimeError("metadata unavailable")
            return super().put(key, body, content_type=content_type)

    storage = FailingMetadataStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, _ = plane.create_dataset(
        owner_scope={"project_id": "project-1"},
        run_id="prelaunch",
        step_id="upload",
        rows=[{"id": 1}],
    )
    storage.fail_metadata = True

    with pytest.raises(RuntimeError, match="metadata unavailable"):
        plane.bind_replay_owner(
            dataset_id=handle.dataset_id,
            project_id="project-1",
            job_id="job-1",
        )

    storage.fail_metadata = False
    with pytest.raises(ValueError, match="already claimed"):
        plane.bind_replay_owner(
            dataset_id=handle.dataset_id,
            project_id="project-1",
            job_id="job-2",
        )
    recovered = plane.bind_replay_owner(
        dataset_id=handle.dataset_id,
        project_id="project-1",
        job_id="job-1",
    )
    assert recovered["owner_scope"]["job_id"] == "job-1"


def _parquet_dataset(
    plane: ArtifactDatasetPlane,
    storage: InMemoryObjectStorage | LocalFilesystemStorageAdapter,
    *,
    rows: list[dict[str, object]],
) -> tuple[str, dict[str, object], bytes]:
    import io

    import polars as pl

    buffer = io.BytesIO()
    frame = pl.DataFrame(rows)
    frame.write_parquet(buffer)
    body = buffer.getvalue()
    handle, meta = plane.create_dataset(
        owner_scope={"account_id": "acc", "project_id": "project"},
        run_id="run",
        step_id="step",
        schema={
            "columns": [
                {"name": name, "type": str(dtype)}
                for name, dtype in zip(frame.columns, frame.dtypes, strict=True)
            ],
            "format": "parquet",
        },
        rows=[],
    )
    storage.delete(str(meta["object_key"]))
    object_key = f"datasets/{handle.dataset_id}/rows.parquet"
    stored = storage.put(
        object_key,
        body,
        content_type="application/vnd.apache.parquet",
    )
    parquet_meta = {
        **meta,
        "object_key": object_key,
        "content_type": "application/vnd.apache.parquet",
        "encoding": "parquet",
        "format": "parquet",
        "row_count": frame.height,
        "size_bytes": stored["size_bytes"],
        "sha256": stored["sha256"],
    }
    plane._datasets[handle.dataset_id] = parquet_meta
    storage.put(
        f"datasets/{handle.dataset_id}/meta.json",
        json.dumps(parquet_meta, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
    )
    return handle.dataset_id, parquet_meta, body


def test_dataset_input_grant_materializes_requested_ndjson_representation(
    tmp_path,
    monkeypatch,
) -> None:
    from core.application.extensions import runtime_grants
    from core.application.management.scope import ScopeContext

    class HttpLocalStorage(LocalFilesystemStorageAdapter):
        def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str:
            return f"https://objects.example/{key}?method={method}&ttl={ttl_seconds}"

    monkeypatch.setenv("FS_SECRET_KEY", "dataset-representation-signing-test")
    storage = HttpLocalStorage(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    dataset_id, source_meta, _body = _parquet_dataset(
        plane,
        storage,
        rows=[{"sku": "00123", "qty": 2}, {"sku": "00002", "qty": 5}],
    )

    prepared = runtime_grants.prepare_extension_grants(
        plane=plane,
        scope=ScopeContext(
            account_id="acc",
            project_id="project",
            allowed_project_ids=frozenset({"project"}),
        ),
        audit={"account_id": "acc", "project_id": "project"},
        extension_id="tabular_fixture",
        dataset_inputs=[
            {
                "dataset_handle": f"dataset:{dataset_id}",
                "binding_key": "tabular_input_dataset",
                "content_type": "application/x-ndjson",
                "max_size_bytes": 1024 * 1024,
            }
        ],
    )

    descriptor = prepared.dataset_inputs[0]
    assert descriptor["dataset_id"] == dataset_id
    assert descriptor["dataset_handle"] == f"dataset:{dataset_id}"
    assert descriptor["binding_key"] == "tabular_input_dataset"
    assert descriptor["content_type"] == "application/x-ndjson"
    assert descriptor["access"]["download_url"].startswith("https://objects.example/datasets/")
    assert descriptor["access"]["platform_grant"]["signature"]
    assert descriptor["object_key"] != source_meta["object_key"]
    assert descriptor["object_key"].endswith("/representations/rows.ndjson")
    assert descriptor["schema"] == {"columns": source_meta["schema"]["columns"]}
    assert descriptor["row_count"] == 2
    ndjson_body = storage.get(descriptor["object_key"])
    assert ndjson_body is not None
    assert [json.loads(line) for line in ndjson_body.splitlines()] == [
        {"sku": "00123", "qty": 2},
        {"sku": "00002", "qty": 5},
    ]
    assert descriptor["size_bytes"] == len(ndjson_body)
    assert descriptor["checksum_sha256"] == hashlib.sha256(ndjson_body).hexdigest()
    assert plane.get_dataset(dataset_id)["object_key"] == source_meta["object_key"]

    cleanup = ArtifactDatasetPlane(storage=storage).cleanup_expired(
        now=float(source_meta["expires_at"]) + 1
    )
    assert cleanup["datasets_expired"] == 1
    assert storage.stat(source_meta["object_key"]) is None
    assert storage.stat(f"datasets/{dataset_id}/meta.json") is None
    assert storage.stat(descriptor["object_key"]) is None
    assert storage.stat(f"{descriptor['object_key']}.json") is None


def test_dataset_representation_rejects_unsupported_conversion() -> None:
    plane = ArtifactDatasetPlane()
    dataset_id, _meta, _body = _parquet_dataset(
        plane,
        plane._storage,
        rows=[{"sku": "A"}],
    )

    with pytest.raises(ValueError, match="unsupported dataset representation"):
        plane.materialize_dataset_representation(
            dataset_id,
            content_type="text/csv",
            max_size_bytes=1024 * 1024,
        )


def test_dataset_representation_is_reused_after_plane_restart() -> None:
    class CountingStorage(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.stream_puts = 0

        def put_stream(self, *args, **kwargs):
            self.stream_puts += 1
            return super().put_stream(*args, **kwargs)

    storage = CountingStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    dataset_id, _meta, _body = _parquet_dataset(
        plane,
        storage,
        rows=[{"sku": "A", "qty": 1}],
    )
    first = plane.materialize_dataset_representation(
        dataset_id,
        content_type="application/x-ndjson",
        max_size_bytes=1024 * 1024,
    )
    restarted = ArtifactDatasetPlane(storage=storage)

    second = restarted.materialize_dataset_representation(
        dataset_id,
        content_type="application/x-ndjson",
        max_size_bytes=1024 * 1024,
    )

    assert storage.stream_puts == 1
    assert second == first


def test_dataset_representation_enforces_output_limit_and_cleans_partial_body() -> None:
    import io

    import polars as pl

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    rows = [{"sku": f"SKU-{index}", "description": "repeat-me-" * 100} for index in range(500)]
    dataset_id, source_meta, parquet_body = _parquet_dataset(plane, storage, rows=rows)
    expected_ndjson = io.BytesIO()
    pl.DataFrame(rows).write_ndjson(expected_ndjson)
    assert len(parquet_body) < len(expected_ndjson.getvalue())
    max_size_bytes = (len(parquet_body) + len(expected_ndjson.getvalue())) // 2

    with pytest.raises(ValueError, match="exceeds max_size_bytes"):
        plane.materialize_dataset_representation(
            dataset_id,
            content_type="application/x-ndjson",
            max_size_bytes=max_size_bytes,
        )

    representation_key = f"datasets/{dataset_id}/representations/rows.ndjson"
    assert storage.stat(representation_key) is None
    assert storage.stat(f"{representation_key}.json") is None
    assert storage.get(str(source_meta["object_key"])) == parquet_body


def test_finalize_dataset_upload_refreshes_metadata_after_presigned_write(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_dataset_grant(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_2",
        step_id="step_dataset",
        schema={"columns": [{"name": "id", "type": "integer"}]},
    )
    rows = [{"id": 1}, {"id": 2}]
    body = json.dumps(rows, separators=(",", ":")).encode("utf-8")

    assert plane.dataset_presigned_url(handle.dataset_id, method="GET") == ""
    put_url = plane.dataset_presigned_url(handle.dataset_id, method="PUT")
    assert put_url.startswith("file://")
    assert "method=PUT" in put_url

    storage.put(str(meta["object_key"]), body, content_type="application/json")
    refreshed = plane.finalize_dataset_upload(handle.dataset_id)

    assert refreshed is not None
    assert refreshed["row_count"] == 2
    assert refreshed["preview_rows"] == rows
    assert refreshed["size_bytes"] == len(body)
    assert refreshed["sha256"] == hashlib.sha256(body).hexdigest()
    get_url = plane.dataset_presigned_url(handle.dataset_id, method="GET")
    assert get_url.startswith("file://")
    assert "method=GET" in get_url


def test_finalize_ndjson_dataset_upload_derives_rows_preview_and_canonical_schema(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_dataset_grant(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_2",
        step_id="step_dataset",
        content_type="application/x-ndjson",
    )
    body = b'{"sku":"A","qty":"2"}\n{"sku":"B","qty":null}\n'
    storage.put(str(meta["object_key"]), body, content_type="application/x-ndjson")

    refreshed = plane.finalize_dataset_upload(handle.dataset_id)

    assert refreshed is not None
    assert refreshed["content_type"] == "application/x-ndjson"
    assert refreshed["row_count"] == 2
    assert refreshed["preview_rows"] == [
        {"sku": "A", "qty": "2"},
        {"sku": "B", "qty": None},
    ]
    assert refreshed["schema"] == {
        "columns": [
            {"name": "sku", "type": "text"},
            {"name": "qty", "type": "text"},
        ]
    }


def test_finalize_ndjson_dataset_upload_rejects_malformed_rows(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_dataset_grant(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_2",
        step_id="step_dataset",
        content_type="application/x-ndjson",
    )
    body = b'{"sku":"A"}\nnot-json\n'
    storage.put(str(meta["object_key"]), body, content_type="application/x-ndjson")

    refreshed = plane.finalize_dataset_upload(handle.dataset_id)

    assert refreshed is not None
    assert refreshed["upload_status"] == "rejected"
    assert refreshed["upload_error"] == "dataset_content_invalid"
    assert refreshed["row_count"] == 0
    assert refreshed["object_deleted"] is True


def test_finalize_dataset_upload_rejects_oversize_before_body_read(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")

    class NoBodyReadStorage(LocalFilesystemStorageAdapter):
        def get(self, key: str) -> bytes | None:
            if str(key).endswith("/rows.json"):
                raise AssertionError("oversized dataset output body should not be read")
            return super().get(key)

    storage = NoBodyReadStorage(root_dir=str(tmp_path / "artifacts"))
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_dataset_grant(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_2",
        step_id="step_dataset",
    )
    object_key = str(meta["object_key"])
    storage.put(object_key, b'[{"id":1},{"id":2}]', content_type="application/json")

    refreshed = plane.finalize_dataset_upload(handle.dataset_id, max_size_bytes=4)

    assert refreshed is not None
    assert refreshed["upload_status"] == "rejected"
    assert refreshed["upload_error"] == "dataset_size_limit_exceeded"
    assert refreshed["sha256"] == ""
    assert refreshed["row_count"] == 0
    assert refreshed["object_deleted"] is True
    assert storage.get(f"datasets/{handle.dataset_id}/meta.json") is not None


def test_public_sdk_streams_dataset_descriptors_from_file_url(tmp_path, monkeypatch) -> None:
    sdk_root = Path(__file__).parent.parent.parent / "core/infrastructure/extension_sdk/public"
    monkeypatch.syspath_prepend(str(sdk_root))
    from flowsteward_extension_sdk import read_dataset_bytes, stream_dataset_bytes

    body = b'[{"id":1},{"id":2}]'
    rows_path = tmp_path / "rows.json"
    rows_path.write_bytes(body)
    payload = {
        "datasets": {
            "inputs": [
                {
                    "binding_key": "rows",
                    "role": "input",
                    "dataset_id": "dst_1",
                    "dataset_handle": "dataset:dst_1",
                    "content_type": "application/json",
                    "size_bytes": len(body),
                    "checksum_sha256": hashlib.sha256(body).hexdigest(),
                    "access": {
                        "transport": "presigned_url",
                        "mode": "read",
                        "download_url": f"{rows_path.as_uri()}?method=GET",
                    },
                }
            ]
        }
    }

    assert list(stream_dataset_bytes(payload, binding_key="rows", chunk_size=5)) == [
        b'[{"id',
        b'":1},',
        b'{"id"',
        b":2}]",
    ]
    assert read_dataset_bytes(payload, binding_key="rows") == body


def test_public_sdk_local_storage_supports_stream_stat_and_put_stream(
    tmp_path, monkeypatch
) -> None:
    sdk_root = Path(__file__).parent.parent.parent / "core/infrastructure/extension_sdk/public"
    monkeypatch.syspath_prepend(str(sdk_root))
    from flowsteward_extension_sdk import LocalFilesystemStorageAdapter as PublicLocalStorage

    storage = PublicLocalStorage(root_dir=str(tmp_path))
    result = storage.put_stream(
        "objects/feed.bin",
        [b"abc", b"def"],
        content_type="application/octet-stream",
        max_size_bytes=10,
    )

    assert result["size_bytes"] == 6
    assert storage.stat("objects/feed.bin") == {"key": "objects/feed.bin", "size_bytes": 6}
    assert list(storage.stream("objects/feed.bin", chunk_size=2)) == [b"ab", b"cd", b"ef"]


def test_bulk_execute_supports_partial_failure_reporting_and_resume_cursor() -> None:
    items = [1, 2, 0, 3]

    def _handler(item: int) -> float:
        return 10 / item

    result = execute_bulk_operation(
        items=items,
        handler=_handler,
        execution_mode="streamed_batch",
        batch_size=2,
        cursor=0,
        partial_failure_mode="report_failed_items",
        idempotency_key="job-77",
    )
    assert result["status"] == "completed_with_failures"
    assert result["execution_mode"] == "streamed_batch"
    assert result["idempotency_key"] == "job-77"
    assert result["processed_count"] == 4
    assert len(result["failures"]) == 1
    assert result["failures"][0]["index"] == 2


def test_bulk_fail_fast_stops_on_first_error_and_returns_next_cursor() -> None:
    items = [5, 0, 1]

    def _handler(item: int) -> float:
        return 1 / item

    result = execute_bulk_operation(
        items=items,
        handler=_handler,
        execution_mode="async_job",
        batch_size=10,
        cursor=0,
        partial_failure_mode="fail_fast",
    )
    assert result["status"] == "failed"
    assert result["processed_count"] == 2
    assert result["next_cursor"] == 2
    assert len(result["failures"]) == 1


def test_failed_items_can_be_emitted_as_dataset_handle() -> None:
    plane = ArtifactDatasetPlane()
    handle = plane.failed_items_to_dataset(
        owner_scope={"account_id": "acc_default", "project_id": 1},
        run_id="run_3",
        step_id="step_c",
        failed_items=[{"index": 4, "item": {"sku": "A1"}, "error": "timeout"}],
    )
    assert handle is not None
    dataset = plane.get_dataset(handle.dataset_id)
    assert dataset is not None
    assert dataset["row_count"] == 1


def _local_plane(tmp_path: Path) -> ArtifactDatasetPlane:
    return ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "objects"))
    )


def test_local_fs_artifact_streams_every_chunk_without_a_whole_object_read(
    tmp_path: Path,
) -> None:
    """local_fs has no browser URL, so the API is the only way its bytes travel."""
    plane = _local_plane(tmp_path)
    body = b"".join(bytes([index % 256]) * 4096 for index in range(8))
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.bin",
        content_type="application/octet-stream",
        body=body,
    )

    local_stream = plane.artifact_local_stream(handle.artifact_id, chunk_size=4096)

    assert local_stream is not None
    chunks, size_bytes = local_stream
    collected = list(chunks)
    assert len(collected) > 1, "a multi-chunk object must arrive in chunks"
    assert b"".join(collected) == body
    # The declared size is the one the chunks add up to.
    assert size_bytes == len(body)


def test_local_fs_stream_is_withheld_when_the_object_is_gone(tmp_path: Path) -> None:
    """Metadata alone must never produce an empty successful download."""
    plane = _local_plane(tmp_path)
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.txt",
        content_type="text/plain",
        body=b"hello",
    )
    row = plane.get_artifact(handle.artifact_id)
    assert row is not None
    os.remove(Path(tmp_path / "objects") / row["object_key"])

    assert plane.artifact_local_stream(handle.artifact_id) is None


def test_server_side_stream_is_not_restricted_to_one_storage_backend() -> None:
    """AC17-R130: the fallback used to be refused to everything but local_fs.

    Object storage with no public endpoint cannot give the browser a URL either,
    and under the old gate its Download button had nothing left to fall back on.
    Whether to stream is the caller's decision; whether the bytes are readable is
    this method's, and it is not answered by the backend's class.
    """
    plane = ArtifactDatasetPlane()
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.txt",
        content_type="text/plain",
        body=b"hello",
    )

    local_stream = plane.artifact_local_stream(handle.artifact_id)

    assert local_stream is not None
    chunks, size_bytes = local_stream
    assert b"".join(chunks) == b"hello"
    assert size_bytes == 5


def test_no_server_side_stream_when_storage_cannot_stream() -> None:
    """The one gate left: a backend with no way to read the bytes back."""
    plane = ArtifactDatasetPlane()
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.txt",
        content_type="text/plain",
        body=b"hello",
    )
    plane._storage = SimpleNamespace(stat=lambda key: {"key": key, "size_bytes": 5})

    assert plane.artifact_local_stream(handle.artifact_id) is None


def test_local_fs_stream_is_withheld_for_an_unknown_artifact(tmp_path: Path) -> None:
    assert _local_plane(tmp_path).artifact_local_stream("art_missing") is None


def test_local_fs_stream_survives_an_object_deleted_after_the_response_starts(
    tmp_path: Path,
) -> None:
    """The object is opened before the caller commits to a 200.

    A generator opens nothing until it is first advanced, which for an HTTP
    caller is after the status line and Content-Length have gone out. The
    object is held open from the moment the stream is handed over, so a delete
    landing afterwards cannot truncate the body.
    """
    plane = _local_plane(tmp_path)
    body = b"x" * 9000
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.bin",
        content_type="application/octet-stream",
        body=body,
    )
    row = plane.get_artifact(handle.artifact_id)
    assert row is not None

    local_stream = plane.artifact_local_stream(handle.artifact_id, chunk_size=4096)
    assert local_stream is not None
    chunks, size_bytes = local_stream
    os.remove(Path(tmp_path / "objects") / row["object_key"])

    assert b"".join(chunks) == body
    assert size_bytes == len(body)


def test_local_fs_stream_is_withheld_when_the_object_vanishes_after_the_stat(
    tmp_path: Path,
) -> None:
    """The window between the existence check and the open is closed too."""
    plane = _local_plane(tmp_path)
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="report.txt",
        content_type="text/plain",
        body=b"hello",
    )
    row = plane.get_artifact(handle.artifact_id)
    assert row is not None
    object_path = Path(tmp_path / "objects") / row["object_key"]

    def stat_then_delete(_self: object, key: str) -> dict[str, object]:
        size_bytes = os.path.getsize(object_path)
        os.remove(object_path)
        return {"key": key, "size_bytes": size_bytes}

    with patch.object(LocalFilesystemStorageAdapter, "stat", stat_then_delete):
        assert plane.artifact_local_stream(handle.artifact_id) is None


def test_local_fs_stream_serves_a_genuinely_empty_object(tmp_path: Path) -> None:
    """Nothing to yield is not the same as nothing to serve."""
    plane = _local_plane(tmp_path)
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_default", "project_id": 7},
        run_id="run_1",
        step_id="step_a",
        filename="empty.txt",
        content_type="text/plain",
        body=b"",
    )

    local_stream = plane.artifact_local_stream(handle.artifact_id)

    assert local_stream is not None
    chunks, size_bytes = local_stream
    assert b"".join(chunks) == b""
    assert size_bytes == 0


def test_local_storage_stays_writable_for_every_role_sharing_the_volume(tmp_path) -> None:
    """AC17-R87: a peer role could not write into directories this adapter created.

    The artifact root is one volume shared by roles running as different uids that meet
    only through the `fs-artifacts` group. The app created the tree first, with the default
    umask, so `/data/artifacts/artifacts` and `_metadata` landed 0755: the worker was not
    the owner, fell through to "other", and every `invoke_workflow` died with
    `[Errno 13] Permission denied: '/data/artifacts/artifacts/_metadata/art_….json'`.
    One missing group bit stopped all parallel work on the profile.
    """
    import os
    import stat

    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter

    previous_umask = os.umask(0o022)
    try:
        adapter = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
        adapter.put("artifacts/_metadata/art_one.json", b"{}", content_type="application/json")
        adapter.put_if_absent("artifacts/exclusive.bin", b"body", content_type="text/plain")
        adapter.put_stream(
            "artifacts/streamed/object.bin",
            [b"chunk"],
            content_type="text/plain",
            max_size_bytes=1024,
        )
    finally:
        os.umask(previous_umask)

    root = tmp_path / "artifacts"
    for relative in ("artifacts", "artifacts/_metadata", "artifacts/streamed"):
        mode = os.stat(root / relative).st_mode
        assert mode & stat.S_IWGRP, f"{relative} is not group-writable: {stat.filemode(mode)}"
        assert mode & stat.S_IXGRP, f"{relative} is not group-traversable"
        assert not mode & stat.S_IWOTH, f"{relative} must not be world-writable"

    for relative in (
        "artifacts/_metadata/art_one.json",
        "artifacts/exclusive.bin",
        "artifacts/streamed/object.bin",
    ):
        mode = os.stat(root / relative).st_mode
        assert mode & stat.S_IWGRP, f"{relative} is not group-writable: {stat.filemode(mode)}"
        assert mode & stat.S_IRGRP, f"{relative} is not group-readable"
        assert not mode & stat.S_IWOTH, f"{relative} must not be world-writable"


def test_local_storage_preserves_setgid_on_directories_it_touches(tmp_path) -> None:
    """The group bits are added, not assigned, so inherited setgid survives.

    Group ownership flows down the shared volume through setgid on the parent. Writing an
    absolute mode here would clear it and break the very inheritance this relies on.
    """
    import os
    import stat

    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter

    root = tmp_path / "artifacts"
    root.mkdir()
    os.chmod(root, 0o2770)

    adapter = LocalFilesystemStorageAdapter(root_dir=str(root))
    adapter.put("artifacts/nested/deep.json", b"{}", content_type="application/json")

    assert os.stat(root).st_mode & stat.S_ISGID, "setgid was stripped from the storage root"
