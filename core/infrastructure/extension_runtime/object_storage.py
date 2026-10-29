"""Object storage contracts, bounded traversal and memory/S3 adapters."""

from __future__ import annotations

import contextlib
import hashlib
import threading
from bisect import bisect_left, bisect_right, insort
from collections.abc import Iterable, Iterator
from typing import Any, Protocol
from urllib.parse import quote

_MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER = "max_size_bytes must be a positive integer"


_MESSAGE_OBJECT_STREAM_EXCEEDS_MAX_SIZE_BYTES = "object stream exceeds max_size_bytes"


APPLICATION_JSON_VALUE = "application/json"


APPLICATION_NDJSON_VALUE = "application/x-ndjson"


OCTET_STREAM_VALUE = "application/octet-stream"


ARTIFACT_INGRESS_GRANT_MAX_TTL_SECONDS = 7 * 24 * 60 * 60


STORAGE_KEY_PAGE_MAX = 1000


STORAGE_CLEANUP_PAGE_SIZE = 128


STORAGE_ITERATION_MAX_PAGES = 131_072


def _bounded_page_limit(value: int) -> int:
    return max(1, min(int(value or 1), STORAGE_KEY_PAGE_MAX))


def read_storage_key_page(
    storage: Any,
    prefix: str,
    *,
    limit: int = STORAGE_CLEANUP_PAGE_SIZE,
    continuation_token: str | None = None,
) -> tuple[list[str], str | None]:
    page_limit = _bounded_page_limit(limit)
    list_page = getattr(storage, "list_key_page", None)
    if not callable(list_page):
        raise RuntimeError("artifact storage must support bounded key pagination")
    page = list_page(
        prefix,
        limit=page_limit,
        continuation_token=continuation_token,
    )
    if not isinstance(page, tuple) or len(page) != 2:
        raise RuntimeError("artifact storage returned an invalid key page")
    raw_keys, raw_next = page
    if not isinstance(raw_keys, list) or len(raw_keys) > page_limit:
        raise RuntimeError("artifact storage exceeded the requested key page bound")
    keys = [str(key or "").strip() for key in raw_keys]
    if any(not key or not key.startswith(str(prefix or "")) for key in keys):
        raise RuntimeError("artifact storage returned a key outside the requested prefix")
    next_token = str(raw_next or "").strip() or None
    if next_token is not None and next_token == str(continuation_token or "").strip():
        raise RuntimeError("artifact storage continuation token did not advance")
    return keys, next_token


def read_storage_key_page_after(
    storage: Any,
    prefix: str,
    *,
    limit: int = STORAGE_CLEANUP_PAGE_SIZE,
    start_after: str | None = None,
) -> tuple[list[str], str | None]:
    """Read a restart-stable bounded page ordered after a logical object key."""
    page_limit = _bounded_page_limit(limit)
    list_page_after = getattr(storage, "list_key_page_after", None)
    if not callable(list_page_after):
        raise RuntimeError("artifact storage must support restart-stable key pagination")
    page = list_page_after(prefix, limit=page_limit, start_after=start_after)
    if not isinstance(page, tuple) or len(page) != 2:
        raise RuntimeError("artifact storage returned an invalid restart-stable key page")
    raw_keys, raw_next = page
    if not isinstance(raw_keys, list) or len(raw_keys) > page_limit:
        raise RuntimeError("artifact storage exceeded the requested key page bound")
    keys = [str(key or "").strip() for key in raw_keys]
    logical_prefix = str(prefix or "")
    boundary = str(start_after or "").strip()
    if any(
        not key or not key.startswith(logical_prefix) or (boundary and key <= boundary)
        for key in keys
    ):
        raise RuntimeError("artifact storage returned an invalid restart-stable key page")
    next_key = str(raw_next or "").strip() or None
    if next_key is not None and (not keys or next_key != keys[-1]):
        raise RuntimeError("artifact storage returned an invalid restart-stable cursor")
    return keys, next_key


