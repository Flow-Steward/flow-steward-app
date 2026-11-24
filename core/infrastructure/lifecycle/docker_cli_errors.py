"""Read the error a Flow Steward CLI in a container reported as JSON."""

from __future__ import annotations

import json
from typing import Any


def cli_error_detail(stdout: Any) -> str:
    """The error a Flow Steward CLI in a container reported as JSON on stdout.

    0.9.25: `database upgrade --json` in a one-shot container reports its failure on
    stdout and leaves stderr to Compose, so the release upgrade showed Compose's
    "Container ... Created" lines as the migration problem and hid the real one.
    """
    try:
        payload = json.loads(str(stdout or ""))
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    code = str(error.get("code") or "").strip()
    message = str(error.get("message") or "").strip()
    hint = str(error.get("hint") or "").strip()
    text = ": ".join(part for part in (code, message) if part)
    return f"{text} {hint}".strip() if text else ""


__all__ = ["cli_error_detail"]
