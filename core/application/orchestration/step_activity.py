"""Tell a running job that a long connector step is still alive.

A connector or extension step blocks until its runtime returns: an extension
exporting a whole catalog can take many minutes and says nothing on the way.
Without a signal, Job Details shows a step that started and nothing after it,
and the operator cannot tell a slow step from a dead one. While the step blocks,
a background thread refreshes the job heartbeat and names the step and how long
it has been running.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

STEP_HEARTBEAT_INTERVAL_SECONDS = 15.0


def _elapsed_text(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def _beat(bl: Any, job_id: str, excerpt: str) -> None:
    try:
        bl.update_job_runtime(job_id, heartbeat_now=True, last_response_excerpt=excerpt)
    except Exception as exc:
        # A missed heartbeat must never fail the step it describes.
        logger.debug("step heartbeat failed: job_id=%s error=%s", job_id, exc)


@contextmanager
def step_activity_heartbeat(
    bl: Any,
    *,
    job_id: Any,
    label: str,
    interval_seconds: float = STEP_HEARTBEAT_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    """Refresh the job heartbeat every ``interval_seconds`` while the block runs."""
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id or bl is None or not hasattr(bl, "update_job_runtime"):
        yield
        return

    started = clock()
    stop = threading.Event()
    _beat(bl, normalized_job_id, f"{label}: started")

    def _run() -> None:
        while not stop.wait(interval_seconds):
            _beat(
                bl,
                normalized_job_id,
                f"{label}: running for {_elapsed_text(clock() - started)}",
            )

    thread = threading.Thread(target=_run, name="step-activity-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=interval_seconds)


STEP_PROGRESS_MIN_INTERVAL_SECONDS = 2.0


def _progress_text(update: dict[str, Any]) -> str:
    message = str(update.get("message") or "").strip() or "Working"
    done = update.get("done")
    total = update.get("total")
    if isinstance(done, int) and isinstance(total, int) and total:
        return f"{message} ({done} of {total})"
    if isinstance(done, int):
        return f"{message} ({done})"
    return message


def step_progress_reporter(
    bl: Any,
    *,
    job_id: Any,
    step_id: str,
    label: str,
    min_interval_seconds: float = STEP_PROGRESS_MIN_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[dict[str, Any]], None] | None:
    """Turn an extension's progress updates into step events and heartbeats.

    Updates closer together than ``min_interval_seconds`` are dropped, except
    the one that reaches its total, so a chatty extension cannot flood the job
    log.
    """
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id or bl is None or not hasattr(bl, "log_event"):
        return None
    lock = threading.Lock()
    last_sent = [float("-inf")]

    def report(update: dict[str, Any]) -> None:
        done = update.get("done")
        total = update.get("total")
        final = isinstance(done, int) and isinstance(total, int) and total and done >= total
        with lock:
            now = clock()
            if now - last_sent[0] < min_interval_seconds and not final:
                return
            last_sent[0] = now
        text = _progress_text(update)
        try:
            bl.log_event(
                job_id=normalized_job_id,
                phase=step_id or "workflow",
                agent="workflow_runtime",
                event_type="step_progress",
                title=text,
                detail={
                    "step_id": step_id,
                    "message": str(update.get("message") or ""),
                    "done": done if isinstance(done, int) else None,
                    "total": total if isinstance(total, int) else None,
                    "source": "extension",
                },
                severity="info",
            )
        except Exception as exc:
            logger.debug("step progress event failed: job_id=%s error=%s", normalized_job_id, exc)
        if hasattr(bl, "update_job_runtime"):
            _beat(bl, normalized_job_id, f"{label}: {text}")

    return report


__all__ = [
    "STEP_HEARTBEAT_INTERVAL_SECONDS",
    "STEP_PROGRESS_MIN_INTERVAL_SECONDS",
    "step_activity_heartbeat",
    "step_progress_reporter",
]
