"""Shared helpers and constants for the worker runtime."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("platform.worker")
_RETRYABLE_INTERRUPTED_STEP_STATUS = "retryable_interrupted_step"


def _to_json_obj(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _truncate_text(value: object | None, limit: int) -> str:
    text = str(value or "")
    if len(text) <= max(0, int(limit)):
        return text
    return text[: max(0, int(limit))]
