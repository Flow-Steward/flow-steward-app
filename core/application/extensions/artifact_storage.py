"""Local artifact storage and backend adapter composition."""

from __future__ import annotations

import contextlib
import hashlib
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.extensions.artifact_plane_values import REPLAY_CLEANUP_COMPLETION_PREFIX
from core.infrastructure.extension_runtime.object_storage import (
    _MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER,
    _MESSAGE_OBJECT_STREAM_EXCEEDS_MAX_SIZE_BYTES,
    InMemoryObjectStorage,
    ObjectStorageAdapter,
    S3ObjectStorageAdapter,
    _bounded_page_limit,
    _positive_int,
)

_MESSAGE_LOCAL_FILESYSTEM_STORAGE_ADAPTER_IS_CLOSED = "local filesystem storage adapter is closed"


LOCAL_STORAGE_CURSOR_TTL_SECONDS = 300.0


LOCAL_STORAGE_CURSOR_MAX = 256


@dataclass(slots=True)
class _LocalFilesystemScanFrame:
    iterator: Any


@dataclass(slots=True)
class _LocalFilesystemListCursor:
    prefix: str
    storage_root: str
    frames: list[_LocalFilesystemScanFrame]
    buffered_key: str | None
    expires_at: float
    sequence: int = 0


# AC17-R87: the local artifact root is a volume shared by roles that run as different
# uids and meet only through the `fs-artifacts` group, so anything created under it has to
# stay group-accessible. The default umask strips exactly that bit: directories landed 0755
# and files 0644 or 0600, which let the creating role write and left every peer role with
# EACCES. Bits are added rather than assigned so that a parent's setgid -- which is what
# keeps the group ownership flowing down -- survives, and so that "other" is never widened.
_SHARED_DIRECTORY_GROUP_BITS = 0o070


_SHARED_FILE_GROUP_BITS = 0o060


def _share_with_storage_group(path: str, *, bits: int) -> None:
    """Grant the owning group access to a shared-storage path, best effort."""
    import os

    try:
        current = os.stat(path).st_mode
    except OSError:
        return
    desired = current | bits
    if desired == current:
        return
    with contextlib.suppress(OSError):
        os.chmod(path, desired)


