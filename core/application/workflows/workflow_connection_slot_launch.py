"""Pin a slot workflow's connections into every new job (ADR 0068).

Every new job passes ``enqueue_job``; a run from the UI, a webhook, a schedule,
a provider trigger, a restart or an Invoke child all reach this function there.
The connections are pinned in the job's own ``meta`` before it is created, so a
run keeps the connection it started with, and a workflow that is not ready is
refused before anything starts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.workflows.workflow_action_contract import COMPOSITE_ACTION_META_KEY
from core.application.workflows.workflow_connection_slots import (
    CONNECTION_SLOTS_META_KEY,
    installed_connection_slots,
    job_connection_context,
)

WorkflowLoader = Callable[[str, str], Any]
JobLoader = Callable[[str], Any]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _registry_db() -> Any:
    from core.application.registry.registry_manager import RegistryManager

    return getattr(RegistryManager.instance(), "db_store", None)


def _registry_workflow(workflow_id: str, account_id: str) -> Any:
    from core.application.registry.workflow_lookup import get_workflow_row

    db = _registry_db()
    if db is None:
        return None
    kwargs = {"account_id": account_id} if account_id else {}
    return get_workflow_row(db, workflow_id, **kwargs)


def memoized_workflow_loader(load: WorkflowLoader | None = None) -> WorkflowLoader:
    """One registry read per workflow for every launch check of one submission."""
    loader = load or _registry_workflow
    cache: dict[tuple[str, str], Any] = {}

    def load_once(workflow_id: str, account_id: str) -> Any:
        key = (workflow_id, account_id)
        if key not in cache:
            cache[key] = loader(workflow_id, account_id)
        return cache[key]

    return load_once


def _inherited_connections(
    meta: Mapping[str, Any], *, project_id: str, load_job: JobLoader | None
) -> dict[str, str]:
    """The slot connections of the parent run, for a child of an Invoke in the same project."""
    parent_id = _text(meta.get("invoke_parent_job_id"))
    if not parent_id or load_job is None:
        return {}
    try:
        parent = load_job(parent_id)
    except Exception:
        return {}
    if not isinstance(parent, Mapping) or _text(parent.get("project_id")) != project_id:
        return {}
    context = job_connection_context(parent.get("meta"))
    return {
        _text(key): _text(value)
        for key, value in dict(context.get("connections") or {}).items()
        if _text(key) and _text(value)
    }


def pin_workflow_connection_slots(
    *,
    workflow_id: Any,
    meta: dict[str, Any],
    project_id: Any,
    account_id: Any,
    load_workflow: WorkflowLoader | None = None,
    load_job: JobLoader | None = None,
    service_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Write the pinned slot context into *meta* (returned) or raise when not ready.

    A value a caller put under the pin's key is always dropped first: only the
    host decides which connection a run reads. An action child keeps the action
    context its Invoke wrote (ADR 0065) and gets nothing here. Only projected
    extension workflows (``extension.<install>.<id>``) are looked up.
    """
    meta.pop(CONNECTION_SLOTS_META_KEY, None)
    normalized = _text(workflow_id)
    if not normalized.startswith("extension.") or isinstance(
        meta.get(COMPOSITE_ACTION_META_KEY), Mapping
    ):
        return meta
    account = _text(account_id) or DEFAULT_ACCOUNT_ID
    project = _text(project_id)
    row = (load_workflow or _registry_workflow)(normalized, account)
    if not isinstance(row, Mapping) or not installed_connection_slots(row):
        return meta
    if service_factory is None:
        from core.application.workflows.workflow_connection_slot_service import (
            WorkflowConnectionSlotService,
        )

        def service_factory(account: str) -> Any:
            return WorkflowConnectionSlotService(_registry_db(), account_id=account)

    context = service_factory(account).pin(
        row,
        project,
        preferred=_inherited_connections(meta, project_id=project, load_job=load_job),
    )
    if context is not None:
        meta[CONNECTION_SLOTS_META_KEY] = {**context, "workflow_id": normalized}
    return meta


__all__ = ["memoized_workflow_loader", "pin_workflow_connection_slots"]
