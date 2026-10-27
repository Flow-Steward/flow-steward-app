"""Shared scalar values for artifact storage and granted transfers."""

from __future__ import annotations

from typing import Any

APPLICATION_JSON_VALUE = "application/json"


OCTET_STREAM_VALUE = "application/octet-stream"


def _as_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_artifact_filename(filename: str | None) -> str:
    value = str(filename or "").strip() or "blob"
    if "\x00" in value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("Artifact filename must be a plain filename")
    return value
