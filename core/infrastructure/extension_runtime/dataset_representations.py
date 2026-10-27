"""Immutable alternate representations for extension dataset grants."""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_MESSAGE_DATASET_REPRESENTATION_SOURCE_EXCEEDS_MAX_SIZE_BYTES = (
    "dataset representation source exceeds max_size_bytes"
)

NDJSON_CONTENT_TYPE = "application/x-ndjson"
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"
_REPRESENTATION_LIMIT_MARKER = "dataset_representation_size_limit"
_STORAGE_CHUNK_SIZE = 1024 * 1024


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _storage_stream(storage: Any, key: str) -> Iterator[bytes]:
    if hasattr(storage, "stream"):
        yield from storage.stream(key, chunk_size=_STORAGE_CHUNK_SIZE)
        return
    body = storage.get(key)
    if body is not None:
        yield bytes(body)


@contextmanager
def _storage_key_path(
    storage: Any,
    key: str,
    *,
    suffix: str,
    max_bytes: int,
) -> Iterator[str]:
    if not key:
        raise FileNotFoundError("storage object key is missing")
    size_hint = None
    if hasattr(storage, "stat"):
        stat = storage.stat(key)
        if stat is None:
            raise FileNotFoundError(f"Storage object '{key}' not found")
        if isinstance(stat, dict):
            size_hint = _positive_int(stat.get("size_bytes"))
        if size_hint is not None and size_hint > max_bytes:
            raise ValueError(_MESSAGE_DATASET_REPRESENTATION_SOURCE_EXCEEDS_MAX_SIZE_BYTES)
    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        total = 0
        saw_chunk = False
        for chunk in _storage_stream(storage, key):
            data = bytes(chunk or b"")
            if not data:
                continue
            saw_chunk = True
            total += len(data)
            if total > max_bytes:
                raise ValueError(_MESSAGE_DATASET_REPRESENTATION_SOURCE_EXCEEDS_MAX_SIZE_BYTES)
            handle.write(data)
        if not saw_chunk and size_hint not in {0, None}:
            raise FileNotFoundError(f"Storage object '{key}' not found")
        if not saw_chunk and size_hint is None:
            raw = storage.get(key) if hasattr(storage, "get") else None
            if raw is None:
                raise FileNotFoundError(f"Storage object '{key}' not found")
            data = bytes(raw)
            if len(data) > max_bytes:
                raise ValueError(_MESSAGE_DATASET_REPRESENTATION_SOURCE_EXCEEDS_MAX_SIZE_BYTES)
            handle.write(data)
        handle.flush()
        yield handle.name


def _file_chunks(path: str) -> Iterator[bytes]:
    with open(path, "rb") as handle:
        yield from iter(lambda: handle.read(_STORAGE_CHUNK_SIZE), b"")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_STORAGE_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _put_file_to_storage(
    storage: Any,
    object_key: str,
    path: str,
    *,
    content_type: str,
    max_size_bytes: int,
) -> dict[str, Any]:
    if hasattr(storage, "put_stream"):
        result = storage.put_stream(
            object_key,
            _file_chunks(path),
            content_type=content_type,
            max_size_bytes=max_size_bytes,
        )
        return dict(result) if isinstance(result, dict) else {}
    payload = Path(path).read_bytes()
    if len(payload) > max_size_bytes:
        raise ValueError(f"dataset representation exceeds max_size_bytes={max_size_bytes}")
    result = storage.put(object_key, payload, content_type=content_type)
    return dict(result) if isinstance(result, dict) else {}


class _BoundedWriter:
    def __init__(self, handle: Any, *, max_size_bytes: int) -> None:
        self._handle = handle
        self._max_size_bytes = max_size_bytes
        self.size_bytes = 0

    def write(self, value: bytes) -> int:
        data = bytes(value)
        if self.size_bytes + len(data) > self._max_size_bytes:
            raise OSError(_REPRESENTATION_LIMIT_MARKER)
        written = self._handle.write(data)
        self.size_bytes += written
        return written

    def flush(self) -> None:
        self._handle.flush()


def _representation_keys(dataset_id: str) -> tuple[str, str]:
    body_key = f"datasets/{dataset_id}/representations/rows.ndjson"
    return body_key, f"{body_key}.json"


