"""Bounded, authorized execution identities of a job's immediate children."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from core.application.management.errors import ServiceForbiddenError, ServiceNotFoundError
from core.application.management.guards import require_workflow_access
from core.application.management.scope import ScopeContext

_MAX_CHILD_INVOCATIONS = 100
_INVOCATION_FIELDS = (
    "step_id",
    "step_execution_id",
    "invocation_key",
    "child_job_id",
    "child_workflow_id",
)


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def project_child_invocations(
    parent: dict[str, Any],
    *,
    scope: ScopeContext,
    read_child: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Expose only linked children the caller can read, never recurse or infer revisions."""
    parent_id = str(parent.get("id") or "")
    parent_workflow = str(parent.get("selected_workflow") or "")
    if parent_workflow:
        require_workflow_access(scope, parent_workflow)
    links = _object(_object(parent.get("meta")).get("invoke_children"))
    projected: list[dict[str, str]] = []
    for index, (child_id, raw) in enumerate(links.items()):
        if index >= _MAX_CHILD_INVOCATIONS:
            break
        entry = _object(raw)
        row = {key: str(entry.get(key) or "").strip() for key in _INVOCATION_FIELDS}
        if not all(row.values()) or row["child_job_id"] != str(child_id):
            continue
        try:
            child = read_child(row["child_job_id"])
            require_workflow_access(scope, row["child_workflow_id"])
        except (ServiceForbiddenError, ServiceNotFoundError):
            continue
        child_meta = _object(child.get("meta"))
        if (
            str(child.get("selected_workflow") or "") != row["child_workflow_id"]
            or str(child_meta.get("invoke_parent_job_id") or "") != parent_id
            or str(child_meta.get("invoke_parent_step_id") or "") != row["step_id"]
        ):
            continue
        projected.append(row)
    return {"invocations": projected, "truncated": len(links) > _MAX_CHILD_INVOCATIONS}