def iter_storage_keys(
    storage: Any,
    prefix: str,
    *,
    page_size: int = STORAGE_CLEANUP_PAGE_SIZE,
) -> Iterator[str]:
    continuation: str | None = None
    page_count = 0
    while True:
        page_count += 1
        if page_count > STORAGE_ITERATION_MAX_PAGES:
            raise RuntimeError("artifact storage listing exceeded the hard page limit")
        keys, next_token = read_storage_key_page(
            storage,
            prefix,
            limit=page_size,
            continuation_token=continuation,
        )
        yield from keys
        if next_token is None:
            return
        continuation = next_token


def _safe_artifact_filename(filename: str | None) -> str:
    value = str(filename or "").strip() or "blob"
    if "\x00" in value:
        raise ValueError("Artifact filename contains invalid characters")
    if "/" in value or "\\" in value:
        raise ValueError("Artifact filename must not contain path separators")
    if value in {".", ".."}:
        raise ValueError("Artifact filename must not be a path traversal segment")
    return value


def _opened_object_stream(
    chunks: Iterator[bytes],
    *,
    expected_bytes: int,
) -> Iterator[bytes] | None:
    """Pull the first chunk so the object is open before the caller responds.

    ``stream()`` is a generator: nothing is opened until it is first advanced,
    which for an HTTP caller is after the ``200`` and its ``Content-Length``
    have already gone out. Draining one chunk here moves the ``open()`` inside
    this call, so an object that disappeared after ``stat()`` is reported as an
    error instead of a truncated body, and one deleted after this point is
    still served in full from the handle we are holding.

    Returns ``None`` when the object turned out to be gone. An object that is
    genuinely empty yields nothing and is not an error.
    """

    first_chunk = next(chunks, b"")
    if not first_chunk:
        if expected_bytes > 0:
            _close_iterator(chunks)
            return None
        return iter(())

    def opened() -> Iterator[bytes]:
        yield first_chunk
        yield from chunks

    return opened()


def _close_iterator(chunks: Iterator[bytes]) -> None:
    close = getattr(chunks, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


class ObjectStorageAdapter(Protocol):
    def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, Any]: ...

    def put_if_absent(self, key: str, body: bytes, *, content_type: str) -> bool: ...

    def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str,
        max_size_bytes: int,
    ) -> dict[str, Any]: ...

    def get(self, key: str) -> bytes | None: ...

    def stream(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]: ...

    def stat(self, key: str) -> dict[str, Any] | None: ...

    def delete(self, key: str) -> bool: ...

    def list_key_page(
        self,
        prefix: str,
        *,
        limit: int,
        continuation_token: str | None = None,
    ) -> tuple[list[str], str | None]: ...

    def list_key_page_after(
        self,
        prefix: str,
        *,
        limit: int,
        start_after: str | None = None,
    ) -> tuple[list[str], str | None]: ...

    def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str: ...


