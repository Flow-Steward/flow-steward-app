"""Object storage adapters used by the public extension SDK."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from .artifact_values import OCTET_STREAM_VALUE, _as_positive_int, _safe_artifact_filename

_MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER = "max_size_bytes must be a positive integer"


_MESSAGE_OBJECT_STREAM_EXCEEDS_MAX_SIZE_BYTES = "object stream exceeds max_size_bytes"


class ObjectStorageAdapter(Protocol):
    def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, Any]: ...

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

    def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str: ...


class InMemoryObjectStorage:
    def __init__(self, *, key_prefix: str = "") -> None:
        self._objects: dict[str, dict[str, Any]] = {}
        self._key_prefix = str(key_prefix or "").strip().strip("/")
        self.key_prefix = f"{self._key_prefix}/" if self._key_prefix else ""

    def _full_key(self, key: str) -> str:
        safe = str(key or "").strip().lstrip("/")
        return f"{self._key_prefix}/{safe}" if self._key_prefix else safe

    def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, Any]:
        payload = bytes(body)
        checksum = hashlib.sha256(payload).hexdigest()
        self._objects[self._full_key(key)] = {
            "body": payload,
            "content_type": str(content_type or OCTET_STREAM_VALUE).strip(),
            "size_bytes": len(payload),
            "sha256": checksum,
        }
        return {"key": key, "size_bytes": len(payload), "sha256": checksum}

    def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str,
        max_size_bytes: int,
    ) -> dict[str, Any]:
        max_size = _as_positive_int(max_size_bytes)
        if max_size is None:
            raise ValueError(_MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER)
        payload = bytearray()
        digest = hashlib.sha256()
        size = 0
        try:
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
            self._objects[self._full_key(key)] = {
                "body": body,
                "content_type": str(content_type or OCTET_STREAM_VALUE).strip(),
                "size_bytes": size,
                "sha256": checksum,
            }
            return {"key": key, "size_bytes": size, "sha256": checksum}
        except Exception:
            self.delete(key)
            raise

    def get(self, key: str) -> bytes | None:
        row = self._objects.get(self._full_key(key))
        return bytes(row["body"]) if row else None

    def stream(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        body = self.get(key)
        if body is None:
            return
        size = max(1, int(chunk_size or 1))
        for offset in range(0, len(body), size):
            yield body[offset : offset + size]

    def stat(self, key: str) -> dict[str, Any] | None:
        row = self._objects.get(self._full_key(key))
        if not row:
            return None
        return {
            "key": key,
            "size_bytes": int(row.get("size_bytes") or 0),
            "sha256": str(row.get("sha256") or "").strip(),
        }

    def delete(self, key: str) -> bool:
        return self._objects.pop(self._full_key(key), None) is not None

    def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str:
        safe_method = str(method or "GET").strip().upper()
        ttl = max(60, min(int(ttl_seconds or 600), 3600))
        return f"inmemory://{key}?method={safe_method}&ttl={ttl}"


class LocalFilesystemStorageAdapter:
    def __init__(self, *, root_dir: str, key_prefix: str = "") -> None:
        self._root = os.path.abspath(str(root_dir or "/data/artifacts"))
        self._key_prefix = str(key_prefix or "").strip().strip("/")
        self.key_prefix = f"{self._key_prefix}/" if self._key_prefix else ""
        os.makedirs(self._root, exist_ok=True)

    def _full_key(self, key: str) -> str:
        safe = str(key or "").strip().lstrip("/")
        return f"{self._key_prefix}/{safe}" if self._key_prefix else safe

    def _path(self, key: str) -> str:
        full = os.path.abspath(os.path.join(self._root, self._full_key(key)))
        if os.path.commonpath([self._root, full]) != self._root:
            raise ValueError(f"Storage key escapes root: {key}")
        return full

    def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, Any]:
        _ = content_type
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = bytes(body)
        checksum = hashlib.sha256(payload).hexdigest()
        with open(path, "wb") as handle:
            handle.write(payload)
        return {"key": key, "size_bytes": len(payload), "sha256": checksum}

    def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str,
        max_size_bytes: int,
    ) -> dict[str, Any]:
        _ = content_type
        max_size = _as_positive_int(max_size_bytes)
        if max_size is None:
            raise ValueError(_MESSAGE_MAX_SIZE_BYTES_MUST_BE_A_POSITIVE_INTEGER)
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path) or 'object'}.",
            suffix=".tmp",
            dir=os.path.dirname(path),
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in chunks:
                    data = bytes(chunk or b"")
                    if not data:
                        continue
                    if size + len(data) > max_size:
                        raise ValueError(_MESSAGE_OBJECT_STREAM_EXCEEDS_MAX_SIZE_BYTES)
                    handle.write(data)
                    digest.update(data)
                    size += len(data)
            os.replace(tmp_path, path)
            return {"key": key, "size_bytes": size, "sha256": digest.hexdigest()}
        except Exception:
            with contextlib.suppress(Exception):
                os.close(fd)
            with contextlib.suppress(Exception):
                os.remove(tmp_path)
            with contextlib.suppress(Exception):
                os.remove(path)
            raise

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    def stream(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        path = self._path(key)
        if not os.path.isfile(path):
            return
        with open(path, "rb") as handle:
            yield from iter(lambda: handle.read(max(1, int(chunk_size or 1))), b"")

    def stat(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        return {"key": key, "size_bytes": os.path.getsize(path)}

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not os.path.isfile(path):
            return False
        os.remove(path)
        return True

    def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str:
        safe_method = str(method or "GET").strip().upper()
        ttl = max(60, min(int(ttl_seconds or 600), 3600))
        return f"{Path(self._path(key)).as_uri()}?method={safe_method}&ttl={ttl}"


class S3ObjectStorageAdapter:
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
    ) -> None:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config as BotoConfig

        session_kwargs: dict[str, Any] = {}
        if access_key and secret_key:
            session_kwargs["aws_access_key_id"] = access_key
            session_kwargs["aws_secret_access_key"] = secret_key
        config_kwargs: dict[str, Any] = {
            "s3": {"addressing_style": addressing_style or "auto"},
            "signature_version": "s3v4",
        }
        if endpoint_url and not access_key and not secret_key:
            config_kwargs["signature_version"] = UNSIGNED
        client_kwargs: dict[str, Any] = {"config": BotoConfig(**config_kwargs)}
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
        self._bucket = str(bucket or "platform-artifacts").strip()
        self._prefix = str(key_prefix or "").strip().rstrip("/")
        self.key_prefix = f"{self._prefix}/" if self._prefix else ""
        self._presign_ttl = max(60, min(int(presign_ttl_seconds or 600), 7 * 24 * 3600))

    def _full_key(self, key: str) -> str:
        safe = str(key or "").strip().lstrip("/")
        return f"{self._prefix}/{safe}" if self._prefix else safe

    def put(self, key: str, body: bytes, *, content_type: str) -> dict[str, Any]:
        payload = bytes(body)
        checksum = hashlib.sha256(payload).hexdigest()
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._full_key(key),
            Body=payload,
            ContentType=str(content_type or OCTET_STREAM_VALUE).strip(),
        )
        return {"key": key, "size_bytes": len(payload), "sha256": checksum}

    def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        *,
        content_type: str,
        max_size_bytes: int,
    ) -> dict[str, Any]:
        max_size = _as_positive_int(max_size_bytes)
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
            response = self._client.upload_part(
                Bucket=self._bucket,
                Key=full_key,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=payload,
            )
            parts.append({"ETag": response["ETag"], "PartNumber": part_number})
            part_number += 1

        try:
            for chunk in chunks:
                data = bytes(chunk or b"")
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
            response = self._client.get_object(Bucket=self._bucket, Key=self._full_key(key))
            return response["Body"].read()
        except self._client.exceptions.NoSuchKey:
            return None

    def stream(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._full_key(key))
        except self._client.exceptions.NoSuchKey:
            return
        body = response["Body"]
        if hasattr(body, "iter_chunks"):
            yield from body.iter_chunks(chunk_size=max(1, int(chunk_size or 1)))
            return
        yield from iter(lambda: body.read(max(1, int(chunk_size or 1))), b"")

    def stat(self, key: str) -> dict[str, Any] | None:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=self._full_key(key))
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            error = getattr(exc, "response", {}).get("Error", {})
            if str(error.get("Code") or "").strip() in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return {"key": key, "size_bytes": int(response.get("ContentLength") or 0)}

    def delete(self, key: str) -> bool:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._full_key(key))
            return True
        except Exception:
            return False

    def presign(self, key: str, *, method: str, ttl_seconds: int = 600) -> str:
        safe_method = str(method or "GET").strip().upper()
        if safe_method not in {"GET", "PUT"}:
            raise ValueError(f"presign() only supports GET/PUT; got {safe_method!r}")
        ttl = max(60, min(int(ttl_seconds or 600), self._presign_ttl))
        client_method = "get_object" if safe_method == "GET" else "put_object"
        return self._client.generate_presigned_url(
            ClientMethod=client_method,
            Params={"Bucket": self._bucket, "Key": self._full_key(key)},
            ExpiresIn=ttl,
        )

    def presign_public_download(
        self,
        key: str,
        *,
        filename: str,
        ttl_seconds: int = 600,
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
            },
            ExpiresIn=ttl,
        )


def create_storage_adapter(config: dict[str, Any] | None = None) -> ObjectStorageAdapter:
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
        return LocalFilesystemStorageAdapter(
            root_dir=str(cfg.get("root_dir") or "/data/artifacts").strip(),
            key_prefix=str(cfg.get("key_prefix") or "").strip(),
        )
    if backend == "memory":
        return InMemoryObjectStorage(key_prefix=str(cfg.get("key_prefix") or "").strip())
    raise ValueError("Unsupported artifact storage backend; expected memory, local_fs, or s3")
