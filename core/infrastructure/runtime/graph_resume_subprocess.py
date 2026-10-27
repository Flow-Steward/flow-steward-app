# mypy: ignore-errors
"""Subprocess entrypoint for approval graph resume execution."""

from __future__ import annotations

import json
import logging
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger("platform.graph_resume_subprocess")


def _load_payload(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m core.infrastructure.runtime.graph_resume_subprocess <payload.json>")
        return 2

    payload = _load_payload(args[0])
    thread_id = str(payload.get("thread_id") or "").strip()
    if not thread_id:
        print("graph resume payload missing thread_id")
        return 2

    try:
        from core.infrastructure.runtime import http_app_events_resume as http_app

        http_app._run_graph_resume(
            thread_id,
            dict(payload.get("resume_value") or {}),
            bool(payload.get("approved")),
            payload.get("action"),
            str(payload.get("comment") or ""),
            dict(payload.get("interrupt_payload") or {}),
            actor_username=str(payload.get("actor_username") or "operator"),
            approval_id=str(payload.get("approval_id") or "") or None,
        )
    except Exception as exc:
        logger.exception("graph resume subprocess failed for thread %s: %s", thread_id, exc)
        from core.infrastructure.runtime.graph_resume_runtime import TerminalPersistenceError

        if isinstance(exc, TerminalPersistenceError):
            # The checkpoint has already consumed the decision.  Reverting the
            # job to the old gate would make it impossible to complete the
            # durable terminal projection on recovery.
            return 1
        with suppress(Exception):
            from core.infrastructure.runtime import http_app_events_resume as http_app

            http_app._set_job_status_by_thread_id(thread_id, "awaiting_approval")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
