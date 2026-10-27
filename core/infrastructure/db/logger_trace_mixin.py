from __future__ import annotations

from typing import Any


def _helpers():
    from core.infrastructure.db import platform_logger as _platform_logger

    return _platform_logger


def _as_dict(value):
    return _helpers()._as_dict(value)


def _default_account_id() -> str:
    return _helpers()._default_account_id()


def _trace_id_for_job(job_id) -> str | None:
    return _helpers()._trace_id_for_job(job_id)


def _trace_json_value(value, *, default):
    return _helpers()._trace_json_value(value, default=default)


def _trace_iso(value) -> str | None:
    return _helpers()._trace_iso(value)


def _redact_trace_value(value, *, max_len: int = 1000):
    return _helpers()._redact_trace_value(value, max_len=max_len)


def _truncate_text(value, max_len: int = 4000) -> str | None:
    return _helpers()._truncate_text(value, max_len=max_len)


class PlatformLoggerTraceMixin:
    get_job: Any
    get_logs: Any
    get_events: Any
    get_agent_messages: Any
    list_extension_invocations: Any
    list_domain_events: Any
    list_event_dispatches: Any
    list_job_errors: Any
    get_project: Any

    @staticmethod
    def _trace_span(
        *,
        span_id: str,
        trace_id: str,
        kind: str,
        layer: str,
        name: str,
        parent_span_id: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        correlation_id: str | None = None,
        started_at=None,
        finished_at=None,
        occurred_at=None,
        duration_ms: int | None = None,
        attributes: dict | None = None,
    ) -> dict[str, Any]:
        return {
            "span_id": span_id,
            "trace_id": trace_id,
            "parent_span_id": parent_span_id,
            "kind": str(kind or "span").strip() or "span",
            "layer": str(layer or "runtime").strip() or "runtime",
            "name": str(name or layer or kind or "span").strip() or "span",
            "status": str(status or "").strip() or None,
            "run_id": str(run_id or "").strip() or None,
            "parent_run_id": str(parent_run_id or "").strip() or None,
            "correlation_id": str(correlation_id or "").strip() or None,
            "started_at": _trace_iso(started_at),
            "finished_at": _trace_iso(finished_at),
            "occurred_at": _trace_iso(occurred_at),
            "duration_ms": int(duration_ms) if duration_ms is not None else None,
            "attributes": _as_dict(attributes),
        }

    @staticmethod
    def _trace_sort_key(span: dict[str, Any]) -> tuple[str, str, str]:
        started = str(
            span.get("started_at") or span.get("occurred_at") or span.get("finished_at") or ""
        )
        layer = str(span.get("layer") or "")
        span_id = str(span.get("span_id") or "")
        return (started, layer, span_id)

    @staticmethod
    def _trace_log_layer(action_type: str) -> tuple[str, str]:
        normalized = str(action_type or "").strip().lower()
        if normalized == "llm_call":
            return "llm", "span"
        if normalized.startswith("node_"):
            return "node", "span"
        if normalized.startswith("tool_"):
            return "tool", "span"
        return "log", "log"

    def build_job_trace(
        self,
        job_id: str,
        *,
        account_id: str | None = None,
        limit: int = 500,
        job: dict[str, Any] | None = None,
    ) -> dict | None:
        normalized_job_id = str(job_id or "").strip()
        resolved_job = dict(job) if isinstance(job, dict) else None
        if resolved_job is None:
            get_job = getattr(self, "get_job", None)
            resolved_job = get_job(normalized_job_id) if callable(get_job) else None
        if not resolved_job:
            return None

        project_id = resolved_job.get("project_id")
        get_project = getattr(self, "get_project", None)
        project = get_project(str(project_id)) if project_id and callable(get_project) else {}
        resolved_account_id = (
            str(
                resolved_job.get("account_id")
                or (project or {}).get("account_id")
                or _default_account_id()
            ).strip()
            or _default_account_id()
        )
        normalized_account_id = str(account_id or "").strip()
        if normalized_account_id and normalized_account_id != resolved_account_id:
            return None

        trace_id = str(
            _trace_json_value(resolved_job.get("meta"), default={}).get("trace_id")
            or _trace_id_for_job(job_id)
            or f"job:{normalized_job_id}"
        ).strip()
        root_span_id = f"{trace_id}:root"
        root_run_id = (
            str(
                _trace_json_value(resolved_job.get("meta"), default={}).get("root_run_id") or ""
            ).strip()
            or None
        )

        spans: list[dict[str, Any]] = [
            self._trace_span(
                span_id=root_span_id,
                trace_id=trace_id,
                kind="run",
                layer="job",
                name=str(
                    resolved_job.get("task_summary")
                    or resolved_job.get("task_id")
                    or f"job:{job_id}"
                ).strip()
                or f"job:{job_id}",
                status=str(resolved_job.get("status") or "").strip() or None,
                run_id=root_run_id,
                started_at=resolved_job.get("started_at") or resolved_job.get("queued_at"),
                finished_at=resolved_job.get("finished_at"),
                duration_ms=resolved_job.get("duration_ms"),
                attributes={
                    "job_id": normalized_job_id,
                    "project_id": str(project_id) if project_id is not None else None,
                    "project_name": str(
                        resolved_job.get("project_name") or (project or {}).get("name") or ""
                    ).strip()
                    or None,
                    "thread_id": str(resolved_job.get("thread_id") or "").strip() or None,
                    "selected_workflow": str(resolved_job.get("selected_workflow") or "").strip()
                    or None,
                    "current_step_id": str(resolved_job.get("current_step_id") or "").strip()
                    or None,
                    "current_group_id": str(resolved_job.get("current_group_id") or "").strip()
                    or None,
                    "current_agent": str(resolved_job.get("current_agent") or "").strip() or None,
                },
            )
        ]

        correlation_index: dict[str, str] = {}
        tool_call_index: dict[str, str] = {}
        safe_limit = max(1, min(int(limit or 500), 2000))

        logs = (
            self.get_logs(normalized_job_id, since_id=0, limit=safe_limit, include_deleted=False)
            or []
        )
        for row in logs:
            if not isinstance(row, dict):
                continue
            meta = _as_dict(_trace_json_value(row.get("meta"), default={}))
            run_id = str(meta.get("run_id") or "").strip() or None
            parent_run_id = str(meta.get("parent_run_id") or "").strip() or None
            correlation_id = str(meta.get("correlation_id") or "").strip() or None
            span_id = str(meta.get("span_id") or "").strip() or (
                f"run:{run_id}" if run_id else f"log:{str(row.get('id') or '').strip()}"
            )
            parent_span_id = str(meta.get("parent_span_id") or "").strip() or (
                f"run:{parent_run_id}" if parent_run_id else root_span_id
            )
            action_type = str(row.get("action_type") or "").strip() or "log"
            layer, kind = self._trace_log_layer(action_type)
            if correlation_id:
                correlation_index.setdefault(correlation_id, span_id)
            spans.append(
                self._trace_span(
                    span_id=span_id,
                    trace_id=trace_id,
                    parent_span_id=parent_span_id,
                    kind=kind,
                    layer=layer,
                    name=action_type,
                    status=str(row.get("status") or "").strip() or None,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    correlation_id=correlation_id,
                    started_at=row.get("timestamp"),
                    duration_ms=row.get("duration"),
                    attributes={
                        "log_id": str(row.get("id") or "").strip(),
                        "agent": str(row.get("agent") or "").strip() or None,
                        "model_used": str(row.get("model_used") or "").strip() or None,
                        "tokens_prompt": row.get("tokens_prompt"),
                        "tokens_completion": row.get("tokens_completion"),
                        "tokens_total": row.get("tokens_total"),
                        "cost_usd": float(row.get("cost_usd") or 0)
                        if row.get("cost_usd") is not None
                        else None,
                        "payload": _redact_trace_value(
                            _trace_json_value(row.get("payload"), default={})
                        ),
                        "result": _redact_trace_value(
                            _trace_json_value(row.get("result"), default={})
                        ),
                        "error": _truncate_text(row.get("error"), 1000),
                    },
                )
            )

        semantic_events = (
            self.get_events(normalized_job_id, since_id=0, limit=min(safe_limit, 500)) or []
        )
        for row in semantic_events:
            if not isinstance(row, dict):
                continue
            event_type = str(row.get("event_type") or "").strip() or "event"
            detail = _as_dict(_trace_json_value(row.get("detail"), default={}))
            tool_call_id = str(detail.get("tool_call_id") or "").strip() or None
            correlation_id = str(detail.get("correlation_id") or "").strip() or None
            tool_name = str(detail.get("tool_name") or "").strip() or None
            if event_type in {"tool_call", "tool_result"}:
                base_span_id = (
                    f"tool:{tool_call_id}"
                    if tool_call_id
                    else f"tool_event:{str(row.get('id') or '').strip()}"
                )
                span_id = base_span_id if event_type == "tool_call" else f"{base_span_id}:result"
                if event_type == "tool_result":
                    parent_span_candidate = tool_call_index.get(tool_call_id or "")
                elif correlation_id:
                    parent_span_candidate = correlation_index.get(correlation_id)
                else:
                    parent_span_candidate = None
                parent_span_id = parent_span_candidate or root_span_id
                if bool(detail.get("is_error")):
                    tool_span_status = "error"
                elif event_type == "tool_call":
                    tool_span_status = "requested"
                else:
                    tool_span_status = "done"
                if tool_call_id and event_type == "tool_call":
                    tool_call_index[tool_call_id] = span_id
                if correlation_id:
                    correlation_index.setdefault(correlation_id, span_id)
                spans.append(
                    self._trace_span(
                        span_id=span_id,
                        trace_id=trace_id,
                        parent_span_id=parent_span_id,
                        kind="span",
                        layer="tool",
                        name=tool_name or event_type,
                        status=tool_span_status,
                        correlation_id=correlation_id,
                        occurred_at=row.get("timestamp"),
                        attributes={
                            "event_id": str(row.get("id") or "").strip(),
                            "event_type": event_type,
                            "phase": str(row.get("phase") or "").strip() or None,
                            "agent": str(row.get("agent") or "").strip() or None,
                            "severity": str(row.get("severity") or "").strip() or None,
                            "title": _truncate_text(row.get("title"), 1000),
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                            "detail": _redact_trace_value(detail),
                        },
                    )
                )

        messages = (
            self.get_agent_messages(normalized_job_id, since_id=0, limit=min(safe_limit, 500)) or []
        )
        for row in messages:
            if not isinstance(row, dict):
                continue
            correlation_id = str(row.get("correlation_id") or "").strip() or None
            span_id = f"message:{str(row.get('id') or '').strip()}"
            if correlation_id:
                correlation_index.setdefault(correlation_id, span_id)
            spans.append(
                self._trace_span(
                    span_id=span_id,
                    trace_id=trace_id,
                    parent_span_id=root_span_id,
                    kind="span",
                    layer="prompt",
                    name=str(
                        row.get("skill") or row.get("step_id") or row.get("from_agent") or "message"
                    ).strip()
                    or "message",
                    status=str(row.get("status") or "").strip() or None,
                    correlation_id=correlation_id,
                    occurred_at=row.get("timestamp"),
                    attributes={
                        "message_id": str(row.get("id") or "").strip(),
                        "from_agent": str(row.get("from_agent") or "").strip() or None,
                        "to_agent": str(row.get("to_agent") or "").strip() or None,
                        "step_id": str(row.get("step_id") or "").strip() or None,
                        "step_execution_id": str(row.get("step_execution_id") or "").strip()
                        or None,
                        "attempt": row.get("attempt"),
                        "group_id": str(row.get("group_id") or "").strip() or None,
                        "skill": str(row.get("skill") or "").strip() or None,
                        "summary": _truncate_text(row.get("summary"), 1000),
                    },
                )
            )

        extension_rows = (
            self.list_extension_invocations(
                job_id=normalized_job_id, account_id=resolved_account_id, limit=min(safe_limit, 500)
            )
            or []
        )
        for row in extension_rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("job_id") or "").strip() != normalized_job_id:
                continue
            request_context = _as_dict(row.get("request_context_jsonb"))
            correlation_id = str(request_context.get("correlation_id") or "").strip() or None
            event_id = str(request_context.get("event_id") or "").strip() or None
            span_id = (
                str(request_context.get("span_id") or "").strip()
                or f"extension:{str(row.get('id') or '').strip()}"
            )
            parent_span_id = (
                str(request_context.get("parent_span_id") or "").strip()
                or (f"event:{event_id}" if event_id else "")
                or (correlation_index.get(correlation_id) if correlation_id else None)
                or root_span_id
            )
            if correlation_id:
                correlation_index.setdefault(correlation_id, span_id)
            spans.append(
                self._trace_span(
                    span_id=span_id,
                    trace_id=str(request_context.get("trace_id") or trace_id).strip() or trace_id,
                    parent_span_id=parent_span_id,
                    kind="span",
                    layer="extension",
                    name=str(row.get("invocation_type") or "extension").strip() or "extension",
                    status=str(row.get("status") or "").strip() or None,
                    correlation_id=correlation_id,
                    occurred_at=row.get("created_at"),
                    attributes={
                        "invocation_id": str(row.get("id") or "").strip(),
                        "query_id": str(row.get("query_id") or "").strip() or None,
                        "action_id": str(row.get("action_id") or "").strip() or None,
                        "command_id": str(row.get("command_id") or "").strip() or None,
                        "request_context": _redact_trace_value(request_context),
                        "request": _redact_trace_value(row.get("request_jsonb")),
                        "response": _redact_trace_value(row.get("response_jsonb")),
                        "error_text": _truncate_text(row.get("error_text"), 1000),
                    },
                )
            )

        domain_events = (
            self.list_domain_events(
                account_id=resolved_account_id,
                job_id=normalized_job_id,
                limit=min(safe_limit, 500),
            )
            or []
        )
        for event_row in domain_events:
            if not isinstance(event_row, dict):
                continue
            envelope = _as_dict(_trace_json_value(event_row.get("envelope_jsonb"), default={}))
            metadata = _as_dict(envelope.get("metadata"))
            correlation_id = (
                str(event_row.get("correlation_id") or envelope.get("correlation_id") or "").strip()
                or None
            )
            event_id = str(event_row.get("event_id") or "").strip() or None
            span_id = f"event:{event_id or str(event_row.get('id') or '').strip()}"
            parent_span_id = (
                str(metadata.get("parent_span_id") or "").strip()
                or (correlation_index.get(correlation_id) if correlation_id else None)
                or root_span_id
            )
            if correlation_id:
                correlation_index.setdefault(correlation_id, span_id)
            spans.append(
                self._trace_span(
                    span_id=span_id,
                    trace_id=str(metadata.get("trace_id") or trace_id).strip() or trace_id,
                    parent_span_id=parent_span_id,
                    kind="span",
                    layer="event",
                    name=str(event_row.get("event_type") or "event").strip() or "event",
                    status=str(event_row.get("status") or "").strip() or None,
                    correlation_id=correlation_id,
                    occurred_at=event_row.get("occurred_at") or event_row.get("created_at"),
                    attributes={
                        "event_id": event_id,
                        "severity": str(event_row.get("severity") or "").strip() or None,
                        "summary": _truncate_text(event_row.get("summary"), 1000),
                        "group_id": str(event_row.get("group_id") or "").strip() or None,
                        "step_id": str(event_row.get("step_id") or "").strip() or None,
                        "step_execution_id": str(event_row.get("step_execution_id") or "").strip()
                        or None,
                        "payload": _redact_trace_value(event_row.get("payload_jsonb")),
                        "source": str(event_row.get("source") or "").strip() or None,
                    },
                )
            )
            for dispatch in (
                self.list_event_dispatches(
                    event_id=event_id, account_id=resolved_account_id, limit=100
                )
                or []
            ):
                if not isinstance(dispatch, dict):
                    continue
                dispatch_id = (
                    str(dispatch.get("dispatch_id") or "").strip()
                    or f"dispatch:{str(dispatch.get('id') or '').strip()}"
                )
                spans.append(
                    self._trace_span(
                        span_id=f"dispatch:{dispatch_id}",
                        trace_id=trace_id,
                        parent_span_id=span_id,
                        kind="span",
                        layer="dispatch",
                        name=str(dispatch.get("target_type") or "dispatch").strip() or "dispatch",
                        status=str(dispatch.get("status") or "").strip() or None,
                        occurred_at=dispatch.get("created_at"),
                        attributes={
                            "dispatch_id": dispatch_id,
                            "target_id": str(dispatch.get("target_id") or "").strip() or None,
                            "target_label": str(dispatch.get("target_label") or "").strip() or None,
                            "reason_code": str(dispatch.get("reason_code") or "").strip() or None,
                            "reason_detail": _truncate_text(dispatch.get("reason_detail"), 1000),
                            "attempt_count": dispatch.get("attempt_count"),
                        },
                    )
                )

        errors = self.list_job_errors(normalized_job_id, limit=min(safe_limit, 200)) or []
        for row in errors:
            if not isinstance(row, dict):
                continue
            context = _as_dict(_trace_json_value(row.get("context_jsonb"), default={}))
            correlation_id = str(context.get("correlation_id") or "").strip() or None
            run_id = str(context.get("run_id") or "").strip() or None
            span_id = (
                str(context.get("span_id") or "").strip()
                or f"error:{str(row.get('id') or '').strip()}"
            )
            parent_span_id = (
                str(context.get("parent_span_id") or "").strip()
                or (f"run:{run_id}" if run_id else "")
                or (correlation_index.get(correlation_id) if correlation_id else None)
                or root_span_id
            )
            spans.append(
                self._trace_span(
                    span_id=span_id,
                    trace_id=str(context.get("trace_id") or trace_id).strip() or trace_id,
                    parent_span_id=parent_span_id,
                    kind="span",
                    layer="error",
                    name=str(row.get("category") or row.get("source") or "error").strip()
                    or "error",
                    status="error",
                    run_id=run_id,
                    correlation_id=correlation_id,
                    occurred_at=row.get("created_at"),
                    attributes={
                        "error_id": str(row.get("id") or "").strip(),
                        "source": str(row.get("source") or "").strip() or None,
                        "category": str(row.get("category") or "").strip() or None,
                        "provider_name": str(row.get("provider_name") or "").strip() or None,
                        "request_id": str(row.get("request_id") or "").strip() or None,
                        "retryable": row.get("retryable"),
                        "error_message": _truncate_text(row.get("error_message"), 1000),
                        "response_excerpt": _truncate_text(row.get("response_excerpt"), 1000),
                        "context": _redact_trace_value(context),
                    },
                )
            )

        spans = sorted(spans, key=self._trace_sort_key)
        return {
            "trace_id": trace_id,
            "job_id": normalized_job_id,
            "project_id": str(project_id) if project_id is not None else None,
            "account_id": resolved_account_id,
            "status": str(resolved_job.get("status") or "").strip() or None,
            "root_span_id": root_span_id,
            "root_run_id": root_run_id,
            "linkage": {
                "correlation_ids": sorted(
                    {
                        str(span.get("correlation_id") or "").strip()
                        for span in spans
                        if str(span.get("correlation_id") or "").strip()
                    }
                ),
                "run_ids": sorted(
                    {
                        str(span.get("run_id") or "").strip()
                        for span in spans
                        if str(span.get("run_id") or "").strip()
                    }
                ),
            },
            "spans": spans,
        }
