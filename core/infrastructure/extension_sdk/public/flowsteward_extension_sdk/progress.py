"""Tell the person watching a run what a long operation is doing.

An extension returns one result when it finishes; a step that takes minutes
otherwise shows nothing until then. Call ``report_progress`` as the work moves:
Job Details shows the message, and ``done`` of ``total`` when you give them, on
the running step.

    for page in pages:
        export(page)
        report_progress("Exporting products", done=exported, total=product_count)

It is cheap and safe to call often: the host shows the newest update and drops
the rest. Outside a Flow Steward run, or on a host without progress support, it
does nothing. It never raises.
"""

from __future__ import annotations

import json
import os

PROGRESS_FD_ENV = "FS_EXTENSION_PROGRESS_FD"
_MAX_MESSAGE_CHARS = 500


def report_progress(
    message: str = "",
    *,
    done: int | None = None,
    total: int | None = None,
) -> None:
    """Report progress of the running operation. Never raises."""
    update: dict[str, object] = {"message": str(message or "")[:_MAX_MESSAGE_CHARS]}
    if isinstance(done, int) and not isinstance(done, bool) and done >= 0:
        update["done"] = done
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        update["total"] = total
    _write_line(update)


def _write_line(update: dict[str, object]) -> None:
    raw_fd = os.environ.get(PROGRESS_FD_ENV, "")
    if not raw_fd.isdigit():
        return
    line = (json.dumps(update, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        os.write(int(raw_fd), line)
    except OSError:
        # The host stopped listening; progress is advisory, the work goes on.
        os.environ.pop(PROGRESS_FD_ENV, None)


def announce_output_ready(kind: str, object_id: str) -> None:
    """Tell the host an output finished uploading, so it can show the file now.

    Called by the SDK's own writers after an upload succeeds; never before, so
    the host never shows a partial file. Never raises.
    """
    normalized_id = str(object_id or "").strip()
    if normalized_id and kind in {"artifact", "dataset"}:
        _write_line({"output_ready": {"kind": kind, "id": normalized_id}})


__all__ = ["PROGRESS_FD_ENV", "announce_output_ready", "report_progress"]
