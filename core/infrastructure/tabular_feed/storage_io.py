"""Streamed object/file transfer helpers for tabular feed infrastructure."""

from __future__ import annotations

import contextlib
import hashlib
import tempfile
from pathlib import Path
from typing import Any

from core.infrastructure.tabular_feed.common import _as_dict, _limit_env, _positive_int
from core.infrastructure.tabular_feed.constants import (
    DEFAULT_MAX_FEED_BYTES,
    STORAGE_CHUNK_SIZE,
)


def _storage_stream(storage: Any, key: str) -> Any:
    if hasattr(storage, "stream"):
        yield from storage.stream(key, chunk_size=STORAGE_CHUNK_SIZE)
        return
    body = storage.get(key)
    if body is None:
        return
    yield bytes(body)


@contextlib.contextmanager
def _storage_key_path(storage: Any, key: str, *, suffix: str, max_bytes: int | None = None):
    if not key:
        raise FileNotFoundError("storage object key is missing")
    size_hint = None
    if hasattr(storage, "stat"):
        stat = storage.stat(key)
        size_hint = _positive_int(_as_dict(stat).get("size_bytes")) if stat else None
        if stat is None:
            raise FileNotFoundError(f"Storage object '{key}' not found")
        if max_bytes is not None and size_hint is not None and size_hint > max_bytes:
            raise ValueError("feed_too_large")
    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        total = 0
        saw_chunk = False
        for chunk in _storage_stream(storage, key):
            data = bytes(chunk or b"")
            if not data:
                continue
            saw_chunk = True
            total += len(data)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("feed_too_large")
            handle.write(data)
        if not saw_chunk and size_hint not in {0, None}:
            raise FileNotFoundError(f"Storage object '{key}' not found")
        if not saw_chunk and size_hint is None:
            raw = storage.get(key) if hasattr(storage, "get") else None
            if raw is None:
                raise FileNotFoundError(f"Storage object '{key}' not found")
            total = len(raw)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("feed_too_large")
            handle.write(raw)
        handle.flush()
        yield handle.name


def _file_chunks(path: str) -> Any:
    with open(path, "rb") as handle:
        yield from iter(lambda: handle.read(STORAGE_CHUNK_SIZE), b"")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(STORAGE_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preview_text_file(path: str, *, limit: int = 256) -> str:
    with open(path, "rb") as handle:
        return handle.read(limit).decode("utf-8", errors="replace").strip()


def _put_file_to_storage(
    storage: Any,
    object_key: str,
    path: str,
    *,
    content_type: str,
    max_size_bytes: int | None = None,
) -> dict[str, Any]:
    max_size = max_size_bytes or _limit_env("FS_FEED_MAX_BYTES", DEFAULT_MAX_FEED_BYTES)
    if hasattr(storage, "put_stream"):
        return storage.put_stream(
            object_key,
            _file_chunks(path),
            content_type=content_type,
            max_size_bytes=max_size,
        )
    payload = Path(path).read_bytes()
    if len(payload) > max_size:
        raise ValueError("feed_too_large")
    return storage.put(object_key, payload, content_type=content_type)
