"""Reusable helpers for job trace, replay, and causal-chain projections."""

from __future__ import annotations

import json
from typing import Any


def mermaid_label(text: str) -> str:
    label = str(text or "").replace('"', "'").replace("\n", " ").strip()
    if len(label) > 120:
        label = label[:117] + "..."
    return label


def resolve_job_project_name(bl, job: dict[str, Any], *, to_json_obj) -> str:
    """Resolve a stable project name for job detail pages."""
    if not isinstance(job, dict):
        return ""

    project_name = str(job.get("project_name") or "").strip()
    project_id = str(job.get("project_id") or "").strip()

    if project_name:
        return project_name

    if project_id and bl:
        try:
            project_row = bl.get_project(project_id)
            project_name = str((project_row or {}).get("name") or "").strip()
        except Exception:
            project_name = ""
        if project_name:
            return project_name

        if hasattr(bl, "get_project_run_config"):
            try:
                run_cfg = bl.get_project_run_config(project_id) or {}
                project_name = str((run_cfg or {}).get("project_name") or "").strip()
            except Exception:
                project_name = ""
            if project_name:
                return project_name

    meta = to_json_obj(job.get("meta")) or {}
    if isinstance(meta, dict):
        snapshot = to_json_obj(meta.get("project_config_snapshot")) or {}
        if isinstance(snapshot, dict):
            project_name = str(snapshot.get("project_name") or "").strip()
        if not project_name:
            project_name = str(meta.get("project_name") or "").strip()
        if project_name:
            return project_name

    return ""


def build_resolve_job_project_name(*, to_json_obj):
    def _resolver(bl, job: dict[str, Any]) -> str:
        return resolve_job_project_name(bl, job, to_json_obj=to_json_obj)

    return _resolver


def _opaque_id(value: Any) -> str:
    normalized = str(value or "").strip()
    return "" if normalized == "0" else normalized


def _node_id(prefix: str, value: Any) -> str:
    normalized = _opaque_id(value)
    safe = "".join(ch if ch.isalnum() else "_" for ch in normalized)
    return f"{prefix}{safe}" if safe else prefix


def _routing_events_for_message(
    message: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    message_id = _opaque_id(message.get("id"))
    corr = str(message.get("correlation_id") or "").strip()
    step_id = str(message.get("step_id") or "").strip()
    routing_events: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("event_type") or "") not in {"routing_decision", "decision"}:
            continue
        detail_raw = event.get("detail")
        detail: dict[str, Any] = detail_raw if isinstance(detail_raw, dict) else {}
        cause_message_id = _opaque_id(detail.get("message_id") or detail.get("cause_message_id"))
        cause_corr = str(detail.get("cause_correlation_id") or "").strip()
        if message_id and cause_message_id == message_id:
            routing_events.append(event)
            continue
        if corr and cause_corr == corr:
            routing_events.append(event)
    if routing_events or not step_id:
        return routing_events[-10:]

    for event in events:
        if str(event.get("event_type") or "") not in {"routing_decision", "decision"}:
            continue
        detail_raw = event.get("detail")
        detail: dict[str, Any] = detail_raw if isinstance(detail_raw, dict) else {}
        event_step = str(detail.get("step_id") or "").strip()
        if event_step == step_id:
            annotated = dict(event)
            annotated_detail = dict(detail or {})
            annotated_detail["linkage_method"] = "heuristic_step"
            annotated["detail"] = annotated_detail
            routing_events.append(annotated)
    return routing_events[-10:]