class LocalFilesystemStorageAdapter:
    """Object storage backed by local filesystem. Suitable for single-node dev."""

    def __init__(
        self,
        *,
        root_dir: str,
        key_prefix: str = "",
        list_cursor_ttl_seconds: float = LOCAL_STORAGE_CURSOR_TTL_SECONDS,
        list_cursor_max: int = LOCAL_STORAGE_CURSOR_MAX,
    ):
        import os

        self._root = os.path.abspath(root_dir)
        self._key_prefix = str(key_prefix or "").strip().strip("/")
        self.key_prefix = f"{self._key_prefix}/" if self._key_prefix else ""
        self._list_cursor_ttl_seconds = max(1.0, float(list_cursor_ttl_seconds))
        self._list_cursor_max = min(LOCAL_STORAGE_CURSOR_MAX, max(1, int(list_cursor_max)))
        self._list_cursors: OrderedDict[str, _LocalFilesystemListCursor] = OrderedDict()
        self._list_cursor_lock = threading.RLock()
        self._closed = False
        os.makedirs(self._root, exist_ok=True)
        _share_with_storage_group(self._root, bits=_SHARED_DIRECTORY_GROUP_BITS)

    @staticmethod
    def _close_list_cursor(cursor: _LocalFilesystemListCursor) -> None:
        while cursor.frames:
            frame = cursor.frames.pop()
            close_fn = getattr(frame.iterator, "close", None)
            if callable(close_fn):
                with contextlib.suppress(Exception):
                    close_fn()

    @staticmethod
    def _next_list_cursor_key(cursor: _LocalFilesystemListCursor) -> str:
        import os

        while cursor.frames:
            frame = cursor.frames[-1]
            try:
                entry = next(frame.iterator)
            except StopIteration:
                cursor.frames.pop()
                close_fn = getattr(frame.iterator, "close", None)
                if callable(close_fn):
                    close_fn()
                continue
            if entry.is_dir(follow_symlinks=False):
                directory_key = (
                    os.path.relpath(entry.path, cursor.storage_root).replace(os.sep, "/") + "/"
                )
                if not (
                    cursor.prefix.startswith(directory_key)
                    or directory_key.startswith(cursor.prefix)
                ):
                    continue
                cursor.frames.append(_LocalFilesystemScanFrame(iterator=os.scandir(entry.path)))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            key = os.path.relpath(entry.path, cursor.storage_root).replace(os.sep, "/")
            if key.startswith(cursor.prefix):
                return key
        raise StopIteration

    def _expire_list_cursors_unlocked(self, now: float) -> None:
        expired_tokens = [
            token for token, cursor in self._list_cursors.items() if cursor.expires_at <= now
        ]
        for token in expired_tokens:
            cursor = self._list_cursors.pop(token)
            self._close_list_cursor(cursor)

    def _store_list_cursor(self, cursor: _LocalFilesystemListCursor) -> str:
        now = time.monotonic()
        cursor.expires_at = now + self._list_cursor_ttl_seconds
        cursor.sequence += 1
        token = f"localfs:{uuid.uuid4().hex}:{cursor.sequence}"
        with self._list_cursor_lock:
            if self._closed:
                self._close_list_cursor(cursor)
                raise RuntimeError(_MESSAGE_LOCAL_FILESYSTEM_STORAGE_ADAPTER_IS_CLOSED)
            self._expire_list_cursors_unlocked(now)
            while len(self._list_cursors) >= self._list_cursor_max:
                _old_token, old_cursor = self._list_cursors.popitem(last=False)
                self._close_list_cursor(old_cursor)
            self._list_cursors[token] = cursor
        return token

    def close(self) -> None:
        with self._list_cursor_lock:
            self._closed = True
            cursors = list(self._list_cursors.values())
            self._list_cursors.clear()
        for cursor in cursors:
            self._close_list_cursor(cursor)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def _full_key(self, key: str) -> str:
        safe = str(key or "").strip().lstrip("/")
        return f"{self._key_prefix}/{safe}" if self._key_prefix else safe

    def _path(self, key: str) -> str:
        import os

        safe = self._full_key(key)
        full = os.path.abspath(os.path.join(self._root, safe))
        if os.path.commonpath([self._root, full]) != self._root:
            raise ValueError(f"Storage key escapes root: {key}")
        return full

    def _ensure_shared_directory(self, path: str) -> None:
        """Create a storage directory that peer roles in the group can also write to."""
        import os

        os.makedirs(path, exist_ok=True)
        relative = os.path.relpath(path, self._root)
        if relative.startswith(os.pardir):
            return
        current = self._root
        _share_with_storage_group(current, bits=_SHARED_DIRECTORY_GROUP_BITS)
        for part in relative.split(os.sep):
            if not part or part == os.curdir:
                continue
            current = os.path.join(current, part)
            _share_with_storage_group(current, bits=_SHARED_DIRECTORY_GROUP_BITS)

    def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, Any]:
        _ = content_type
        import os

        path = self._path(key)
        self._ensure_shared_directory(os.path.dirname(path))
        payload = bytes(body)
        checksum = hashlib.sha256(payload).hexdigest()
        with open(path, "wb") as f:
            f.write(payload)
        _share_with_storage_group(path, bits=_SHARED_FILE_GROUP_BITS)
        return {"key": key, "size_bytes": len(payload), "sha256": checksum}

    def put_if_absent(self, key: str, body: bytes, *, content_type: str) -> bool:
        _ = content_type
        import os

        path = self._path(key)
        self._ensure_shared_directory(os.path.dirname(path))
        payload = bytes(body)
        parent = os.path.dirname(path)
        fd, temporary_path = tempfile.mkstemp(prefix=".conditional-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _share_with_storage_group(temporary_path, bits=_SHARED_FILE_GROUP_BITS)
            try:
                # Both names are on the same filesystem. Link publishes a fully
                # written inode atomically and refuses to replace an existing key.
                os.link(temporary_path, path)
            except FileExistsError:
                return False
            return True
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)

    def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str,
        max_size_bytes: int,
    ) -> dict[str, Any]:
        _ = content_type
        import os

        max_size = _positive_int(max_size_bytes)
        if max_size is None:
            raise ValueError(_MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER)
        path = self._path(key)
        self._ensure_shared_directory(os.path.dirname(path))
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            dir=os.path.dirname(path),
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                for chunk in chunks:
                    data = bytes(chunk or b"")
                    if not data:
                        continue
                    if size + len(data) > max_size:
                        raise ValueError(_MESSAGE_OBJECT_STREAM_EXCEEDS_MAX_SIZE_BYTES)
                    f.write(data)
                    digest.update(data)
                    size += len(data)
            os.replace(tmp_path, path)
            _share_with_storage_group(path, bits=_SHARED_FILE_GROUP_BITS)
            return {"key": key, "size_bytes": size, "sha256": digest.hexdigest()}
        except Exception:
            with contextlib.suppress(Exception):
                os.close(tmp_fd)
            with contextlib.suppress(Exception):
                os.remove(tmp_path)
            with contextlib.suppress(Exception):
                os.remove(path)
            raise

    def get(self, key: str) -> bytes | None:
        import os

        path = self._path(key)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def stream(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        import os

        path = self._path(key)
        if not os.path.isfile(path):
            return
        with open(path, "rb") as f:
            yield from iter(lambda: f.read(max(1, int(chunk_size or 1))), b"")

    def stat(self, key: str) -> dict[str, Any] | None:
        import os

        path = self._path(key)
        if not os.path.isfile(path):
            return None
        return {"key": key, "size_bytes": os.path.getsize(path)}

    def delete(self, key: str) -> bool:
        import os

        path = self._path(key)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    def list_key_page(
        self,
        prefix: str,
        *,
        limit: int,
        continuation_token: str | None = None,
    ) -> tuple[list[str], str | None]:
        """Return one page; continuation tokens are process-local and ephemeral."""
        import os

        logical_prefix = str(prefix or "").strip().lstrip("/")
        page_limit = _bounded_page_limit(limit)
        token = str(continuation_token or "").strip()

        if token:
            now = time.monotonic()
            with self._list_cursor_lock:
                if self._closed:
                    raise RuntimeError(_MESSAGE_LOCAL_FILESYSTEM_STORAGE_ADAPTER_IS_CLOSED)
                self._expire_list_cursors_unlocked(now)
                cursor = self._list_cursors.get(token)
                if cursor is None:
                    raise RuntimeError("local filesystem continuation token is invalid or expired")
                if cursor.prefix != logical_prefix:
                    raise RuntimeError("local filesystem continuation token does not match prefix")
                cursor = self._list_cursors.pop(token)
        else:
            with self._list_cursor_lock:
                if self._closed:
                    raise RuntimeError(_MESSAGE_LOCAL_FILESYSTEM_STORAGE_ADAPTER_IS_CLOSED)
                self._expire_list_cursors_unlocked(time.monotonic())
            prefix_path = self._path(logical_prefix)
            if logical_prefix.endswith("/") and not os.path.isdir(prefix_path):
                return [], None
            scan_root = prefix_path if os.path.isdir(prefix_path) else os.path.dirname(prefix_path)
            if not os.path.isdir(scan_root):
                return [], None
            cursor = _LocalFilesystemListCursor(
                prefix=logical_prefix,
                storage_root=self._path(""),
                frames=[_LocalFilesystemScanFrame(iterator=os.scandir(scan_root))],
                buffered_key=None,
                expires_at=0.0,
            )

        keys: list[str] = []
        if cursor.buffered_key is not None:
            keys.append(cursor.buffered_key)
            cursor.buffered_key = None
        try:
            while len(keys) < page_limit:
                keys.append(self._next_list_cursor_key(cursor))
            cursor.buffered_key = self._next_list_cursor_key(cursor)
        except StopIteration:
            self._close_list_cursor(cursor)
            return keys, None
        except Exception:
            self._close_list_cursor(cursor)
            raise
        return keys, self._store_list_cursor(cursor)

    def list_key_page_after(
        self,
        prefix: str,
        *,
        limit: int,
        start_after: str | None = None,
    ) -> tuple[list[str], str | None]:
        """Return a durable lexical page without retaining process-local cursors."""
        import os

        logical_prefix = str(prefix or "").strip().lstrip("/")
        boundary = str(start_after or "").strip().lstrip("/")
        page_limit = _bounded_page_limit(limit)
        shard_parts = logical_prefix.removeprefix(REPLAY_CLEANUP_COMPLETION_PREFIX).split("/")
        is_cleanup_shard = (
            logical_prefix.startswith(REPLAY_CLEANUP_COMPLETION_PREFIX)
            and logical_prefix.endswith("/")
            and len(shard_parts) == 2
            and len(shard_parts[0]) == 2
            and all(char in "0123456789abcdef" for char in shard_parts[0])
        )
        if not is_cleanup_shard:
            raise RuntimeError("restart-stable local pagination requires a cleanup journal shard")
        after_suffix = (
            boundary.removeprefix(logical_prefix) if boundary.startswith(logical_prefix) else ""
        )
        after_parts = after_suffix.split("/") if after_suffix else []
        after_digits = after_parts[:-1] if after_parts[-1:] == ["journal.json"] else []
        if len(after_digits) != 62 or any(
            len(part) != 1 or part not in "0123456789abcdef" for part in after_digits
        ):
            after_digits = []

        root = self._path(logical_prefix)

        def _walk(path: str, digits: list[str], *, tight: bool) -> Iterator[str]:
            depth = len(digits)
            if depth == 62:
                marker_path = os.path.join(path, "journal.json")
                if os.path.isfile(marker_path) and not tight:
                    yield f"{logical_prefix}{'/'.join(digits)}/journal.json"
                return
            minimum = after_digits[depth] if tight and after_digits else "0"
            for digit in "0123456789abcdef":
                if digit < minimum:
                    continue
                child = os.path.join(path, digit)
                if not os.path.isdir(child):
                    continue
                yield from _walk(
                    child,
                    [*digits, digit],
                    tight=bool(tight and after_digits and digit == minimum),
                )

        page_with_sentinel: list[str] = []
        if os.path.isdir(root):
            for key in _walk(root, [], tight=bool(after_digits)):
                page_with_sentinel.append(key)
                if len(page_with_sentinel) > page_limit:
                    break
        has_more = len(page_with_sentinel) > page_limit
        page = page_with_sentinel[:page_limit]
        return page, page[-1] if has_more else None

    def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str:
        safe_method = str(method or "GET").strip().upper()
        ttl = max(60, min(int(ttl_seconds or 600), 3600))
        return f"{Path(self._path(key)).as_uri()}?method={safe_method}&ttl={ttl}"


def create_storage_adapter(config: dict[str, Any] | None = None) -> ObjectStorageAdapter:
    """Factory: build the right storage adapter from configuration.

    Config keys:
        backend: "s3" | "local_fs" | "memory" (default: "memory")
        root_dir: local filesystem root (for local_fs)
        endpoint_url, region, bucket, access_key, secret_key,
        key_prefix, use_ssl, addressing_style, presign_ttl_seconds: S3 config
    """
    cfg = dict(config or {})
    backend = str(cfg.get("backend") or "memory").strip().lower()
    if backend == "s3":
        return S3ObjectStorageAdapter(
            endpoint_url=str(cfg.get("endpoint_url") or "").strip(),
            public_endpoint_url=str(cfg.get("public_endpoint_url") or "").strip(),
            region=str(cfg.get("region") or "us-east-1").strip(),
            bucket=str(cfg.get("bucket") or "platform-artifacts").strip(),
            access_key=str(cfg.get("access_key") or "").strip(),
            secret_key=str(cfg.get("secret_key") or "").strip(),
            key_prefix=str(cfg.get("key_prefix") or "").strip(),
            use_ssl=bool(cfg.get("use_ssl", True)),
            addressing_style=str(cfg.get("addressing_style") or "auto").strip(),
            presign_ttl_seconds=int(cfg.get("presign_ttl_seconds") or 600),
        )
    if backend == "local_fs":
        root_dir = str(cfg.get("root_dir") or "/data/artifacts").strip()
        return LocalFilesystemStorageAdapter(
            root_dir=root_dir,
            key_prefix=str(cfg.get("key_prefix") or "").strip(),
        )
    if backend == "memory":
        return InMemoryObjectStorage(key_prefix=str(cfg.get("key_prefix") or "").strip())
    raise ValueError("Unsupported artifact storage backend; expected memory, local_fs, or s3")
