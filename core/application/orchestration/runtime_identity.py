"""Trusted workflow runtime identity extracted at connector execution boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.orchestration.active_step import ActiveStep, active_step

_IDENTITY_FIELDS = (
    "job_id",
    "workflow_id",
    "account_id",
    "tenant_id",
    "project_id",
    "workspace_id",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_value(field: str, sources: tuple[Mapping[str, Any], ...]) -> Any:
    for source in sources:
        raw = source.get(field)
        if _text(raw):
            return raw if field == "job_id" else _text(raw)
    return ""


def _loop_identity(state: Mapping[str, Any], current: ActiveStep | None) -> dict[str, Any]:
    """The For each item the active step runs for.

    The innermost For each frame of the cursor names the container and the item
    position; outside any For each there is no identity. The item position, not
    the execution id, keys effect deduplication, as before (ADR 0038).
    """
    del state
    frame = current.for_each_frame if current is not None else None
    if frame is None:
        return {}
    return {"for_each_id": frame.for_each_id, "index": frame.item_index}


@dataclass(frozen=True)
class WorkflowRuntimeIdentity:
    job_id: Any = ""
    workflow_id: str = ""
    account_id: str = ""
    tenant_id: str = ""
    project_id: str = ""
    workspace_id: str = ""
    step_id: str = ""
    step_execution_id: str = ""
    attempt: int | None = None
    group_id: str = ""
    for_each_id: str = ""
    loop: Mapping[str, Any] | None = None
    test_mode: bool = False

    def as_context(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workflow_id": self.workflow_id,
            "account_id": self.account_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "step_id": self.step_id,
            "step_execution_id": self.step_execution_id,
            "attempt": self.attempt,
            # The immediate owner: at most one is set, both empty at the root.
            "group_id": self.group_id,
            "for_each_id": self.for_each_id,
            "loop": dict(self.loop or {}),
            "test_mode": self.test_mode,
        }

    def missing(self, *fields: str) -> tuple[str, ...]:
        return tuple(field for field in fields if not _text(getattr(self, field, "")))


def runtime_identity_from_state(state: Mapping[str, Any]) -> WorkflowRuntimeIdentity:
    """Resolve trusted queue/checkpoint identity without consulting connector input."""
    trusted = _mapping(state.get("trusted_runtime_context")) or _mapping(
        state.get("_trusted_runtime_context")
    )
    project_config = _mapping(state.get("project_config"))
    job_meta = _mapping(state.get("job_meta"))
    meta = _mapping(state.get("meta"))
    task_envelope = _mapping(state.get("task_envelope"))
    # The worker builds ``trusted`` and ``project_config`` from durable job/project
    # rows. Top-level state remains a compatibility source for older checkpoints.
    sources = (trusted, project_config, job_meta, meta, task_envelope, state)
    values = {field: _first_value(field, sources) for field in _IDENTITY_FIELDS}
    current = active_step(state)

    try:
        from core.infrastructure.runtime.test_mode import parse_test_mode
    except Exception:  # pragma: no cover - lightweight import fallback
        test_mode = trusted.get("test_mode") is True
    else:
        test_mode = parse_test_mode(trusted.get("test_mode"), False)

    return WorkflowRuntimeIdentity(
        **values,
        step_id=current.step_id if current is not None else "",
        step_execution_id=current.step_execution_id if current is not None else "",
        attempt=current.attempt if current is not None else None,
        group_id=(current.group_id or "") if current is not None else "",
        for_each_id=(current.for_each_id or "") if current is not None else "",
        loop=_loop_identity(state, current),
        test_mode=test_mode,
    )
