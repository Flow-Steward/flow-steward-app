from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request

from .artifact_plane import RETENTION_CLASSES as RETENTION_CLASSES
from .artifact_plane import ArtifactDatasetPlane as ArtifactDatasetPlane
from .artifact_plane import ArtifactHandle as ArtifactHandle
from .artifact_plane import DatasetHandle as DatasetHandle
from .artifact_plane import artifact_plane_from_payload as artifact_plane_from_payload
from .artifact_storage import InMemoryObjectStorage as InMemoryObjectStorage
from .artifact_storage import LocalFilesystemStorageAdapter as LocalFilesystemStorageAdapter
from .artifact_storage import ObjectStorageAdapter as ObjectStorageAdapter
from .artifact_storage import S3ObjectStorageAdapter as S3ObjectStorageAdapter
from .artifact_storage import create_storage_adapter as create_storage_adapter
from .artifact_values import APPLICATION_JSON_VALUE as APPLICATION_JSON_VALUE
from .artifact_values import OCTET_STREAM_VALUE as OCTET_STREAM_VALUE
from .artifact_values import _as_positive_int
from .http import (
    PinnedPeerError,
    open_pinned_url,
    open_platform_artifact_url,
    verified_platform_grant_signature,
)
from .progress import announce_output_ready

_MESSAGE_ARTIFACT_BODY_EXCEEDS_GRANTED_SIZE_LIMIT = "Artifact body exceeds granted size limit"

_consumed_platform_grants: set[str] = set()


class ArtifactAccessError(RuntimeError):
    """Raised when a payload artifact grant cannot be used by the extension."""


def descriptors_from_payload(
    payload: dict[str, Any] | None,
    *,
    kind: str = "artifact",
    role: str | None = None,
) -> list[dict[str, Any]]:
    root = payload if isinstance(payload, dict) else {}
    normalized_kind = _normalize_descriptor_kind(kind)
    collection_key = "datasets" if normalized_kind == "dataset" else "artifacts"
    raw_collection = root.get(collection_key)
    collection: dict[str, Any] = raw_collection if isinstance(raw_collection, dict) else {}
    roles = [_normalize_descriptor_role(role)] if role else ["input", "output"]
    rows: list[dict[str, Any]] = []
    for item_role in roles:
        key = "inputs" if item_role == "input" else "outputs"
        raw_rows = collection.get(key)
        if not isinstance(raw_rows, list):
            continue
        for row in raw_rows:
            if isinstance(row, dict):
                rows.append(dict(row))
    return rows


def artifact_descriptors_from_payload(
    payload: dict[str, Any] | None,
    *,
    role: str | None = None,
) -> list[dict[str, Any]]:
    return descriptors_from_payload(payload, kind="artifact", role=role)


def dataset_descriptors_from_payload(
    payload: dict[str, Any] | None,
    *,
    role: str | None = None,
) -> list[dict[str, Any]]:
    return descriptors_from_payload(payload, kind="dataset", role=role)


