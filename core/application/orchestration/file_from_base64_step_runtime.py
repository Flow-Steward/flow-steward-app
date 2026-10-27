"""Save a base64 string from workflow state as a downloadable file.

Model and API responses often carry files as base64: an image generation
response's ``b64_json``, an ``inline_data`` part, or a ``data:image/png;base64,``
URL. This step decodes such a value, stores it as a job artifact and writes the
artifact handle (``artifact:<id>``) to ``result_target``. The base64 text itself
never travels further through state, and the file shows up under the step's
generated files.

Config (``override.file_from_base64``):

- ``source`` (required): state selector of the base64 value. A plain string, a
  data URL, or an object holding one (``b64_json``, ``data``, ``base64``,
  ``bytes_base64``, with an optional ``mime_type`` / ``mimeType``).
- ``result_target`` (required): state path that receives the artifact handle.
- ``filename``: template for the file name; an extension is added from the
  content type when it has none.
- ``content_type``: template; otherwise taken from the data URL or object, else
  recognised from the file's own bytes.
- ``max_bytes``: a lower limit on the decoded size for this step. The limit
  for every step is ``FS_BASE64_FILE_MAX_BYTES`` (default 50 MB); a step can
  only lower it.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from typing import Any

from core.application.orchestration.step_templating import _render_value, _resolve_path
from core.infrastructure.config.workflow_file_limits import base64_file_max_bytes

DEFAULT_FILENAME = "file"
OCTET_STREAM = "application/octet-stream"
STEP_RUN_FALLBACK = "file_from_base64"

_OBJECT_DATA_KEYS = ("b64_json", "data", "base64", "bytes_base64", "content")
_OBJECT_TYPE_KEYS = ("mime_type", "mimeType", "content_type", "contentType")
_DATA_URL = re.compile(r"^data:(?P<type>[^;,]*)(?P<params>(?:;[^;,]*)*),(?P<data>.*)$", re.S)
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_WHITESPACE = re.compile(r"\s+")

#: Content types recognised from the decoded bytes, and the extension each gets.
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/csv": ".csv",
}


class FileFromBase64Error(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _text(value: Any) -> str:
    return str(value or "").strip()


def sniff_content_type(body: bytes) -> str:
    """The content type of a file recognised from its first bytes, if any."""
    head = body[:64]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if head.startswith(b"ID3") or head[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "audio/mpeg"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    stripped = head.lstrip()
    if stripped.startswith((b"<svg", b"<?xml")) and b"<svg" in body[:1024]:
        return "image/svg+xml"
    return ""


def _payload_and_type(value: Any) -> tuple[str, str]:
    """The base64 text and any content type the value itself declares."""
    declared = ""
    if isinstance(value, Mapping):
        declared = next((_text(value.get(key)) for key in _OBJECT_TYPE_KEYS if value.get(key)), "")
        value = next((value.get(key) for key in _OBJECT_DATA_KEYS if value.get(key)), None)
    if not isinstance(value, str) or not value.strip():
        raise FileFromBase64Error("source_not_base64", "The source holds no base64 text")
    text = value.strip()
    match = _DATA_URL.match(text)
    if match:
        if ";base64" not in match.group("params").lower():
            raise FileFromBase64Error("source_not_base64", "The data URL is not base64-encoded")
        declared = declared or _text(match.group("type"))
        text = match.group("data")
    return _WHITESPACE.sub("", text), declared


def decode_base64(text: str, *, max_bytes: int) -> bytes:
    """Decode standard or URL-safe base64, padding optional, within ``max_bytes``."""
    if (len(text) * 3) // 4 > max_bytes + 3:
        raise FileFromBase64Error("file_too_large", f"The file exceeds {max_bytes} bytes")
    normalized = text.replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        body = base64.b64decode(normalized, validate=True)
    except ValueError as exc:  # binascii.Error is a ValueError
        raise FileFromBase64Error("invalid_base64", "The source is not valid base64") from exc
    if not body:
        raise FileFromBase64Error("source_not_base64", "The decoded file is empty")
    if len(body) > max_bytes:
        raise FileFromBase64Error("file_too_large", f"The file exceeds {max_bytes} bytes")
    return body


def _max_bytes(config: Mapping[str, Any]) -> int:
    """The step's own limit, never above the operator's ``FS_BASE64_FILE_MAX_BYTES``."""
    ceiling = base64_file_max_bytes()
    raw = config.get("max_bytes")
    if raw is None or raw == "":
        return ceiling
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return ceiling
    return max(1, min(value, ceiling))


def _media_type(value: str) -> str:
    media = value.split(";", 1)[0].strip().lower()
    return media if _MEDIA_TYPE.match(media) else ""


def _filename(config: Mapping[str, Any], state: dict[str, Any], content_type: str) -> str:
    rendered = _text(_render_value(config.get("filename"), state)) if config.get("filename") else ""
    name = rendered or DEFAULT_FILENAME
    if any(ord(ch) < 32 or ch == "\x7f" for ch in name) or "/" in name or "\\" in name:
        raise FileFromBase64Error("invalid_filename", "The file name must be a plain name")
    if name in {".", ".."}:
        raise FileFromBase64Error("invalid_filename", "The file name must be a plain name")
    if "." not in name.lstrip("."):
        name += _EXTENSIONS.get(content_type, "")
    return name[:200]


def validate_file_from_base64_config(config: Mapping[str, Any], *, path_prefix: str) -> list[dict]:
    """Authoring issues for one ``file_from_base64`` configuration."""
    issues: list[dict] = []

    def issue(field: str, code: str, message: str) -> None:
        issues.append(
            {
                "code": f"workflow.step_file_from_base64_{code}",
                "message": message,
                "path": f"{path_prefix}.{field}",
                "severity": "error",
            }
        )

    if not _text(config.get("source")):
        issue("source", "source_missing", "file_from_base64 requires the base64 source")
    if not _text(config.get("result_target")):
        issue("result_target", "result_target_missing", "file_from_base64 requires result_target")
    raw = config.get("max_bytes")
    if raw is not None and raw != "":
        try:
            valid = not isinstance(raw, bool) and int(raw) >= 1
        except (TypeError, ValueError):
            valid = False
        if not valid:
            issue("max_bytes", "max_bytes_invalid", "max_bytes must be a positive whole number")
    return issues


def _failure(reason: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "failure_reason": reason,
        "errors": [{"code": reason, "message": message}],
    }


def execute_file_from_base64_step(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Decode the configured base64 value and store it as a job artifact."""
    from core.application.orchestration.http_request_step_runtime import (
        _owner_scope_from_state,
        _shared_artifact_plane_from_state,
    )

    source = _text(config.get("source"))
    if not source:
        return _failure("source_missing", "file_from_base64 requires a source")
    try:
        text, declared = _payload_and_type(_resolve_path(state, source))
        body = decode_base64(text, max_bytes=_max_bytes(config))
        explicit = (
            _text(_render_value(config.get("content_type"), state))
            if config.get("content_type")
            else ""
        )
        content_type = (
            _media_type(explicit)
            or _media_type(declared)
            or sniff_content_type(body)
            or OCTET_STREAM
        )
        filename = _filename(config, state, content_type)
    except FileFromBase64Error as exc:
        return _failure(exc.reason, str(exc))

    plane = _shared_artifact_plane_from_state(state)
    if plane is None:
        return _failure("storage_unavailable", "File storage is not available")
    try:
        handle, meta = plane.create_artifact(
            owner_scope=_owner_scope_from_state(state),
            run_id=str(state.get("run_id") or state.get("job_id") or STEP_RUN_FALLBACK),
            step_id=str(state.get("step_id") or STEP_RUN_FALLBACK),
            filename=filename,
            content_type=content_type,
            retention_class="run_window",
            body=body,
        )
    except Exception as exc:
        return _failure("storage_error", f"The file could not be stored: {exc}")
    artifact_id = _text(getattr(handle, "artifact_id", "") or (meta or {}).get("artifact_id"))
    return {
        "ok": True,
        "artifact_handle": f"artifact:{artifact_id}",
        "artifact_id": artifact_id,
        "filename": _text((meta or {}).get("filename")) or filename,
        "content_type": content_type,
        "size_bytes": len(body),
        "sha256": _text((meta or {}).get("sha256")),
    }


__all__ = [
    "FileFromBase64Error",
    "decode_base64",
    "execute_file_from_base64_step",
    "sniff_content_type",
    "validate_file_from_base64_config",
]
