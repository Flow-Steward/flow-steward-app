"""Closed projections for readiness probes and operator diagnostics."""

from __future__ import annotations

import re
from typing import Any

from core.infrastructure.config.memory_config import (
    SUPPORTED_CONTEXT_GRAPH_BACKENDS,
    SUPPORTED_KNOWLEDGE_BACKENDS,
    SUPPORTED_MEMORY_PROFILES,
    SUPPORTED_REPOSITORY_BACKENDS,
    SUPPORTED_TEXT_SEARCH_BACKENDS,
    SUPPORTED_VECTOR_BACKENDS,
)

_STALE_CATALOG_MESSAGE = (
    "Workflow knowledge index is stale for the requested account/project scope."
)
# doctor and maintenance use these fields to recognize expected transitions.
# The protected operations endpoints have their own diagnostic allowlist.
_TRANSITION_FIELDS: dict[str, Any] = {
    "lifecycle_recovery": {
        "state": frozenset(
            {"not_configured", "clear", "recovery_required", "mutation_in_progress", "unknown"}
        ),
        "recovery_required_scope": frozenset({"none", "release_upgrade", "other"}),
    },
    "stuck_jobs": {
        "persisted_count": int,
        "detection_count": int,
        "max_allowed": int,
        "error": frozenset({""}),
    },
    "memory_backends": {
        "error": frozenset({_STALE_CATALOG_MESSAGE}),
        "workflow_knowledge": {
            "error": frozenset({_STALE_CATALOG_MESSAGE}),
            "graph": {"available": bool},
            "vector": {"available": bool},
            "scope": {
                "freshness": frozenset({"fresh", "stale", "empty", "unknown"}),
                "empty": bool,
                "digest_fresh": bool,
                "catalog_digest": re.compile(r"[a-f0-9]{64}"),
                "stored_digest": re.compile(r"[a-f0-9]{64}"),
            },
        },
    },
    "workflow_knowledge_sync": {
        "last_failure_reason": frozenset({""}),
        "consecutive_failures": int,
        "detail": re.compile(r"workflow knowledge sync is stale by [0-9]+s \(limit [0-9]+s\)"),
    },
}

