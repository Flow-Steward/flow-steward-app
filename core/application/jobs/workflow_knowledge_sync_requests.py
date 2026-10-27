"""Application-owned scheduling contract for workflow-knowledge synchronization."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID

WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE = "workflow_knowledge_sync_requested"
WORKFLOW_KNOWLEDGE_CHANGELOG_ENTITY_TYPES = (
    "agent",
    "dataset",
    "skill",
    "step_kind_profile",
    "tool",
    "workflow",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _scope_from_changelog_entry(entry: Mapping[str, Any]) -> dict[str, str]:
    snapshot = _mapping(entry.get("snapshot"))
    scope = _mapping(snapshot.get("scope"))
    return {
        "account_id": _text(
            snapshot.get("account_id") or scope.get("account_id") or DEFAULT_ACCOUNT_ID
        )
        or DEFAULT_ACCOUNT_ID,
        "tenant_id": _text(snapshot.get("tenant_id") or scope.get("tenant_id"))
        or DEFAULT_TENANT_ID,
        "project_id": _text(snapshot.get("project_id") or scope.get("project_id")),
    }


def _revision_component(entry: Mapping[str, Any]) -> dict[str, str]:
    return {
        "created_at": _text(entry.get("created_at")),
        "entity_id": _text(entry.get("entity_id")),
        "entity_type": _text(entry.get("entity_type")),
        "id": _text(entry.get("id")),
    }


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_scope_payloads(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    revisions: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if _text(row.get("entity_type")) not in WORKFLOW_KNOWLEDGE_CHANGELOG_ENTITY_TYPES:
            continue
        scope = _scope_from_changelog_entry(row)
        key = (scope["account_id"], scope["tenant_id"], scope["project_id"])
        revisions[key].append(_revision_component(row))

    payloads: list[dict[str, Any]] = []
    for account_id, tenant_id, project_id in sorted(revisions):
        revision_rows = sorted(
            revisions[(account_id, tenant_id, project_id)],
            key=lambda item: tuple(item.values()),
        )
        payloads.append(
            {
                "account_id": account_id,
                "ensure_indexes": True,
                "include_capability_catalog": True,
                "project_id": project_id,
                "source_revision": _digest(revision_rows),
                "tenant_id": tenant_id,
            }
        )
    return payloads


def enqueue_workflow_knowledge_sync_batch(
    *,
    changelog_store: Any,
    event_store: Any,
    changelog_limit: int,
    cadence_seconds: float,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Persist one bounded, deduplicated sync request for the worker fleet."""

    list_changes = getattr(changelog_store, "list_recent_registry_changelog", None)
    enqueue = getattr(event_store, "enqueue_domain_event", None)
    if not callable(list_changes):
        raise RuntimeError("workflow knowledge changelog repository port is unavailable")
    if not callable(enqueue):
        raise RuntimeError("workflow knowledge dispatch outbox port is unavailable")

    limit = max(1, int(changelog_limit or 1))
    rows = [
        dict(row)
        for row in (
            list_changes(
                entity_types=WORKFLOW_KNOWLEDGE_CHANGELOG_ENTITY_TYPES,
                limit=limit,
            )
            or []
        )
        if isinstance(row, Mapping)
    ]
    scopes = _bounded_scope_payloads(rows)
    clock = now or (lambda: datetime.now(UTC))
    cadence = max(1, int(cadence_seconds or 1))
    cadence_bucket = int(clock().timestamp()) // cadence
    request_payload: dict[str, Any] = {
        "cadence_bucket": cadence_bucket,
        "changelog_limit": limit,
        "scopes": scopes,
    }
    idempotency_key = f"workflow-knowledge-sync:{_digest(request_payload)}"
    request_payload["idempotency_key"] = idempotency_key
    request_payload = dict(sorted(request_payload.items()))
    enqueue(
        event_type=WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
        event_id=idempotency_key,
        aggregate_type="workflow_knowledge_catalog",
        aggregate_id="workflow-knowledge-catalog",
        payload=request_payload,
        severity="info",
        summary="Workflow knowledge synchronization requested",
        correlation_id=idempotency_key,
    )
    return {
        "idempotency_key": idempotency_key,
        "scope_count": len(scopes),
        "status": "queued",
    }


__all__ = [
    "WORKFLOW_KNOWLEDGE_CHANGELOG_ENTITY_TYPES",
    "WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE",
    "enqueue_workflow_knowledge_sync_batch",
]
