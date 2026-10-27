"""Domain-event helper functions extracted from serve.py."""

from __future__ import annotations

from datetime import date
from typing import Any

from core.application.observability_vocabulary import event_status_meta, event_type_meta


def serialize_domain_event_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    text = str(value or "").strip()
    return text or None


def parse_iso_date_param(raw: str) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    return date.fromisoformat(text)


def event_project_name(bl: Any, project_id: Any, cache: dict[str, str]) -> str:
    pid = str(project_id or "").strip()
    if not pid:
        return ""
    if pid in cache:
        return cache[pid]
    if not hasattr(bl, "get_project"):
        cache[pid] = ""
        return ""
    try:
        project = bl.get_project(pid) or {}
    except Exception:
        project = {}
    name = str((project or {}).get("name") or "").strip()
    cache[pid] = name
    return name


def project_account_id(bl: Any, project_id: Any) -> str | None:
    pid = str(project_id or "").strip()
    if not pid or not hasattr(bl, "get_project"):
        return None
    try:
        project = bl.get_project(pid) or {}
    except Exception:
        project = {}
    return str((project or {}).get("account_id") or "").strip() or None


def event_row_matches_scope(
    row: dict | None,
    *,
    default_account_id: str,
    account_id: str | None = None,
    allowed_project_ids: frozenset[str] | None = None,
    allowed_workflow_ids: frozenset[str] | None = None,
) -> bool:
    payload = dict(row or {})
    normalized_account_id = str(account_id or "").strip()
    row_account_id = (
        str(payload.get("account_id") or default_account_id).strip() or default_account_id
    )
    if normalized_account_id and row_account_id != normalized_account_id:
        return False
    if allowed_project_ids:
        project_id = str(payload.get("project_id") or "").strip()
        if project_id and project_id not in allowed_project_ids:
            return False
    if allowed_workflow_ids:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if workflow_id and workflow_id not in allowed_workflow_ids:
            return False
    return True


def resolve_domain_event_by_ref(
    bl: Any,
    event_ref: str,
    *,
    default_account_id: str,
    account_id: str | None = None,
    allowed_project_ids: frozenset[str] | None = None,
    allowed_workflow_ids: frozenset[str] | None = None,
) -> dict | None:
    normalized_ref = str(event_ref or "").strip()
    if not normalized_ref:
        return None
    event_row = None
    if hasattr(bl, "get_domain_event"):
        try:
            event_row = bl.get_domain_event(normalized_ref, account_id=account_id)
        except Exception:
            event_row = None
    if event_row and event_row_matches_scope(
        event_row,
        default_account_id=default_account_id,
        account_id=account_id,
        allowed_project_ids=allowed_project_ids,
        allowed_workflow_ids=allowed_workflow_ids,
    ):
        return dict(event_row)
    if normalized_ref and hasattr(bl, "list_domain_events"):
        try:
            rows = bl.list_domain_events(account_id=account_id, limit=2000) or []
        except Exception:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not event_row_matches_scope(
                row,
                default_account_id=default_account_id,
                account_id=account_id,
                allowed_project_ids=allowed_project_ids,
                allowed_workflow_ids=allowed_workflow_ids,
            ):
                continue
            if str(row.get("id") or "").strip() == normalized_ref:
                return dict(row)
    return None


def serialize_domain_event_summary(
    row: dict,
    *,
    project_name: str = "",
    to_json_obj: Any,
) -> dict:
    payload = to_json_obj(row.get("payload")) if isinstance(row, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    detail_text = str(
        payload.get("detail") or payload.get("message") or payload.get("reason") or ""
    ).strip()
    event_id = str(row.get("event_id") or "").strip()
    event_type = str(row.get("event_type") or "").strip().lower()
    status_meta = event_status_meta(row.get("status") or "recorded")
    type_meta = event_type_meta(event_type)
    return {
        "id": row.get("id"),
        "event_id": event_id,
        "event_type": event_type,
        "event_type_label": type_meta["event_type_label"],
        "event_family": type_meta["event_family"],
        "event_family_label": type_meta["event_family_label"],
        "event_category": type_meta["event_category"],
        "event_category_label": type_meta["event_category_label"],
        "severity": str(row.get("severity") or "info").strip().lower() or "info",
        "status": status_meta["code"],
        "status_label": status_meta["label"],
        "status_tone": status_meta["tone"],
        "title": str(
            row.get("summary") or type_meta["event_type_label"] or row.get("event_type") or event_id
        ).strip(),
        "summary": str(row.get("summary") or "").strip(),
        "detail": detail_text,
        "project_id": row.get("project_id"),
        "project_name": project_name or None,
        "workflow_id": row.get("workflow_id"),
        "job_id": row.get("job_id"),
        "group_id": row.get("group_id"),
        "step_id": row.get("step_id"),
        "step_execution_id": str(row.get("step_execution_id") or "").strip() or None,
        "created_at": serialize_domain_event_datetime(
            row.get("occurred_at") or row.get("created_at")
        ),
        "updated_at": serialize_domain_event_datetime(row.get("updated_at")),
    }


__all__ = [
    "event_project_name",
    "event_row_matches_scope",
    "parse_iso_date_param",
    "project_account_id",
    "resolve_domain_event_by_ref",
    "serialize_domain_event_datetime",
    "serialize_domain_event_summary",
]
