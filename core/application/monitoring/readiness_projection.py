"""Closed public readiness projection, including typed lifecycle probe fields."""

from __future__ import annotations

import re
from typing import Any

_STALE_CATALOG_MESSAGE = (
    "Workflow knowledge index is stale for the requested account/project scope."
)
# doctor and maintenance use these fields to recognize expected transitions.
# All other readiness details belong to the protected operations endpoints.
_TRANSITION_FIELDS: dict[str, Any] = {
    "lifecycle_recovery": {
        "state": frozenset(
            {"not_configured", "clear", "recovery_required", "mutation_in_progress", "unknown"}
        ),
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
