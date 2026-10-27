"""Bounded stdout/stderr capture and diagnostic rendering."""

from __future__ import annotations

import threading
from tempfile import SpooledTemporaryFile

_MAX_CAPTURE_CHARS = 20_000


_MAX_CAPTURE_STORAGE_CHARS = 1_000_000


class _StreamingCapture:
    """Mutable stdout/stderr capture shared across streaming failure paths."""

    def __init__(self) -> None:
        self._streams = {
            "stdout": SpooledTemporaryFile(  # noqa: SIM115 - owned for capture lifetime
                mode="w+", max_size=256_000, encoding="utf-8"
            ),
            "stderr": SpooledTemporaryFile(  # noqa: SIM115 - owned for capture lifetime
                mode="w+", max_size=256_000, encoding="utf-8"
            ),
        }
        self._sizes = {"stdout": 0, "stderr": 0}
        self._lock = threading.Lock()

    def append(self, stream_name: str, line: str) -> None:
        name = "stdout" if stream_name == "stdout" else "stderr"
        with self._lock:
            remaining = _MAX_CAPTURE_STORAGE_CHARS - self._sizes[name]
            if remaining <= 0:
                return
            chunk = line[:remaining]
            self._streams[name].write(chunk)
            self._sizes[name] += len(chunk)

    def _read(self, name: str) -> str:
        with self._lock:
            stream = self._streams[name]
            position = stream.tell()
            stream.seek(0)
            value = stream.read()
            stream.seek(position)
            return value

    @property
    def stdout(self) -> str:
        return self._read("stdout")

    @property
    def stderr(self) -> str:
        return self._read("stderr")


def _tail(value: str | bytes | None) -> str:
    text = value.decode(errors="replace") if isinstance(value, bytes) else str(value or "")
    if len(text) <= _MAX_CAPTURE_CHARS:
        return text
    marker = f"\n...[output truncated to {_MAX_CAPTURE_CHARS} chars; middle omitted]...\n"
    if len(marker) + 2 >= _MAX_CAPTURE_CHARS:
        return text[:_MAX_CAPTURE_CHARS]
    remaining = _MAX_CAPTURE_CHARS - len(marker)
    head_len = max(1, remaining // 2)
    tail_len = max(1, remaining - head_len)
    return f"{text[:head_len]}{marker}{text[-tail_len:]}"


def _append_bounded_diagnostic(stderr: str, diagnostic: str) -> str:
    """Retain a terminal diagnostic without exceeding the capture bound."""
    value = str(stderr or "")
    message = str(diagnostic or "")
    if not message or value.endswith(message):
        return value[:_MAX_CAPTURE_STORAGE_CHARS]
    separator = "" if not value or value.endswith("\n") else "\n"
    combined = f"{value}{separator}{message}"
    if len(combined) <= _MAX_CAPTURE_STORAGE_CHARS:
        return combined
    suffix = f"\n{message}" if value else message
    if len(suffix) >= _MAX_CAPTURE_STORAGE_CHARS:
        return suffix[-_MAX_CAPTURE_STORAGE_CHARS:]
    prefix_length = _MAX_CAPTURE_STORAGE_CHARS - len(suffix)
    return f"{value[:prefix_length]}{suffix}"