def _cached_representation(
    storage: Any,
    *,
    dataset_id: str,
    source_sha256: str,
    max_size_bytes: int,
) -> dict[str, Any] | None:
    body_key, metadata_key = _representation_keys(dataset_id)
    try:
        raw = storage.get(metadata_key)
        candidate = json.loads(raw) if raw else None
        stat = storage.stat(body_key)
    except Exception:
        return None
    if not isinstance(candidate, dict) or not isinstance(stat, dict):
        return None
    size_bytes = candidate.get("size_bytes")
    if (
        candidate.get("dataset_id") != dataset_id
        or candidate.get("source_sha256") != source_sha256
        or candidate.get("object_key") != body_key
        or candidate.get("content_type") != NDJSON_CONTENT_TYPE
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or int(stat.get("size_bytes", -1)) != size_bytes
    ):
        return None
    if size_bytes > max_size_bytes:
        raise ValueError(f"dataset representation exceeds max_size_bytes={max_size_bytes}")
    return dict(candidate)


def materialize_dataset_representation(
    storage: Any,
    dataset_meta: dict[str, Any],
    *,
    content_type: str,
    max_size_bytes: int,
) -> dict[str, Any]:
    """Create or reuse a bounded immutable representation of a canonical dataset."""
    dataset_id = str(dataset_meta.get("dataset_id") or "").strip()
    object_key = str(dataset_meta.get("object_key") or "").strip()
    source_sha256 = str(dataset_meta.get("sha256") or "").strip()
    source_content_type = str(dataset_meta.get("content_type") or "").strip().lower()
    source_encoding = str(dataset_meta.get("encoding") or "").strip().lower()
    desired_content_type = str(content_type or "").strip().lower()
    max_size = _positive_int(max_size_bytes)
    if not dataset_id or not object_key or not source_sha256:
        raise ValueError("dataset representation source is unavailable")
    if max_size is None:
        raise ValueError("dataset representation max_size_bytes must be positive")
    if desired_content_type != NDJSON_CONTENT_TYPE or not (
        source_content_type == PARQUET_CONTENT_TYPE
        or source_encoding == "parquet"
        or object_key.endswith(".parquet")
    ):
        raise ValueError(
            f"unsupported dataset representation: {source_content_type or source_encoding} "
            f"to {desired_content_type}"
        )
    source_size = dataset_meta.get("size_bytes")
    if (
        isinstance(source_size, bool)
        or not isinstance(source_size, int)
        or source_size < 0
        or source_size > max_size
    ):
        raise ValueError(f"dataset representation source exceeds max_size_bytes={max_size}")

    body_key, metadata_key = _representation_keys(dataset_id)
    cached = _cached_representation(
        storage,
        dataset_id=dataset_id,
        source_sha256=source_sha256,
        max_size_bytes=max_size,
    )
    if cached is not None:
        return cached

    with contextlib.suppress(Exception):
        storage.delete(body_key)
    with contextlib.suppress(Exception):
        storage.delete(metadata_key)

    try:
        with (
            _storage_key_path(
                storage, object_key, suffix=".parquet", max_bytes=max_size
            ) as source_path,
            tempfile.NamedTemporaryFile(suffix=".ndjson") as output,
        ):
            bounded_output = _BoundedWriter(output, max_size_bytes=max_size)
            try:
                import polars as pl

                pl.scan_parquet(source_path).sink_ndjson(bounded_output, engine="streaming")
            except OSError as exc:
                if _REPRESENTATION_LIMIT_MARKER not in str(exc):
                    raise
                raise ValueError(
                    f"dataset representation exceeds max_size_bytes={max_size}"
                ) from exc
            output.flush()
            representation_size = bounded_output.size_bytes
            if Path(output.name).stat().st_size != representation_size:
                raise RuntimeError("dataset representation size verification failed")
            stored = _put_file_to_storage(
                storage,
                body_key,
                output.name,
                content_type=NDJSON_CONTENT_TYPE,
                max_size_bytes=max_size,
            )
            representation = {
                **dataset_meta,
                "object_key": body_key,
                "filename": "rows.ndjson",
                "content_type": NDJSON_CONTENT_TYPE,
                "encoding": "ndjson",
                "format": "ndjson",
                "size_bytes": int(stored.get("size_bytes") or representation_size),
                "sha256": str(stored.get("sha256") or "").strip() or _sha256_file(output.name),
                "source_sha256": source_sha256,
            }
            source_schema = dataset_meta.get("schema")
            if isinstance(source_schema, dict) and isinstance(source_schema.get("columns"), list):
                representation["schema"] = {"columns": list(source_schema["columns"])}
        storage.put(
            metadata_key,
            json.dumps(representation, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
        )
        return representation
    except Exception:
        with contextlib.suppress(Exception):
            storage.delete(body_key)
        with contextlib.suppress(Exception):
            storage.delete(metadata_key)
        raise


__all__ = ["NDJSON_CONTENT_TYPE", "materialize_dataset_representation"]