def _related_messages(
    all_messages: list[dict[str, Any]],
    caused_by: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    corr_refs: set[str] = set()
    for ref in caused_by:
        if not isinstance(ref, dict):
            continue
        for key in ("correlation_id", "cause_correlation_id"):
            value = str(ref.get(key) or "").strip()
            if value:
                corr_refs.add(value)

    related_messages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for message in all_messages:
        message_corr = str(message.get("correlation_id") or "").strip()
        message_id = _opaque_id(message.get("id"))
        if message_corr and message_corr in corr_refs and message_id and message_id not in seen_ids:
            seen_ids.add(message_id)
            related_messages.append(message)
    return related_messages


def collect_replay_step_trace(
    bl,
    job_id: str,
    message_row: dict[str, Any],
    *,
    serialize_agent_message_row,
    serialize_event_row,
    serialize_log_row,
    to_json_obj,
) -> dict[str, Any]:
    """Build replay/debug trace payload for one message."""
    message = serialize_agent_message_row(message_row)
    corr = str(message.get("correlation_id") or "").strip()
    producer = str(message.get("from_agent") or "").strip()
    caused_by = message.get("caused_by") if isinstance(message.get("caused_by"), list) else []

    try:
        events_rows = bl.get_events(job_id, since_id=0, limit=2000) or []
    except Exception:
        events_rows = []
    events = [serialize_event_row(dict(row)) for row in events_rows if isinstance(row, dict)]
    routing_events = _routing_events_for_message(message, events)

    try:
        log_rows = bl.get_logs(job_id, since_id=0, limit=2000, newest=True) or []
    except Exception:
        log_rows = []
    prompt_request = None
    prompt_response = None
    for row in log_rows:
        if not isinstance(row, dict):
            continue
        action_type = str(row.get("action_type") or "")
        meta = to_json_obj(row.get("meta")) or {}
        if not isinstance(meta, dict):
            meta = {}
        row_corr = str(meta.get("correlation_id") or "").strip()
        row_agent = str(row.get("agent") or "")
        if corr and row_corr != corr:
            continue
        if producer and row_agent and row_agent != producer:
            continue
        if action_type == "prompt_request":
            prompt_request = serialize_log_row(row, job_id)
        elif action_type in {"prompt_response", "cli_response"}:
            prompt_response = serialize_log_row(row, job_id)

    try:
        all_rows = bl.get_agent_messages(job_id, since_id=0, limit=2000) or []
    except Exception:
        all_rows = []
    all_messages = [
        serialize_agent_message_row(dict(row)) for row in all_rows if isinstance(row, dict)
    ]

    return {
        "job_id": job_id,
        "message": message,
        "caused_by": caused_by,
        "routing_decisions": routing_events,
        "prompt_request": prompt_request,
        "prompt_response": prompt_response,
        "related_messages": _related_messages(all_messages, caused_by),
    }


def build_collect_replay_step_trace_handler(
    *,
    serialize_agent_message_row,
    serialize_event_row,
    serialize_log_row,
    to_json_obj,
):
    def _handler(bl, job_id: str, message_row: dict[str, Any]) -> dict[str, Any]:
        return collect_replay_step_trace(
            bl,
            job_id,
            message_row,
            serialize_agent_message_row=serialize_agent_message_row,
            serialize_event_row=serialize_event_row,
            serialize_log_row=serialize_log_row,
            to_json_obj=to_json_obj,
        )

    return _handler


def build_job_causal_chain(
    bl,
    job_id: str,
    *,
    serialize_agent_message_row,
    serialize_event_row,
    mermaid_label_fn=mermaid_label,
) -> dict[str, Any]:
    messages_raw = (
        bl.get_agent_messages(job_id, since_id=0, limit=5000)
        if hasattr(bl, "get_agent_messages")
        else []
    )
    events_raw = bl.get_events(job_id, since_id=0, limit=2000) if hasattr(bl, "get_events") else []

    messages = [
        serialize_agent_message_row(dict(row))
        for row in (messages_raw or [])
        if isinstance(row, dict)
    ]
    events = [serialize_event_row(dict(row)) for row in (events_raw or []) if isinstance(row, dict)]
    message_by_id = {
        _opaque_id(message.get("id")): message
        for message in messages
        if _opaque_id(message.get("id"))
    }
    messages_by_corr: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        corr = str(message.get("correlation_id") or "").strip()
        if corr:
            messages_by_corr.setdefault(corr, []).append(message)
    for corr in messages_by_corr:
        messages_by_corr[corr].sort(
            key=lambda item: (_opaque_id(item.get("timestamp")), _opaque_id(item.get("id")))
        )

    event_by_id = {
        _opaque_id(event.get("id")): event for event in events if _opaque_id(event.get("id"))
    }
    external_nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []

    for message in messages:
        target_id = _opaque_id(message.get("id"))
        if not target_id:
            continue
        causes = message.get("caused_by") if isinstance(message.get("caused_by"), list) else []
        for cause in causes:
            if not isinstance(cause, dict):
                continue
            source_node_id, via = _resolve_causal_source(
                cause,
                target_id=target_id,
                message_by_id=message_by_id,
                messages_by_corr=messages_by_corr,
                event_by_id=event_by_id,
                external_nodes=external_nodes,
            )
            edges.append({"source": source_node_id, "target": _node_id("m", target_id), "via": via})

    if not edges:
        ordered_ids = [
            _opaque_id(message.get("id")) for message in messages if _opaque_id(message.get("id"))
        ]
        ordered_ids.sort()
        for index in range(1, len(ordered_ids)):
            edges.append(
                {
                    "source": _node_id("m", ordered_ids[index - 1]),
                    "target": _node_id("m", ordered_ids[index]),
                    "via": "sequence",
                }
            )

    nodes = _build_causal_nodes(
        messages,
        event_by_id,
        external_nodes,
        edges,
        mermaid_label_fn=mermaid_label_fn,
    )
    mermaid_lines = ["graph TD"]
    for node in nodes:
        mermaid_lines.append(
            f'  {node["node_id"]}["{mermaid_label_fn(node.get("label") or node.get("node_id"))}"]'
        )
    for edge in edges:
        mermaid_lines.append(
            f"  {edge.get('source')} -->|{mermaid_label_fn(edge.get('via') or 'cause')}| {edge.get('target')}"
        )

    return {
        "job_id": str(job_id),
        "nodes": nodes,
        "edges": edges,
        "mermaid": "\n".join(mermaid_lines),
    }


def build_job_causal_chain_builder(
    *,
    serialize_agent_message_row,
    serialize_event_row,
    mermaid_label_fn=mermaid_label,
):
    def _builder(bl, job_id: str) -> dict[str, Any]:
        return build_job_causal_chain(
            bl,
            job_id,
            serialize_agent_message_row=serialize_agent_message_row,
            serialize_event_row=serialize_event_row,
            mermaid_label_fn=mermaid_label_fn,
        )

    return _builder


def _resolve_causal_source(
    cause: dict[str, Any],
    *,
    target_id: str,
    message_by_id: dict[str, dict[str, Any]],
    messages_by_corr: dict[str, list[dict[str, Any]]],
    event_by_id: dict[str, dict[str, Any]],
    external_nodes: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    ref_message_id = _opaque_id(cause.get("message_id"))
    if ref_message_id and ref_message_id in message_by_id:
        return _node_id("m", ref_message_id), "message_id"

    for key_name, via in (
        ("correlation_id", "correlation_id"),
        ("cause_correlation_id", "cause_correlation_id"),
    ):
        corr = str(cause.get(key_name) or "").strip()
        if not corr:
            continue
        candidates = [
            item
            for item in messages_by_corr.get(corr, [])
            if _opaque_id(item.get("id")) != target_id
        ]
        if candidates:
            return _node_id("m", candidates[-1].get("id")), via

    ref_event_id = _opaque_id(cause.get("event_id"))
    if ref_event_id and ref_event_id in event_by_id:
        return _node_id("e", ref_event_id), "event_id"

    raw_key = json.dumps(cause, sort_keys=True, default=str)
    external_id = f"x{abs(hash(raw_key)) % 10_000_000}"
    external_nodes[external_id] = {
        "node_id": external_id,
        "type": "external",
        "label": str(
            cause.get("summary") or cause.get("type") or cause.get("correlation_id") or "external"
        ),
        "raw": cause,
    }
    return external_id, "reference"


def _build_causal_nodes(
    messages: list[dict[str, Any]],
    event_by_id: dict[str, dict[str, Any]],
    external_nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, str]],
    *,
    mermaid_label_fn,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for message in messages:
        message_id = _opaque_id(message.get("id"))
        if not message_id:
            continue
        label = mermaid_label_fn(
            f"#{message_id} {message.get('from_agent') or 'agent'}:{message.get('skill') or ''} [{message.get('status') or ''}]"
        )
        nodes.append(
            {
                "node_id": _node_id("m", message_id),
                "type": "message",
                "label": label,
                "message": message,
            }
        )
    for event_id, event in event_by_id.items():
        if any(edge.get("source") == _node_id("e", event_id) for edge in edges):
            nodes.append(
                {
                    "node_id": _node_id("e", event_id),
                    "type": "event",
                    "label": mermaid_label_fn(
                        f"event:{event.get('event_type') or 'event'}#{event_id}"
                    ),
                    "event": event,
                }
            )
    nodes.extend(external_nodes.values())
    return nodes
