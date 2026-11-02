"""The trace context a job carries in ``jobs.meta`` from its creation to every segment.

Context belongs to the job, not to a queue message: it is written once with the
job row, so it survives the outbox relay, ready-hint recovery and redelivery,
and every resume of the same job reads the same row. Only a bounded W3C
``traceparent``/``tracestate`` is stored; baggage never is.

- A job created inside a workflow step (a child workflow) is parented to that
  step's span; otherwise to the current span (an HTTP request, an MCP call).
- A job created with no active span gets the trace of its first segment
  stored, so a resume continues that trace with the same sampling decision.
- A manual or watchdog retry is a new job: a new trace, linked to the previous
  job's trace, sampled independently.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from typing import Any

from core.application.observability.workflow_telemetry import current_workflow_span
from core.infrastructure.observability import otel_instruments as instruments

TRACE_CONTEXT_KEY = "trace_context"
TRACE_LINK_KEY = "trace_link"


def stamp_job_meta(meta: MutableMapping[str, Any]) -> None:
    """Replace any caller-supplied trace keys of a new job's *meta* with server-side ones."""
    meta.pop(TRACE_CONTEXT_KEY, None)
    link = instruments.bounded_carrier(meta.pop(TRACE_LINK_KEY, None))
    if link is not None:
        meta[TRACE_LINK_KEY] = link
    carrier = instruments.trace_carrier(current_workflow_span())
    if carrier is not None:
        meta[TRACE_CONTEXT_KEY] = carrier


def retry_link(previous_meta: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The link a retry job keeps to the trace of the job it reruns."""
    if not isinstance(previous_meta, Mapping):
        return None
    carrier = instruments.bounded_carrier(previous_meta.get(TRACE_CONTEXT_KEY))
    if carrier is None:
        return None
    # Only the identity: the retry samples independently of the original.
    return {"traceparent": carrier["traceparent"]}


def job_trace_carriers(meta: Any) -> tuple[dict[str, str] | None, tuple[dict[str, str], ...]]:
    """The stored parent carrier of a job and the carriers its segments link to.

    *meta* is the ``jobs.meta`` value as a driver returns it: a mapping or JSON text.
    """
    if isinstance(meta, (str, bytes)):
        try:
            meta = json.loads(meta)
        except ValueError:
            return None, ()
    if not isinstance(meta, Mapping):
        return None, ()
    parent = instruments.bounded_carrier(meta.get(TRACE_CONTEXT_KEY))
    link = instruments.bounded_carrier(meta.get(TRACE_LINK_KEY))
    return parent, (link,) if link is not None else ()


__all__ = [
    "TRACE_CONTEXT_KEY",
    "TRACE_LINK_KEY",
    "job_trace_carriers",
    "retry_link",
    "stamp_job_meta",
]