class InMemoryObjectStorage:
    """S3-compatible shape for local tests/dev."""

    def __init__(self, *, key_prefix: str = "") -> None:
        self._objects: dict[str, dict[str, Any]] = {}
        self._sorted_keys: list[str] = []
        self._lock = threading.RLock()
        self._key_prefix = str(key_prefix or "").strip().strip("/")
        self.key_prefix = f"{self._key_prefix}/" if self._key_prefix else ""

    def _full_key(self, key: str) -> str:
        safe = str(key or "").strip().lstrip("/")
        return f"{self._key_prefix}/{safe}" if self._key_prefix else safe

    def _put_unlocked(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str,
        checksum: str,
    ) -> dict[str, Any]:
        full_key = self._full_key(key)
        if full_key not in self._objects:
            insort(self._sorted_keys, full_key)
        self._objects[full_key] = {
            "body": payload,
            "content_type": str(content_type or OCTET_STREAM_VALUE).strip(),
            "size_bytes": len(payload),
            "sha256": checksum,
        }
        return {"key": key, "size_bytes": len(payload), "sha256": checksum}

    def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, Any]:
        payload = bytes(body)
        checksum = hashlib.sha256(payload).hexdigest()
        with self._lock:
            return self._put_unlocked(
                key,
                payload,
                content_type=content_type,
                checksum=checksum,
            )

    def put_if_absent(self, key: str, body: bytes, *, content_type: str) -> bool:
        full_key = self._full_key(key)
        with self._lock:
            if full_key in self._objects:
                return False
            payload = bytes(body)
            self._put_unlocked(
                key,
                payload,
                content_type=content_type,
                checksum=hashlib.sha256(payload).hexdigest(),
            )
            return True

    def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str,
        max_size_bytes: int,
    ) -> dict[str, Any]:
        max_size = _positive_int(max_size_bytes)
        if max_size is None:
            raise ValueError(_MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER)
        payload = bytearray()
        digest = hashlib.sha256()
        size = 0
        for chunk in chunks:
            data = bytes(chunk or b"")
            if not data:
                continue
            if size + len(data) > max_size:
                raise ValueError(_MESSAGE_OBJECT_STREAM_EXCEEDS_MAX_SIZE_BYTES)
            payload.extend(data)
            digest.update(data)
            size += len(data)
        body = bytes(payload)
        checksum = digest.hexdigest()
        with self._lock:
            return self._put_unlocked(
                key,
                body,
                content_type=content_type,
                checksum=checksum,
            )

    def get(self, key: str) -> bytes | None:
        with self._lock:
            row = self._objects.get(self._full_key(key))
            if not row:
                return None
            return bytes(row["body"])

    def stream(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        body = self.get(key)
        if body is None:
            return
        size = max(1, int(chunk_size or 1))
        for offset in range(0, len(body), size):
            yield body[offset : offset + size]

    def stat(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._objects.get(self._full_key(key))
            if not row:
                return None
            return {
                "key": key,
                "size_bytes": int(row.get("size_bytes") or 0),
                "sha256": str(row.get("sha256") or "").strip(),
            }

    def delete(self, key: str) -> bool:
        full_key = self._full_key(key)
        with self._lock:
            deleted = self._objects.pop(full_key, None) is not None
            if deleted:
                index = bisect_left(self._sorted_keys, full_key)
                if index < len(self._sorted_keys) and self._sorted_keys[index] == full_key:
                    self._sorted_keys.pop(index)
            return deleted

    def list_key_page(
        self,
        prefix: str,
        *,
        limit: int,
        continuation_token: str | None = None,
    ) -> tuple[list[str], str | None]:
        logical_prefix = str(prefix or "").strip().lstrip("/")
        full_prefix = self._full_key(logical_prefix)
        strip_prefix = f"{self._key_prefix}/" if self._key_prefix else ""
        cursor = str(continuation_token or "")
        page_limit = _bounded_page_limit(limit)
        full_cursor = self._full_key(cursor) if cursor else ""
        with self._lock:
            start = bisect_left(self._sorted_keys, full_prefix)
            if full_cursor:
                start = max(start, bisect_right(self._sorted_keys, full_cursor))
            stop = bisect_left(self._sorted_keys, f"{full_prefix}\U0010ffff")
            full_page = self._sorted_keys[start : min(stop, start + page_limit + 1)]
        page = [
            key[len(strip_prefix) :] if strip_prefix and key.startswith(strip_prefix) else key
            for key in full_page
        ]
        has_more = len(page) > page_limit
        keys = page[:page_limit]
        return keys, keys[-1] if has_more else None

    def list_key_page_after(
        self,
        prefix: str,
        *,
        limit: int,
        start_after: str | None = None,
    ) -> tuple[list[str], str | None]:
        return self.list_key_page(prefix, limit=limit, continuation_token=start_after)

    def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str:
        safe_method = str(method or "GET").strip().upper()
        ttl = max(60, min(int(ttl_seconds or 600), 3600))
        return f"inmemory://{key}?method={safe_method}&ttl={ttl}"


class S3ObjectStorageAdapter:
    """S3-compatible object storage via boto3.

    Works with AWS S3, SeaweedFS, MinIO, or any S3-compatible endpoint.
    """

    def __init__(
        self,
        *,
        endpoint_url: str = "",
        public_endpoint_url: str = "",
        region: str = "us-east-1",
        bucket: str,
        access_key: str = "",
        secret_key: str = "",
        key_prefix: str = "",
        use_ssl: bool = True,
        addressing_style: str = "auto",
        presign_ttl_seconds: int = 600,
    ):
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config as BotoConfig

        session_kwargs: dict[str, Any] = {}
        if access_key and secret_key:
            session_kwargs["aws_access_key_id"] = access_key
            session_kwargs["aws_secret_access_key"] = secret_key
        s3_config = {"addressing_style": addressing_style or "auto"}
        config_kwargs: dict[str, Any] = {
            "s3": s3_config,
            "signature_version": "s3v4",
        }
        if endpoint_url and not access_key and not secret_key:
            config_kwargs["signature_version"] = UNSIGNED
        client_kwargs: dict[str, Any] = {
            "config": BotoConfig(**config_kwargs),
        }
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if region:
            client_kwargs["region_name"] = region
        client_kwargs["use_ssl"] = use_ssl

        self._client = boto3.client("s3", **session_kwargs, **client_kwargs)
        self._public_presign_client = self._client
        normalized_public_endpoint = str(public_endpoint_url or "").strip()
        # AC17-R130: whether anyone promised this storage is reachable from a browser.
        # Without a public endpoint the client above is the internal one, and signing
        # with it produces a URL to a name that only exists inside the deployment
        # network -- which the browser is then redirected to, and cannot follow.
        self._public_endpoint_configured = bool(normalized_public_endpoint)
        if normalized_public_endpoint:
            public_client_kwargs = dict(client_kwargs)
            public_client_kwargs["endpoint_url"] = normalized_public_endpoint
            self._public_presign_client = boto3.client(
                "s3",
                **session_kwargs,
                **public_client_kwargs,
            )
        self._bucket = str(bucket).strip()
        self._prefix = str(key_prefix or "").strip().rstrip("/")
        self._key_prefix = self._prefix
        self.key_prefix = f"{self._prefix}/" if self._prefix else ""
        self._presign_ttl = max(
            60,
            min(int(presign_ttl_seconds or 600), ARTIFACT_INGRESS_GRANT_MAX_TTL_SECONDS),
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """Create bucket if it does not exist (idempotent, for SeaweedFS/MinIO/S3 compat)."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            with contextlib.suppress(Exception):
                self._client.create_bucket(Bucket=self._bucket)

    def _full_key(self, key: str) -> str:
        safe = str(key or "").strip().lstrip("/")
        return f"{self._prefix}/{safe}" if self._prefix else safe

    def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, Any]:
        payload = bytes(body)
        checksum = hashlib.sha256(payload).hexdigest()
        full_key = self._full_key(key)
        self._client.put_object(
            Bucket=self._bucket,
            Key=full_key,
            Body=payload,
            ContentType=str(content_type or OCTET_STREAM_VALUE).strip(),
        )
        return {"key": key, "size_bytes": len(payload), "sha256": checksum}

    def put_if_absent(self, key: str, body: bytes, *, content_type: str) -> bool:
        for attempt in range(3):
            try:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=self._full_key(key),
                    Body=bytes(body),
                    ContentType=str(content_type or OCTET_STREAM_VALUE).strip(),
                    IfNoneMatch="*",
                )
                return True
            except Exception as exc:
                response = getattr(exc, "response", {})
                error = response.get("Error", {}) if isinstance(response, dict) else {}
                metadata = (
                    response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
                )
                code = str(error.get("Code") or "").strip()
                status = metadata.get("HTTPStatusCode")
                if code in {"PreconditionFailed", "412"} or status == 412:
                    return False
                if (code in {"ConditionalRequestConflict", "409"} or status == 409) and attempt < 2:
                    continue
                raise
        raise RuntimeError("conditional object write exhausted retries")

    def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str,
        max_size_bytes: int,
    ) -> dict[str, Any]:
        max_size = _positive_int(max_size_bytes)
        if max_size is None:
            raise ValueError(_MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER)
        full_key = self._full_key(key)
        resolved_content_type = (
            str(content_type or OCTET_STREAM_VALUE).strip() or OCTET_STREAM_VALUE
        )
        upload = self._client.create_multipart_upload(
            Bucket=self._bucket,
            Key=full_key,
            ContentType=resolved_content_type,
        )
        upload_id = str(upload.get("UploadId") or "").strip()
        parts: list[dict[str, Any]] = []
        part_number = 1
        buffer = bytearray()
        part_size = 5 * 1024 * 1024
        digest = hashlib.sha256()
        size = 0

        def _upload_part(payload: bytes) -> None:
            nonlocal part_number
            if not payload:
                return
            resp = self._client.upload_part(
                Bucket=self._bucket,
                Key=full_key,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=payload,
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
            part_number += 1

        try:
            for chunk in chunks:
                data = chunk if isinstance(chunk, bytes) else bytes(chunk or b"")
                if not data:
                    continue
                if size + len(data) > max_size:
                    raise ValueError(_MESSAGE_OBJECT_STREAM_EXCEEDS_MAX_SIZE_BYTES)
                digest.update(data)
                size += len(data)
                view = memoryview(data)
                offset = 0
                while offset < len(view):
                    capacity = part_size - len(buffer)
                    take = min(capacity, len(view) - offset)
                    buffer.extend(view[offset : offset + take])
                    offset += take
                    if len(buffer) == part_size:
                        _upload_part(bytes(buffer))
                        buffer.clear()
            if size == 0:
                with contextlib.suppress(Exception):
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket,
                        Key=full_key,
                        UploadId=upload_id,
                    )
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=full_key,
                    Body=b"",
                    ContentType=resolved_content_type,
                )
                return {"key": key, "size_bytes": 0, "sha256": digest.hexdigest()}
            _upload_part(bytes(buffer))
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=full_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            return {"key": key, "size_bytes": size, "sha256": digest.hexdigest()}
        except Exception:
            with contextlib.suppress(Exception):
                self._client.abort_multipart_upload(
                    Bucket=self._bucket,
                    Key=full_key,
                    UploadId=upload_id,
                )
            raise

    def get(self, key: str) -> bytes | None:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._full_key(key))
            return resp["Body"].read()
        except self._client.exceptions.NoSuchKey:
            return None

    def stream(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._full_key(key))
        except self._client.exceptions.NoSuchKey:
            return
        body = resp["Body"]
        size = max(1, int(chunk_size or 1))
        if hasattr(body, "iter_chunks"):
            yield from body.iter_chunks(chunk_size=size)
            return
        yield from iter(lambda: body.read(size), b"")

    def stat(self, key: str) -> dict[str, Any] | None:
        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=self._full_key(key))
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            error = getattr(exc, "response", {}).get("Error", {})
            if str(error.get("Code") or "").strip() in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return {"key": key, "size_bytes": int(resp.get("ContentLength") or 0)}

    def delete(self, key: str) -> bool:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._full_key(key))
            return True
        except Exception:
            return False

    def list_key_page(
        self,
        prefix: str,
        *,
        limit: int,
        continuation_token: str | None = None,
    ) -> tuple[list[str], str | None]:
        logical_prefix = str(prefix or "").strip().lstrip("/")
        full_prefix = self._full_key(logical_prefix)
        strip_prefix = f"{self._prefix}/" if self._prefix else ""
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": full_prefix,
            "MaxKeys": _bounded_page_limit(limit),
        }
        if continuation_token:
            request["ContinuationToken"] = str(continuation_token)
        page = self._client.list_objects_v2(**request)
        keys: list[str] = []
        for row in page.get("Contents") or []:
            key = str(row.get("Key") or "")
            if strip_prefix and key.startswith(strip_prefix):
                key = key[len(strip_prefix) :]
            if key.startswith(logical_prefix):
                keys.append(key)
        next_token = (
            str(page.get("NextContinuationToken") or "").strip() if page.get("IsTruncated") else ""
        )
        if page.get("IsTruncated") and not next_token:
            raise RuntimeError("truncated storage listing omitted continuation token")
        return keys, next_token or None

    def list_key_page_after(
        self,
        prefix: str,
        *,
        limit: int,
        start_after: str | None = None,
    ) -> tuple[list[str], str | None]:
        logical_prefix = str(prefix or "").strip().lstrip("/")
        strip_prefix = f"{self._prefix}/" if self._prefix else ""
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": self._full_key(logical_prefix),
            "MaxKeys": _bounded_page_limit(limit),
        }
        if start_after:
            request["StartAfter"] = self._full_key(str(start_after))
        response = self._client.list_objects_v2(**request)
        keys: list[str] = []
        for row in response.get("Contents") or []:
            key = str(row.get("Key") or "")
            if strip_prefix and key.startswith(strip_prefix):
                key = key[len(strip_prefix) :]
            if key.startswith(logical_prefix):
                keys.append(key)
        return keys, keys[-1] if response.get("IsTruncated") and keys else None

    def abort_incomplete_uploads(self, key: str) -> int:
        """Abort multipart uploads for one exact logical object key."""
        full_key = self._full_key(key)
        aborted = 0
        response = self._client.list_multipart_uploads(Bucket=self._bucket, Prefix=full_key)
        for upload in response.get("Uploads") or []:
            if str(upload.get("Key") or "") != full_key:
                continue
            upload_id = str(upload.get("UploadId") or "").strip()
            if not upload_id:
                continue
            self._client.abort_multipart_upload(
                Bucket=self._bucket,
                Key=full_key,
                UploadId=upload_id,
            )
            aborted += 1
        return aborted

    def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str:
        safe_method = str(method or "GET").strip().upper()
        _ALLOWED_METHODS = {"GET", "PUT"}
        if safe_method not in _ALLOWED_METHODS:
            raise ValueError(
                f"presign() only supports methods {_ALLOWED_METHODS}; got {safe_method!r}"
            )
        ttl = max(60, min(int(ttl_seconds or 600), self._presign_ttl))
        client_method = "get_object" if safe_method == "GET" else "put_object"
        params = {"Bucket": self._bucket, "Key": self._full_key(key)}
        if safe_method == "PUT":
            params["IfNoneMatch"] = "*"
        return self._client.generate_presigned_url(
            ClientMethod=client_method,
            Params=params,
            ExpiresIn=ttl,
        )

    def presign_public_download(
        self,
        key: str,
        *,
        filename: str,
        ttl_seconds: int = 600,
        content_type: str = "",
    ) -> str:
        """Create a browser-facing GET URL, or nothing when none can be promised.

        AC17-R130: an empty answer is not a failure, it is the honest one. The caller
        streams the authorized object instead, which is what it already does for a
        backend that cannot presign at all. Handing back a URL built on the internal
        endpoint looked like success and sent the browser to a host it cannot resolve:
        a 307 to nowhere, and a Download button that produced nothing.

        This inverts what an empty ARTIFACT_S3_PUBLIC_ENDPOINT_URL used to mean --
        "assume the endpoint is already browser reachable" -- and deliberately. A
        deployment whose S3 endpoint really is public restores the redirect by naming
        it, which the variable exists for; a deployment with bundled storage cannot
        name one, and its downloads now work rather than failing silently. The cost of
        being wrong is bytes through the application; the cost of the old default was
        a feature that did not work at all.
        """

        if not self._public_endpoint_configured:
            return ""
        ttl = max(60, min(int(ttl_seconds or 600), self._presign_ttl))
        safe_filename = _safe_artifact_filename(filename)
        disposition = f"attachment; filename*=UTF-8''{quote(safe_filename, safe='')}"
        return self._public_presign_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": self._bucket,
                "Key": self._full_key(key),
                "ResponseContentDisposition": disposition,
                **({"ResponseContentType": content_type} if content_type else {}),
            },
            ExpiresIn=ttl,
        )


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
