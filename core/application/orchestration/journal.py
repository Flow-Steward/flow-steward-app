"""Structured orchestrator journal helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def make_journal_entry(
    *,
    iteration: int = 0,
    event: str = "note",
    step_id: str = "",
    action: str = "",
    reason: str = "",
    message: str = "",
    detail: dict | None = None,
    severity: str = "info",
) -> dict:
    text = str(message or "").strip()
    if not text:
        text = event or "note"
        if action:
            text = f"{text}: {action}"
        if reason:
            text = f"{text} — {reason}"
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "iteration": int(iteration or 0),
        "event": str(event or "note"),
        "step_id": str(step_id or ""),
        "action": str(action or ""),
        "reason": str(reason or ""),
        "message": text,
        "severity": str(severity or "info"),
        "detail": dict(detail or {}),
    }


def _coerce_iteration(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def coerce_journal_entry(raw: Any) -> dict:
    if isinstance(raw, dict):
        return make_journal_entry(
            iteration=_coerce_iteration(raw.get("iteration")),
            event=str(raw.get("event") or "note"),
            step_id=str(raw.get("step_id") or ""),
            action=str(raw.get("action") or ""),
            reason=str(raw.get("reason") or ""),
            message=str(raw.get("message") or ""),
            detail=raw.get("detail") if isinstance(raw.get("detail"), dict) else {},
            severity=str(raw.get("severity") or "info"),
        )
    raise TypeError("orchestrator_journal entries must be structured dict objects")


def coerce_journal_entries(raw_entries: list[Any] | None) -> list[dict]:
    out: list[dict] = []
    for entry in raw_entries or []:
        out.append(coerce_journal_entry(entry))
    return out


def journal_message(entry: Any) -> str:
    coerced = coerce_journal_entry(entry)
    return str(coerced.get("message") or "")