_SCHEMA_INCOMPLETE = "canonical workflow knowledge schema is incomplete"
_STORAGE_UNAVAILABLE = "Workflow knowledge storage status is unavailable"
_BACKEND_STATUS_FIELDS = {
    "available": bool,
    "extension_available": bool,
    "extension_installed": bool,
}
_WORKFLOW_BACKEND_FIELDS = {
    "graph": {
        **_BACKEND_STATUS_FIELDS,
        "labels_ready": bool,
        "error": frozenset(
            {
                _SCHEMA_INCOMPLETE,
                "Apache AGE extension is not available in this Postgres image.",
                "Apache AGE extension is not installed in this database.",
                "Workflow knowledge graph labels are not ready.",
            }
        ),
    },
    "vector": {
        **_BACKEND_STATUS_FIELDS,
        "column_available": bool,
        "error": frozenset(
            {
                _SCHEMA_INCOMPLETE,
                "pgvector extension is not available in this Postgres image.",
                "pgvector extension is not installed in this database.",
                "Workflow knowledge embedding column is missing from "
                "platform_workflow_knowledge_documents.",
            }
        ),
    },
}
_LOOP_FIELDS = {
    "enabled": bool,
    "never_completed": bool,
    "consecutive_failures": int,
    "age_seconds": int,
    "max_age_seconds": int,
}
_QUEUE_DELIVERY_DETAILS = frozenset(
    {
        "Queue delivery observability could not be loaded.",
        "No queued delivery work is waiting for this scope right now.",
        "Jobs are queued normally and waiting for worker pickup.",
        "RabbitMQ delivery is retrying before execution has safely started.",
        "No active delivery backlog is waiting right now, but one or more "
        "dead-lettered jobs still require operator intervention.",
        "No active delivery backlog is waiting right now, but one or more "
        "jobs previously failed after delivery succeeded.",
    }
)
_OPERATIONS_FIELDS: dict[str, dict[str, Any]] = {
    "rabbitmq": {
        "state": frozenset({"broker_available", "broker_unavailable"}),
        "status_label": frozenset({"Broker available", "Broker unavailable"}),
        "status_message": frozenset(
            {"RabbitMQ transport is available.", "RabbitMQ transport is unavailable."}
        ),
    },
    "memory_backends": {
        "error": _TRANSITION_FIELDS["memory_backends"]["error"],
        "matrix": {
            "profile": SUPPORTED_MEMORY_PROFILES,
            "repository": SUPPORTED_REPOSITORY_BACKENDS,
            "knowledge": SUPPORTED_KNOWLEDGE_BACKENDS,
            "text_search": SUPPORTED_TEXT_SEARCH_BACKENDS,
            "vector": SUPPORTED_VECTOR_BACKENDS,
            "context_graph": SUPPORTED_CONTEXT_GRAPH_BACKENDS,
        },
        "workflow_knowledge": {
            **_TRANSITION_FIELDS["memory_backends"]["workflow_knowledge"],
            **_WORKFLOW_BACKEND_FIELDS,
            "checked": bool,
            "required": bool,
            "ok": bool,
            "schema": {"available": bool},
        },
    },
    "worker_manager": {
        "state": frozenset({"active", "lifecycle_paused"}),
        "required": bool,
        "worker_count": int,
        "detail": frozenset(
            {
                "Worker consumption is intentionally lifecycle-paused.",
                "Live ready-queue consumer count is unavailable.",
            }
        ),
    },
    "security_posture": {
        **_LOOP_FIELDS,
        "detail": frozenset(
            {
                "security posture scanning is disabled",
                "security posture loop status repository port is unavailable",
                "security posture loop status could not be read",
                "the latest security posture iteration failed",
                "security posture history could not be read",
                "security posture history returned an unreadable timestamp",
                "security posture loop returned an unreadable success timestamp",
                "security posture scanning has not attempted an iteration; check the "
                "control-plane error log and that SECURITY_PII_SALT is configured",
                "security posture scanning has never completed a successful iteration; "
                "check that SECURITY_PII_SALT reaches the control-plane service",
                "security posture iteration completed successfully; no project snapshots "
                "were produced",
            }
        ),
    },
    "workflow_knowledge_sync": {
        **_LOOP_FIELDS,
        "in_progress": bool,
        "detail": frozenset(
            {
                "workflow knowledge sync is disabled",
                "workflow knowledge sync loop status repository port is unavailable",
                "workflow knowledge sync loop status could not be read",
                "workflow knowledge sync has not attempted an iteration",
                "the latest workflow knowledge sync iteration failed",
                "workflow knowledge sync returned an unreadable attempt timestamp",
                "workflow knowledge sync returned an unreadable success timestamp",
                "workflow knowledge sync claim status could not be read",
                "workflow knowledge sync iteration is in progress",
                "the latest workflow knowledge sync iteration did not complete",
                "workflow knowledge sync is current",
            }
        ),
    },
    "queue_lag": {"pending_count": int, "max_pending": int},
    "queue_delivery": {
        "state": frozenset({"idle", "queued_pending", "retrying_delivery", "unknown"}),
        "state_label": frozenset(
            {"Idle", "Normal queued / pending", "Retrying delivery", "Queue delivery unavailable"}
        ),
        "attention_state": frozenset({"idle", "dead_lettered", "execution_failed_after_delivery"}),
        "queued_pending_jobs": int,
        "retrying_delivery_jobs": int,
        "dead_lettered_jobs": int,
        "post_claim_execution_failed_jobs": int,
        "requires_intervention": bool,
        "detail": _QUEUE_DELIVERY_DETAILS,
    },
    "lease_health": {"stale_leases": int, "max_stale_leases": int, "stale_threshold_seconds": int},
    "stuck_jobs": {
        "persisted_count": int,
        "detection_count": int,
        "max_allowed": int,
        "detail": re.compile(
            r"[0-9]{1,19} persisted stuck job\(s\); "
            r"[0-9]{1,19} additional watchdog detection\(s\); maximum allowed [0-9]{1,19}\."
        ),
    },
    "worker_identity": {
        "used_recently": bool,
        "window_minutes": int,
        "detail": frozenset(
            {
                "Could not determine which worker credential is in use.",
                "No flow-steward-worker credential has ever authenticated; the worker "
                "is not reaching the platform with a machine token.",
            }
        ),
    },
    "lifecycle_recovery": {
        **_TRANSITION_FIELDS["lifecycle_recovery"],
        "unfinished_count": int,
        "recovery_required_count": int,
        "error": frozenset({"Lifecycle recovery state is unreadable."}),
    },
    "graph_catalog": {
        "detail": frozenset({"The graph extension is not installed."}),
    },
}
_OPERATIONS_FAILURE_MESSAGES = {
    "postgres": "PostgreSQL readiness check failed.",
    "guardrail_ownership_schema": "Guardrail ownership schema is unavailable or requires migration.",
    "rabbitmq": "RabbitMQ transport is unavailable.",
    "memory_backends": "Memory backend readiness check failed.",
    "worker_manager": "Worker availability check failed.",
    "security_posture": "Security posture readiness check failed.",
    "workflow_knowledge_sync": "Workflow knowledge sync readiness check failed.",
    "queue_lag": "Pending jobs exceed the readiness limit or could not be counted.",
    "queue_delivery": "Queue delivery observability could not be loaded.",
    "lease_health": "Stale leases exceed the readiness limit or could not be counted.",
    "stuck_jobs": "Stuck jobs exceed the readiness limit or could not be counted.",
    "worker_identity": "Worker identity is not ready. Check its machine token configuration.",
    "lifecycle_recovery": "A lifecycle mutation is incomplete.",
    "graph_catalog": "Graph catalogue identity check failed. Check the database restore status.",
}
_GRAPH_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,62}")


