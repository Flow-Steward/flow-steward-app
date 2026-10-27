"""Dependencies owned by public API route adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

_TOKEN_ARTIFACT_BIN = "artifact.bin"
_MESSAGE_PUBLIC_API_DEPENDENCIES_ARE_NOT_INITIALIZED = "Public API dependencies are not initialized"

_get_logger_only: Callable[[], Any] | None = None
_get_thread_state_snapshot: Callable[[str], Any] | None = None
_get_committed_checkpoint_values: Callable[[str], Mapping[str, Any]] | None = None
_to_json_obj: Callable[[Any], Any] | None = None
_resolve_artifact: Callable[[str], dict[str, Any] | None] | None = None
_artifact_presigned_url: Callable[..., str] | None = None
_artifact_local_stream: Callable[..., tuple[Iterator[bytes], int] | None] | None = None
_artifact_read_bytes: Callable[..., bytes | None] | None = None


def init(
    *,
    get_logger_only: Callable[[], Any],
    get_thread_state_snapshot: Callable[[str], Any],
    to_json_obj: Callable[[Any], Any],
    resolve_artifact: Callable[[str], dict[str, Any] | None],
    artifact_presigned_url: Callable[..., str] | None = None,
    artifact_local_stream: Callable[..., tuple[Iterator[bytes], int] | None] | None = None,
    artifact_read_bytes: Callable[..., bytes | None] | None = None,
    get_committed_checkpoint_values: Callable[[str], Mapping[str, Any]] | None = None,
) -> None:
    global _get_committed_checkpoint_values
    global _artifact_presigned_url, _artifact_local_stream, _artifact_read_bytes
    global _get_logger_only, _get_thread_state_snapshot, _to_json_obj, _resolve_artifact
    _get_committed_checkpoint_values = get_committed_checkpoint_values
    _get_logger_only = get_logger_only
    _get_thread_state_snapshot = get_thread_state_snapshot
    _to_json_obj = to_json_obj
    _resolve_artifact = resolve_artifact
    _artifact_presigned_url = artifact_presigned_url
    _artifact_local_stream = artifact_local_stream
    _artifact_read_bytes = artifact_read_bytes


def get_logger_only() -> Any:
    if _get_logger_only is None:
        raise RuntimeError(_MESSAGE_PUBLIC_API_DEPENDENCIES_ARE_NOT_INITIALIZED)
    return _get_logger_only()


def get_thread_state_snapshot(thread_id: str) -> Any:
    normalized = str(thread_id or "")
    if _get_thread_state_snapshot is None:
        raise RuntimeError(_MESSAGE_PUBLIC_API_DEPENDENCIES_ARE_NOT_INITIALIZED)
    return _get_thread_state_snapshot(normalized)


def get_committed_checkpoint_values(thread_id: str) -> Mapping[str, Any]:
    """Return committed evidence only when the runtime supplied a reader."""
    if _get_committed_checkpoint_values is None:
        return {}
    return _get_committed_checkpoint_values(str(thread_id or ""))


def to_json_obj(value: Any) -> Any:
    if _to_json_obj is None:
        raise RuntimeError(_MESSAGE_PUBLIC_API_DEPENDENCIES_ARE_NOT_INITIALIZED)
    return _to_json_obj(value)


def resolve_artifact(artifact_id: str) -> dict[str, Any] | None:
    normalized = str(artifact_id or "").strip()
    if _resolve_artifact is None:
        raise RuntimeError(_MESSAGE_PUBLIC_API_DEPENDENCIES_ARE_NOT_INITIALIZED)
    return _resolve_artifact(normalized)


def artifact_presigned_url(
    artifact_id: str,
    *,
    method: str = "GET",
    ttl_seconds: int = 600,
    filename: str = _TOKEN_ARTIFACT_BIN,
) -> str:
    normalized = str(artifact_id or "").strip()
    if _artifact_presigned_url is None:
        return ""
    return str(
        _artifact_presigned_url(
            normalized,
            method=str(method or "GET").strip().upper(),
            ttl_seconds=max(60, min(int(ttl_seconds or 600), 3600)),
            filename=str(filename or _TOKEN_ARTIFACT_BIN).strip() or _TOKEN_ARTIFACT_BIN,
        )
        or ""
    ).strip()


def artifact_local_stream(artifact_id: str) -> tuple[Iterator[bytes], int] | None:
    """Chunks and their byte count, for an authorized artifact with no public URL.

    ``None`` means this deployment cannot serve the artifact this way — a
    different storage backend, an incomplete row, or a missing object — and the
    caller must not treat that as an empty download. The size belongs to the
    chunks being returned, so it is the one safe to declare to a client.
    """
    normalized = str(artifact_id or "").strip()
    if _artifact_local_stream is None or not normalized:
        return None
    return _artifact_local_stream(normalized)


def artifact_read_bytes(artifact_id: str, *, max_size_bytes: int) -> bytes | None:
    """The body of an authorized artifact, from any storage backend, within a bound.

    ``None`` means the body is not available here. A body over the bound raises
    ``ValueError`` rather than being read.
    """
    normalized = str(artifact_id or "").strip()
    if _artifact_read_bytes is None or not normalized:
        return None
    return _artifact_read_bytes(normalized, max_size_bytes=max_size_bytes)


__all__ = [
    "artifact_local_stream",
    "artifact_presigned_url",
    "artifact_read_bytes",
    "get_committed_checkpoint_values",
    "get_logger_only",
    "get_thread_state_snapshot",
    "init",
    "resolve_artifact",
    "to_json_obj",
]
