"""The channel an extension reports its progress on while it runs.

An extension runs as a subprocess and returns one JSON result when it exits;
until then the host sees nothing. The host opens a pipe for the run and names
its write end in ``FS_EXTENSION_PROGRESS_FD``. The SDK's ``report_progress``
writes one JSON line per update to it; a thread here reads them and hands each
to the progress sink of the step that started the run.

Progress is advisory: a malformed or oversized line is dropped, and a failing
sink never fails the run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

PROGRESS_FD_ENV = "FS_EXTENSION_PROGRESS_FD"
MAX_PROGRESS_LINE_BYTES = 4096
MAX_PROGRESS_MESSAGE_CHARS = 500

ProgressSink = Callable[[dict[str, Any]], None]

_current_sink: ContextVar[ProgressSink | None] = ContextVar("extension_progress_sink", default=None)


@contextmanager
def extension_progress_sink(sink: ProgressSink | None) -> Iterator[None]:
    """Route the progress of extension runs started inside the block to ``sink``."""
    token = _current_sink.set(sink)
    try:
        yield
    finally:
        _current_sink.reset(token)


def current_progress_sink() -> ProgressSink | None:
    return _current_sink.get()


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_progress_line(line: bytes) -> dict[str, Any] | None:
    """One progress update, or None when the line is not one."""
    if len(line) > MAX_PROGRESS_LINE_BYTES:
        return None
    try:
        payload = json.loads(line.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError and JSONDecodeError
        return None
    if not isinstance(payload, dict):
        return None
    ready = payload.get("output_ready")
    if isinstance(ready, dict):
        kind = ready.get("kind")
        object_id = ready.get("id")
        if kind in {"artifact", "dataset"} and isinstance(object_id, str) and object_id.strip():
            return {"output_ready": {"kind": kind, "id": object_id.strip()[:200]}}
        return None
    message = payload.get("message")
    update: dict[str, Any] = {
        "message": message.strip()[:MAX_PROGRESS_MESSAGE_CHARS] if isinstance(message, str) else "",
    }
    done = _count(payload.get("done"))
    total = _count(payload.get("total"))
    if done is not None:
        update["done"] = done
    if total is not None:
        update["total"] = total
    if not update["message"] and done is None:
        return None
    return update


def drain_progress(read_fd: int, sink: ProgressSink) -> None:
    """Read progress lines until the extension closes its end of the pipe."""
    with os.fdopen(read_fd, "rb") as stream:
        pending = b""
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                break
            pending += chunk
            *lines, pending = pending.split(b"\n")
            if len(pending) > MAX_PROGRESS_LINE_BYTES:
                pending = b""
            for line in lines:
                update = parse_progress_line(line.strip())
                if update is None:
                    continue
                try:
                    sink(update)
                except Exception as exc:
                    logger.debug("extension progress sink failed: %s", exc)


def close_quietly(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.close(fd)


__all__ = [
    "MAX_PROGRESS_LINE_BYTES",
    "PROGRESS_FD_ENV",
    "ProgressSink",
    "close_quietly",
    "current_progress_sink",
    "drain_progress",
    "extension_progress_sink",
    "parse_progress_line",
]
