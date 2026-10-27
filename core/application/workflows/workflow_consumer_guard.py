"""A shared child's save is checked against everyone who calls it (spec §12).

Before a workflow's Interface or definition changes, every active consumer —
direct Invoke steps and project hook bindings — is checked against the new
version: its mappings must still fit, and a wait may not appear under a caller
that runs the child in a parallel lane. A breaking change is refused with the
exact consumer, project and reason; it is never applied silently, and no
consumer is disabled as a workaround. A disabled caller only gets diagnostics.

A joint change of a child and its bindings (one atomic change plan) states
the bindings it applies in the same transaction; the guard checks those
pending mappings instead of the active ones.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator, Mapping
from typing import Any

from core.application.workflows.workflow_where_used import (
    KIND_HOOK,
    WorkflowConsumer,
    workflow_consumers,
)

#: ``(project_id, source_install_id, local_workflow_id, hook_id)`` → pending
#: ``(input_mapping, output_mapping)`` of a change plan being applied.
_PENDING_BINDINGS: contextvars.ContextVar[
    dict[tuple[str, str, str, str], tuple[dict, dict]] | None
] = contextvars.ContextVar("pending_hook_bindings", default=None)


class WorkflowConsumerConflictError(ValueError):
    """The change would break active consumers of the workflow."""

    def __init__(self, workflow_id: str, issues: list[dict[str, Any]]) -> None:
        super().__init__(
            f"Workflow '{workflow_id}' is used by {len(issues)} caller(s) the change would break: "
            + "; ".join(issue["message"] for issue in issues[:5])
        )
        self.workflow_id = workflow_id
        self.issues = issues


@contextlib.contextmanager
def pending_bindings(
    updates: Mapping[tuple[str, str, str, str], tuple[Mapping, Mapping]],
) -> Iterator[None]:
    token = _PENDING_BINDINGS.set({key: (dict(a), dict(b)) for key, (a, b) in updates.items()})
    try:
        yield
    finally:
        _PENDING_BINDINGS.reset(token)


def _value(row: Any, key: str) -> Any:
    return row[key] if isinstance(row, Mapping) else row[0]


def _caller_step(db: Any, consumer: WorkflowConsumer) -> dict[str, Any] | None:
    caller = db.get_workflow(consumer.caller_workflow_id)
    if not isinstance(caller, Mapping):
        return None
    for step in caller.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        if consumer.kind != KIND_HOOK and step.get("step_id") == consumer.step_id:
            return dict(step)
        config = (step.get("override") or {}).get("invoke_workflow") or {}
        hook = config.get("hook") if isinstance(config, Mapping) else None
        if (
            consumer.kind == KIND_HOOK
            and isinstance(hook, Mapping)
            and hook.get("hook_id") == consumer.hook_id
        ):
            return dict(step)
    return None


def _mappings(cur: Any, consumer: WorkflowConsumer, step: Mapping[str, Any] | None):
    if consumer.kind != KIND_HOOK:
        config = ((step or {}).get("override") or {}).get("invoke_workflow") or {}
        return dict(config.get("input_mapping") or {}), dict(config.get("output_mapping") or {})
    key = (
        str(consumer.project_id or ""),
        consumer.source_install_id,
        consumer.local_workflow_id,
        str(consumer.hook_id or ""),
    )
    pending = (_PENDING_BINDINGS.get() or {}).get(key)
    if pending is not None:
        return pending
    cur.execute(
        """SELECT r.input_mapping_jsonb, r.output_mapping_jsonb
             FROM registry_workflow_hook_bindings b
             JOIN registry_workflow_hook_binding_revisions r ON r.id = b.active_revision_id
            WHERE b.project_id::text = %s AND b.source_install_id = %s
              AND b.local_workflow_id = %s AND b.hook_id = %s""",
        key,
    )
    row = cur.fetchone()
    if row is None:
        return {}, {}
    first = row["input_mapping_jsonb"] if isinstance(row, Mapping) else row[0]
    second = row["output_mapping_jsonb"] if isinstance(row, Mapping) else row[1]
    return dict(first or {}), dict(second or {})


def consumer_issues(
    db: Any,
    cur: Any,
    *,
    account_id: Any,
    workflow_id: str,
    settings: Mapping[str, Any],
    definition: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(blockers, diagnostics)`` of saving *workflow_id* with this Interface/definition."""
    from core.application.workflows.workflow_definition_validation import (
        definition_authored_waits,
    )
    from core.application.workflows.workflow_hook_binding_service import mapping_compatibility

    blockers: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    waits = definition_authored_waits(definition) if definition is not None else []
    for consumer in workflow_consumers(cur, account_id=account_id, workflow_id=workflow_id):
        step = _caller_step(db, consumer)
        input_mapping, output_mapping = _mappings(cur, consumer, step)
        reasons = mapping_compatibility(input_mapping, output_mapping, settings)
        if waits and step is not None and step.get("lane_id"):
            reasons.append(
                f"the caller runs it in a parallel lane, where its "
                f"{waits[0]['step_kind']} step '{waits[0]['step_id']}' may not wait"
            )
        for reason in reasons:
            issue = {
                "code": "workflow.consumer.incompatible",
                "message": f"{_label(consumer)}: {reason}",
                "consumer": consumer.dump(),
            }
            (blockers if consumer.caller_enabled else diagnostics).append(issue)
    return blockers, diagnostics


def _label(consumer: WorkflowConsumer) -> str:
    if consumer.kind == KIND_HOOK:
        return (
            f"hook '{consumer.hook_id}' of '{consumer.caller_workflow_id}' "
            f"in project {consumer.project_id}"
        )
    return f"step '{consumer.step_id}' of '{consumer.caller_workflow_id}'"


def assert_consumers_fit(
    db: Any,
    cur: Any,
    *,
    workflow_id: str,
    settings: Mapping[str, Any],
    definition: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Refuse a save that breaks an enabled consumer; return diagnostics otherwise."""
    cur.execute("SELECT account_id FROM registry_workflows WHERE workflow_id = %s", (workflow_id,))
    row = cur.fetchone()
    if row is None:
        return []
    account_id = _value(row, "account_id")
    blockers, diagnostics = consumer_issues(
        db,
        cur,
        account_id=account_id,
        workflow_id=workflow_id,
        settings=settings,
        definition=definition,
    )
    if blockers:
        raise WorkflowConsumerConflictError(workflow_id, blockers)
    return diagnostics


__all__ = [
    "WorkflowConsumerConflictError",
    "assert_consumers_fit",
    "consumer_issues",
    "pending_bindings",
]