def find_descriptor(
    payload: dict[str, Any] | None,
    *,
    kind: str = "artifact",
    object_id: str = "",
    binding_key: str = "",
    role: str = "input",
) -> dict[str, Any]:
    normalized_kind = _normalize_descriptor_kind(kind)
    normalized_role = _normalize_descriptor_role(role)
    normalized_object_id = _normalize_lookup_id(object_id, kind=normalized_kind)
    normalized_binding_key = str(binding_key or "").strip()
    candidates = descriptors_from_payload(payload, kind=normalized_kind, role=normalized_role)
    if not normalized_object_id and not normalized_binding_key:
        if not candidates:
            raise ArtifactAccessError(
                f"No {normalized_role} {normalized_kind} descriptors in payload"
            )
        return dict(candidates[0])
    id_field = f"{normalized_kind}_id"
    handle_field = f"{normalized_kind}_handle"
    for descriptor in candidates:
        row_id = _normalize_lookup_id(str(descriptor.get(id_field) or ""), kind=normalized_kind)
        row_handle = _normalize_lookup_id(
            str(descriptor.get(handle_field) or ""),
            kind=normalized_kind,
        )
        if normalized_kind == "dataset":
            row_compat_id = _normalize_lookup_id(
                str(descriptor.get("artifact_id") or ""),
                kind=normalized_kind,
            )
            row_compat_handle = _normalize_lookup_id(
                str(descriptor.get("artifact_handle") or ""),
                kind=normalized_kind,
            )
        else:
            row_compat_id = ""
            row_compat_handle = ""
        row_binding = str(descriptor.get("binding_key") or "").strip()
        if normalized_object_id and normalized_object_id in {
            row_id,
            row_handle,
            row_compat_id,
            row_compat_handle,
        }:
            return dict(descriptor)
        if normalized_binding_key and normalized_binding_key == row_binding:
            return dict(descriptor)
    raise ArtifactAccessError(
        f"{normalized_kind.title()} descriptor not found for role '{normalized_role}'"
    )


def find_artifact_descriptor(
    payload: dict[str, Any] | None,
    *,
    artifact_id: str = "",
    binding_key: str = "",
    role: str = "input",
) -> dict[str, Any]:
    return find_descriptor(
        payload,
        kind="artifact",
        object_id=artifact_id,
        binding_key=binding_key,
        role=role,
    )


def find_dataset_descriptor(
    payload: dict[str, Any] | None,
    *,
    dataset_id: str = "",
    binding_key: str = "",
    role: str = "input",
) -> dict[str, Any]:
    return find_descriptor(
        payload,
        kind="dataset",
        object_id=dataset_id,
        binding_key=binding_key,
        role=role,
    )


def stream_descriptor_bytes(
    payload: dict[str, Any] | None,
    *,
    kind: str = "artifact",
    object_id: str = "",
    binding_key: str = "",
    role: str = "input",
    chunk_size: int = 1024 * 1024,
    timeout_seconds: float = 30.0,
) -> Iterator[bytes]:
    normalized_kind = _normalize_descriptor_kind(kind)
    descriptor = find_descriptor(
        payload,
        kind=normalized_kind,
        object_id=object_id,
        binding_key=binding_key,
        role=role,
    )
    access = _descriptor_access(descriptor, mode="read")
    url = str(access.get("download_url") or "").strip()
    if not url:
        raise ArtifactAccessError(f"Input {normalized_kind} descriptor does not include a URL")
    yield from _stream_presigned_url(
        url,
        descriptor=descriptor,
        access=access,
        headers=_descriptor_headers(access),
        timeout_seconds=timeout_seconds,
        chunk_size=chunk_size,
    )


def stream_artifact_bytes(
    payload: dict[str, Any] | None,
    *,
    artifact_id: str = "",
    binding_key: str = "",
    chunk_size: int = 1024 * 1024,
    timeout_seconds: float = 30.0,
) -> Iterator[bytes]:
    yield from stream_descriptor_bytes(
        payload,
        kind="artifact",
        object_id=artifact_id,
        binding_key=binding_key,
        chunk_size=chunk_size,
        timeout_seconds=timeout_seconds,
    )


def stream_dataset_bytes(
    payload: dict[str, Any] | None,
    *,
    dataset_id: str = "",
    binding_key: str = "",
    chunk_size: int = 1024 * 1024,
    timeout_seconds: float = 30.0,
) -> Iterator[bytes]:
    yield from stream_descriptor_bytes(
        payload,
        kind="dataset",
        object_id=dataset_id,
        binding_key=binding_key,
        chunk_size=chunk_size,
        timeout_seconds=timeout_seconds,
    )


