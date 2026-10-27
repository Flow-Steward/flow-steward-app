"""Show a step's files as soon as each one is complete.

An extension writes its output files while it runs, but they used to become
visible only when the whole step finished. The SDK now announces each output
after its upload succeeds; this finalizes that one output then (size and digest,
as at the end of the step) and records an ``artifact_ready`` event, so Job
Details can show the file on the running step. Only outputs the step prepared
are accepted, and only a fully finalized file is announced.

What is finalized here is reused when the step ends. If the step fails, its
outputs are removed as before.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

OutputReadyHandler = Callable[[str, str], None]


def early_output_handler(
    *,
    grant_plane: Any,
    prepared_grants: Any,
    finalized: dict[str, dict[str, Any]],
    event_logger: Any,
    job_id: Any,
    step_id: str,
) -> OutputReadyHandler | None:
    """Finalize an announced output now and record that its file is ready.

    ``finalized`` collects what was finalized, keyed by the prepared output id,
    for ``finalize_prepared_extension_grants(already_finalized=...)``.
    """
    if grant_plane is None or prepared_grants is None:
        return None
    from core.application.extensions import runtime_grants

    try:
        prepared_ids = {
            kind: {
                runtime_grants.normalize_grant_id(object_id, kind=kind): object_id
                for object_id in getattr(prepared_grants, f"{kind}_output_ids", None) or []
            }
            for kind in ("artifact", "dataset")
        }
        max_sizes = {
            kind: dict(getattr(prepared_grants, f"{kind}_output_max_size_by_id", None) or {})
            for kind in ("artifact", "dataset")
        }
    except Exception as exc:
        # Showing files early is a convenience; the step still finalizes them at its end.
        logger.debug("early output handler unavailable: %s", exc)
        return None
    normalized_job_id = str(job_id or "").strip()
    lock = threading.Lock()

    def handle(kind: str, object_id: str) -> None:
        normalized_id = runtime_grants.normalize_grant_id(object_id, kind=kind)
        prepared_id = prepared_ids.get(kind, {}).get(normalized_id)
        if not prepared_id:
            # Not an output this step prepared: ignore, never finalize it.
            return
        with lock:
            if prepared_id in finalized:
                return
            try:
                rows, errors = runtime_grants.finalize_uploads(
                    grant_plane, [prepared_id], kind=kind, max_size_by_id=max_sizes[kind]
                )
            except Exception as exc:
                logger.debug("early output finalization failed: %s", exc)
                return
            meta = rows.get(normalized_id) or rows.get(prepared_id)
            if errors or not meta:
                # Left to the end of the step, which reports what went wrong.
                return
            finalized[prepared_id] = meta
        _record_ready(
            event_logger,
            job_id=normalized_job_id,
            step_id=step_id,
            kind=kind,
            object_id=normalized_id,
            meta=meta,
        )

    return handle


def _record_ready(
    event_logger: Any,
    *,
    job_id: str,
    step_id: str,
    kind: str,
    object_id: str,
    meta: dict[str, Any],
) -> None:
    if not job_id or event_logger is None or not hasattr(event_logger, "log_event"):
        return
    filename = str(meta.get("filename") or "").strip() or "file"
    detail: dict[str, Any] = {
        "step_id": step_id,
        "kind": kind,
        f"{kind}_id": object_id,
        "filename": filename,
        "content_type": str(meta.get("content_type") or "").strip(),
        "size_bytes": int(meta.get("size_bytes") or 0),
        "source": "extension",
    }
    if kind == "artifact":
        detail["download_url"] = f"/api/jobs/{job_id}/artifacts/{object_id}"
    try:
        event_logger.log_event(
            job_id=job_id,
            phase=step_id or "workflow",
            agent="workflow_runtime",
            event_type="artifact_ready",
            title=f"File ready: {filename}",
            detail=detail,
            severity="info",
        )
    except Exception as exc:
        logger.debug("artifact_ready event failed: job_id=%s error=%s", job_id, exc)


__all__ = ["OutputReadyHandler", "early_output_handler"]