def _project_fields(source: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, rule in fields.items():
        value = source.get(name)
        if isinstance(rule, dict):
            if isinstance(value, dict):
                nested = _project_fields(value, rule)
                if nested:
                    result[name] = nested
        elif rule is int or rule is bool:
            # In particular, do not turn "false", "0", or bool counts into valid probes.
            if type(value) is rule:
                result[name] = value
        elif isinstance(rule, frozenset):
            if isinstance(value, str):
                for allowed in rule:
                    if value == allowed:
                        result[name] = allowed
                        break
        elif isinstance(rule, re.Pattern) and isinstance(value, str) and rule.fullmatch(value):
            result[name] = value
    return result


def public_readiness_checks(checks: Any) -> dict[str, dict[str, Any]]:
    """Expose states and closed transition evidence, never arbitrary diagnostics."""
    if not isinstance(checks, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, raw in checks.items():
        check = raw if isinstance(raw, dict) else {}
        result[name] = {
            "ok": check.get("ok") is True,
            **_project_fields(check, _TRANSITION_FIELDS.get(name, {})),
        }
    return result


def operations_readiness_checks(checks: Any) -> dict[str, dict[str, Any]]:
    """Preserve actionable check state without forwarding runtime error text."""
    if not isinstance(checks, dict):
        return {}
    result = {}
    for name, failure_message in _OPERATIONS_FAILURE_MESSAGES.items():
        if name not in checks:
            continue
        raw = checks[name]
        check = raw if isinstance(raw, dict) else {}
        ok = check.get("ok") is True
        projected = {"ok": ok, **_project_fields(check, _OPERATIONS_FIELDS.get(name, {}))}
        if not ok and not (projected.get("error") or projected.get("detail")):
            projected["error"] = failure_message
        result[name] = projected
    return result


def workflow_knowledge_storage_status(source: dict[str, Any]) -> dict[str, Any]:
    """Expose workflow knowledge availability and known backend failure reasons."""
    projected = _project_fields(source, {"graph_name": _GRAPH_NAME, **_WORKFLOW_BACKEND_FIELDS})
    for name in _WORKFLOW_BACKEND_FIELDS:
        backend = projected.setdefault(name, {})
        backend.setdefault("available", False)
        backend.setdefault("error", "" if backend["available"] else _STORAGE_UNAVAILABLE)
    return {
        "available": source.get("ok") is True,
        "graph_name": "",
        **projected,
    }