def write_descriptor_stream(
    payload: dict[str, Any] | None,
    chunks: Iterable[bytes | bytearray | memoryview | str],
    *,
    kind: str = "artifact",
    object_id: str = "",
    binding_key: str = "",
    content_type: str = "",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    normalized_kind = _normalize_descriptor_kind(kind)
    descriptor = find_descriptor(
        payload,
        kind=normalized_kind,
        object_id=object_id,
        binding_key=binding_key,
        role="output",
    )
    access = _descriptor_access(descriptor, mode="write")
    url = str(access.get("upload_url") or "").strip()
    if not url:
        raise ArtifactAccessError(f"Output {normalized_kind} descriptor does not include a URL")
    resolved_content_type = (
        str(content_type or descriptor.get("content_type") or OCTET_STREAM_VALUE).strip()
        or OCTET_STREAM_VALUE
    )
    _enforce_platform_grant_content_type(descriptor, access, resolved_content_type)
    result = _write_presigned_url_stream(
        url,
        chunks,
        descriptor=descriptor,
        access=access,
        headers={
            **_descriptor_headers(access),
            "Content-Type": resolved_content_type,
        },
        timeout_seconds=timeout_seconds,
    )
    id_field = f"{normalized_kind}_id"
    handle_field = f"{normalized_kind}_handle"
    written_id = str(descriptor.get(id_field) or descriptor.get("artifact_id") or "").strip()
    announce_output_ready(normalized_kind, written_id)
    return {
        id_field: written_id,
        handle_field: str(
            descriptor.get(handle_field) or descriptor.get("artifact_handle") or ""
        ).strip(),
        "binding_key": str(descriptor.get("binding_key") or "").strip(),
        "content_type": resolved_content_type,
        **result,
    }


def write_artifact_stream(
    payload: dict[str, Any] | None,
    chunks: Iterable[bytes | bytearray | memoryview | str],
    *,
    artifact_id: str = "",
    binding_key: str = "",
    content_type: str = "",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    return write_descriptor_stream(
        payload,
        chunks,
        kind="artifact",
        object_id=artifact_id,
        binding_key=binding_key,
        content_type=content_type,
        timeout_seconds=timeout_seconds,
    )


def write_dataset_stream(
    payload: dict[str, Any] | None,
    chunks: Iterable[bytes | bytearray | memoryview | str],
    *,
    dataset_id: str = "",
    binding_key: str = "",
    content_type: str = APPLICATION_JSON_VALUE,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    return write_descriptor_stream(
        payload,
        chunks,
        kind="dataset",
        object_id=dataset_id,
        binding_key=binding_key,
        content_type=content_type,
        timeout_seconds=timeout_seconds,
    )


def descriptor_to_local_path(
    payload: dict[str, Any] | None,
    *,
    kind: str = "artifact",
    object_id: str = "",
    binding_key: str = "",
    suffix: str = "",
    timeout_seconds: float = 30.0,
) -> str:
    """Return a local path for an input descriptor.

    ``file://`` grants return the live store path and must be treated as
    read-only. Remote grants are copied to a caller-owned tempfile; callers
    should remove that file when they no longer need it.
    """
    normalized_kind = _normalize_descriptor_kind(kind)
    descriptor = find_descriptor(
        payload,
        kind=normalized_kind,
        object_id=object_id,
        binding_key=binding_key,
        role="input",
    )
    access = _descriptor_access(descriptor, mode="read")
    url = str(access.get("download_url") or "").strip()
    if not url:
        raise ArtifactAccessError(f"Input {normalized_kind} descriptor does not include a URL")
    if str(urlparse(url).scheme or "").lower() == "file":
        return _file_url_path(url)
    fd, path = tempfile.mkstemp(
        prefix=f"flowsteward_{normalized_kind}_",
        suffix=suffix,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in _stream_presigned_url(
                url,
                descriptor=descriptor,
                access=access,
                headers=_descriptor_headers(access),
                timeout_seconds=timeout_seconds,
            ):
                handle.write(chunk)
    except Exception:
        with contextlib.suppress(Exception):
            os.unlink(path)
        raise
    return path


def artifact_to_local_path(
    payload: dict[str, Any] | None,
    *,
    artifact_id: str = "",
    binding_key: str = "",
    suffix: str = "",
    timeout_seconds: float = 30.0,
) -> str:
    return descriptor_to_local_path(
        payload,
        kind="artifact",
        object_id=artifact_id,
        binding_key=binding_key,
        suffix=suffix,
        timeout_seconds=timeout_seconds,
    )


def dataset_to_local_path(
    payload: dict[str, Any] | None,
    *,
    dataset_id: str = "",
    binding_key: str = "",
    suffix: str = ".json",
    timeout_seconds: float = 30.0,
) -> str:
    return descriptor_to_local_path(
        payload,
        kind="dataset",
        object_id=dataset_id,
        binding_key=binding_key,
        suffix=suffix,
        timeout_seconds=timeout_seconds,
    )


def read_artifact_bytes(
    payload: dict[str, Any] | None,
    *,
    artifact_id: str = "",
    binding_key: str = "",
    timeout_seconds: float = 30.0,
) -> bytes:
    descriptor = find_artifact_descriptor(
        payload,
        artifact_id=artifact_id,
        binding_key=binding_key,
        role="input",
    )
    access = _descriptor_access(descriptor, mode="read")
    url = str(access.get("download_url") or "").strip()
    if not url:
        raise ArtifactAccessError("Input artifact descriptor does not include a download URL")
    return b"".join(
        _stream_presigned_url(
            url,
            descriptor=descriptor,
            access=access,
            headers=_descriptor_headers(access),
            timeout_seconds=timeout_seconds,
        )
    )


def read_dataset_bytes(
    payload: dict[str, Any] | None,
    *,
    dataset_id: str = "",
    binding_key: str = "",
    timeout_seconds: float = 30.0,
) -> bytes:
    return b"".join(
        stream_dataset_bytes(
            payload,
            dataset_id=dataset_id,
            binding_key=binding_key,
            timeout_seconds=timeout_seconds,
        )
    )


def write_artifact_bytes(
    payload: dict[str, Any] | None,
    body: bytes | bytearray | memoryview | str,
    *,
    artifact_id: str = "",
    binding_key: str = "",
    content_type: str = "",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    descriptor = find_artifact_descriptor(
        payload,
        artifact_id=artifact_id,
        binding_key=binding_key,
        role="output",
    )
    access = _descriptor_access(descriptor, mode="write")
    url = str(access.get("upload_url") or "").strip()
    if not url:
        raise ArtifactAccessError("Output artifact descriptor does not include an upload URL")
    resolved_content_type = (
        str(content_type or descriptor.get("content_type") or OCTET_STREAM_VALUE).strip()
        or OCTET_STREAM_VALUE
    )
    _enforce_platform_grant_content_type(descriptor, access, resolved_content_type)
    payload_bytes = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    _enforce_artifact_body_limits(descriptor, access, payload_bytes)
    _write_presigned_url(
        url,
        payload_bytes,
        descriptor=descriptor,
        access=access,
        headers={
            **_descriptor_headers(access),
            "Content-Type": resolved_content_type,
        },
        timeout_seconds=timeout_seconds,
    )
    written_id = str(descriptor.get("artifact_id") or "").strip()
    announce_output_ready("artifact", written_id)
    return {
        "artifact_id": written_id,
        "artifact_handle": str(descriptor.get("artifact_handle") or "").strip(),
        "binding_key": str(descriptor.get("binding_key") or "").strip(),
        "content_type": resolved_content_type,
        "size_bytes": len(payload_bytes),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }


def write_dataset_bytes(
    payload: dict[str, Any] | None,
    body: bytes | bytearray | memoryview | str,
    *,
    dataset_id: str = "",
    binding_key: str = "",
    content_type: str = APPLICATION_JSON_VALUE,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    payload_bytes = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    return write_dataset_stream(
        payload,
        [payload_bytes],
        dataset_id=dataset_id,
        binding_key=binding_key,
        content_type=content_type,
        timeout_seconds=timeout_seconds,
    )


def _normalize_descriptor_role(role: str | None) -> str:
    normalized = str(role or "input").strip().lower()
    return "output" if normalized in {"output", "outputs", "write"} else "input"


def _normalize_descriptor_kind(kind: str | None) -> str:
    normalized = str(kind or "artifact").strip().lower()
    return "dataset" if normalized in {"dataset", "datasets"} else "artifact"


def _normalize_lookup_id(value: str, *, kind: str) -> str:
    normalized_kind = _normalize_descriptor_kind(kind)
    normalized = str(value or "").strip()
    prefix = f"{normalized_kind}:"
    if normalized.startswith(prefix):
        return normalized.split(":", 1)[1]
    return normalized


def _descriptor_access(descriptor: dict[str, Any], *, mode: str) -> dict[str, Any]:
    raw_access = descriptor.get("access")
    access: dict[str, Any] = raw_access if isinstance(raw_access, dict) else {}
    transport = str(access.get("transport") or "").strip()
    if transport != "presigned_url":
        raise ArtifactAccessError(
            f"Artifact transport '{transport or 'handle_only'}' is not usable"
        )
    access_mode = str(access.get("mode") or "").strip().lower()
    if mode == "read" and access_mode not in {"read", "read_write"}:
        raise ArtifactAccessError("Artifact grant does not allow reads")
    if mode == "write" and access_mode not in {"write", "read_write"}:
        raise ArtifactAccessError("Artifact grant does not allow writes")
    return dict(access)


def _descriptor_headers(access: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    raw_headers = access.get("required_headers")
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            header = str(key or "").strip()
            if header:
                headers[header] = str(value or "")
    return headers


def _private_upload_authority(
    descriptor: dict[str, Any], access: dict[str, Any]
) -> tuple[bool, str]:
    signature = verified_platform_grant_signature(descriptor, access)
    if signature and signature in _consumed_platform_grants:
        raise ArtifactAccessError("Platform artifact upload grant has already been used")
    return bool(signature), signature


def _enforce_platform_grant_content_type(
    descriptor: dict[str, Any],
    access: dict[str, Any],
    content_type: str,
) -> None:
    if not isinstance(access.get("platform_grant"), dict):
        return
    granted_content_type = str(descriptor.get("content_type") or "").strip()
    if not granted_content_type or content_type != granted_content_type:
        raise ArtifactAccessError("Artifact content type does not match the platform upload grant")


def _enforce_artifact_body_limits(
    descriptor: dict[str, Any],
    access: dict[str, Any],
    body: bytes,
) -> None:
    limit = _as_positive_int(access.get("max_size_bytes"))
    if limit is None:
        limit = _as_positive_int(descriptor.get("size_bytes"))
    if limit is not None and len(body) > limit:
        raise ArtifactAccessError(_MESSAGE_ARTIFACT_BODY_EXCEEDS_GRANTED_SIZE_LIMIT)


def _descriptor_size_limit(descriptor: dict[str, Any], access: dict[str, Any]) -> int | None:
    limit = _as_positive_int(access.get("max_size_bytes"))
    if limit is None:
        limit = _as_positive_int(descriptor.get("size_bytes"))
    return limit


def _normalize_chunk(chunk: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(chunk, str):
        return chunk.encode("utf-8")
    if not isinstance(chunk, (bytes, bytearray, memoryview)):
        raise ArtifactAccessError("Artifact stream chunks must be bytes-like objects or strings")
    return bytes(chunk)


def _stream_presigned_url(
    url: str,
    *,
    descriptor: dict[str, Any],
    access: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    parsed = urlparse(url)
    scheme = str(parsed.scheme or "").lower()
    limit = _descriptor_size_limit(descriptor, access)
    expected_sha = str(descriptor.get("checksum_sha256") or "").strip()
    digest = hashlib.sha256()
    total = 0

    def _checked(chunks: Iterable[bytes]) -> Iterator[bytes]:
        nonlocal total
        for raw_chunk in chunks:
            chunk = bytes(raw_chunk)
            if not chunk:
                continue
            next_total = total + len(chunk)
            if limit is not None and next_total > limit:
                raise ArtifactAccessError(_MESSAGE_ARTIFACT_BODY_EXCEEDS_GRANTED_SIZE_LIMIT)
            total = next_total
            digest.update(chunk)
            yield chunk

    if scheme == "file":
        with open(_file_url_path(url), "rb") as handle:
            yield from _checked(iter(lambda: handle.read(max(1, int(chunk_size or 1))), b""))
    elif scheme in {"http", "https"}:
        request = Request(url, method="GET", headers=headers)
        try:
            if verified_platform_grant_signature(descriptor, access):
                response_context = open_platform_artifact_url(
                    request,
                    descriptor=descriptor,
                    access=access,
                    timeout_seconds=float(timeout_seconds),
                )
            else:
                response_context = open_pinned_url(
                    request,
                    timeout_seconds=float(timeout_seconds),
                    purpose="artifact download URL",
                )
            with response_context as response:
                yield from _checked(iter(lambda: response.read(max(1, int(chunk_size or 1))), b""))
        except ArtifactAccessError:
            raise
        except (PinnedPeerError, OSError) as exc:
            raise ArtifactAccessError("Input artifact download failed") from exc
    else:
        raise ArtifactAccessError(f"Unsupported artifact download URL scheme '{scheme}'")

    if expected_sha and digest.hexdigest() != expected_sha:
        raise ArtifactAccessError("Input artifact checksum mismatch")


class _LimitedChunkIterator:
    def __init__(
        self,
        chunks: Iterable[bytes | bytearray | memoryview | str],
        *,
        descriptor: dict[str, Any],
        access: dict[str, Any],
    ) -> None:
        self._chunks = iter(chunks)
        self._limit = _descriptor_size_limit(descriptor, access)
        self._digest = hashlib.sha256()
        self._total = 0

    def __iter__(self) -> _LimitedChunkIterator:
        return self

    def __next__(self) -> bytes:
        while True:
            chunk = _normalize_chunk(next(self._chunks))
            if chunk:
                break
        next_total = self._total + len(chunk)
        if self._limit is not None and next_total > self._limit:
            raise ArtifactAccessError(_MESSAGE_ARTIFACT_BODY_EXCEEDS_GRANTED_SIZE_LIMIT)
        self._total = next_total
        self._digest.update(chunk)
        return chunk

    def result(self) -> dict[str, Any]:
        return {
            "size_bytes": self._total,
            "sha256": self._digest.hexdigest(),
        }


def _spool_limited_chunks(
    chunks: Iterable[bytes | bytearray | memoryview | str],
    *,
    descriptor: dict[str, Any],
    access: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    iterator = _LimitedChunkIterator(chunks, descriptor=descriptor, access=access)
    fd, path = tempfile.mkstemp(prefix="flowsteward_upload_")
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in iterator:
                handle.write(chunk)
        return path, iterator.result()
    except Exception:
        with contextlib.suppress(Exception):
            os.unlink(path)
        raise


def _write_presigned_url_stream(
    url: str,
    chunks: Iterable[bytes | bytearray | memoryview | str],
    *,
    descriptor: dict[str, Any],
    access: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    parsed = urlparse(url)
    scheme = str(parsed.scheme or "").lower()
    iterator = _LimitedChunkIterator(chunks, descriptor=descriptor, access=access)
    if scheme == "file":
        path = _file_url_path(url)
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path) or 'artifact'}.",
            dir=directory,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in iterator:
                    handle.write(chunk)
            result = iterator.result()
            os.replace(tmp_path, path)
            return result
        except Exception:
            with contextlib.suppress(Exception):
                os.unlink(tmp_path)
            raise
    if scheme in {"http", "https"}:
        allow_private, signature = _private_upload_authority(descriptor, access)
        path, result = _spool_limited_chunks(chunks, descriptor=descriptor, access=access)
        try:
            request_headers = dict(headers)
            request_headers["Content-Length"] = str(result["size_bytes"])
            with open(path, "rb") as handle:
                request = Request(url, data=handle, method="PUT", headers=request_headers)
                if allow_private:
                    response_context = open_platform_artifact_url(
                        request,
                        descriptor=descriptor,
                        access=access,
                        timeout_seconds=float(timeout_seconds),
                    )
                else:
                    response_context = open_pinned_url(
                        request,
                        timeout_seconds=float(timeout_seconds),
                        purpose="artifact upload URL",
                    )
                with response_context as response:
                    response.read()
            if signature:
                _consumed_platform_grants.add(signature)
            return result
        finally:
            with contextlib.suppress(Exception):
                os.unlink(path)
    raise ArtifactAccessError(f"Unsupported artifact upload URL scheme '{scheme}'")


def _write_presigned_url(
    url: str,
    body: bytes,
    *,
    descriptor: dict[str, Any],
    access: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
) -> None:
    parsed = urlparse(url)
    scheme = str(parsed.scheme or "").lower()
    if scheme == "file":
        path = _file_url_path(url)
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path) or 'artifact'}.",
            dir=directory,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            os.replace(tmp_path, path)
        except Exception:
            with contextlib.suppress(Exception):
                os.unlink(tmp_path)
            raise
        return
    if scheme in {"http", "https"}:
        allow_private, signature = _private_upload_authority(descriptor, access)
        request_headers = dict(headers)
        request_headers["Content-Length"] = str(len(body))
        request = Request(url, data=body, method="PUT", headers=request_headers)
        if allow_private:
            response_context = open_platform_artifact_url(
                request,
                descriptor=descriptor,
                access=access,
                timeout_seconds=float(timeout_seconds),
            )
        else:
            response_context = open_pinned_url(
                request,
                timeout_seconds=float(timeout_seconds),
                purpose="artifact upload URL",
            )
        with response_context as response:
            response.read()
        if signature:
            _consumed_platform_grants.add(signature)
        return
    raise ArtifactAccessError(f"Unsupported artifact upload URL scheme '{scheme}'")


def _file_url_path(url: str) -> str:
    parsed = urlparse(url)
    if str(parsed.scheme or "").lower() != "file":
        raise ArtifactAccessError("Expected file URL")
    path = unquote(parsed.path or "")
    if not path:
        raise ArtifactAccessError("File artifact URL does not include a path")
    return path


__all__ = [
    "APPLICATION_JSON_VALUE",
    "OCTET_STREAM_VALUE",
    "ArtifactAccessError",
    "ArtifactDatasetPlane",
    "ArtifactHandle",
    "DatasetHandle",
    "InMemoryObjectStorage",
    "LocalFilesystemStorageAdapter",
    "ObjectStorageAdapter",
    "S3ObjectStorageAdapter",
    "artifact_descriptors_from_payload",
    "artifact_plane_from_payload",
    "artifact_to_local_path",
    "create_storage_adapter",
    "dataset_descriptors_from_payload",
    "dataset_to_local_path",
    "descriptor_to_local_path",
    "descriptors_from_payload",
    "find_artifact_descriptor",
    "find_dataset_descriptor",
    "find_descriptor",
    "read_artifact_bytes",
    "read_dataset_bytes",
    "stream_artifact_bytes",
    "stream_dataset_bytes",
    "stream_descriptor_bytes",
    "write_artifact_bytes",
    "write_artifact_stream",
    "write_dataset_bytes",
    "write_dataset_stream",
    "write_descriptor_stream",
]
