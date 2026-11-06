"""The report one workflow run returned, split for a chat host (ADR 0059, task 3).

One read serves the ``workflow_run_report`` MCP tool. ``model`` is everything a
model may see: the job's state and the bounded report preview of decision 11
(kind, version, title, summary text and counts, never datasets, rows or
component values). ``snapshot`` is the full ReportSnapshot, for the report
viewer only; the MCP adapter sends it in the tool result ``_meta``, which hosts
keep from the model. The read is the job status read with its workflow result:
job scope and workflow access are checked by ``get_job_status``.
"""

from __future__ import annotations

from typing import Any

from core.application.management.query_models import JobStatusView
from core.application.observability_vocabulary import job_status_meta
from core.application.workflow_documents import is_report, report_preview

#: The report is in ``snapshot``; ``model.report`` holds its preview.
STATE_READY = "ready"
#: The run has not ended; a later read may find its report.
STATE_PENDING = "pending"
#: The run ended without a report (it failed, or its workflow publishes none).
STATE_NO_REPORT = "no_report"


def _text(job_id: str, status: str, outcome: str, state: str, report: dict[str, Any] | None) -> str:
    if state == STATE_READY and report is not None:
        return str(report.get("summary_text") or report.get("title") or "")
    if state == STATE_PENDING:
        return f"Job {job_id} is {status}; its report is not ready yet."
    return f"Job {job_id} ended ({outcome}) without a report."


def workflow_run_report(status: JobStatusView) -> dict[str, Any]:
    """The model-safe part and the viewer's snapshot of one job's report."""
    meta = job_status_meta(status.status)
    terminal = bool(meta.get("terminal"))
    outcome = str(meta.get("outcome") or "unknown")
    result = status.workflow_result
    name = result.report_output if result is not None else None
    value = result.outputs.get(name) if result is not None and name else None
    snapshot = value if is_report(value) else None
    if snapshot is not None:
        state = STATE_READY
    elif not terminal:
        state = STATE_PENDING
    else:
        state = STATE_NO_REPORT
    report = report_preview(snapshot) if snapshot is not None else None
    return {
        "model": {
            "job": {
                "id": status.id,
                "status": status.status,
                "outcome": outcome,
                "terminal": terminal,
            },
            "state": state,
            "report_output": name if snapshot is not None else None,
            "report": report,
            "text": _text(status.id, status.status, outcome, state, report),
        },
        "snapshot": snapshot,
    }


__all__ = ["STATE_NO_REPORT", "STATE_PENDING", "STATE_READY", "workflow_run_report"]
