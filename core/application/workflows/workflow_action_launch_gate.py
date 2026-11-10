"""The one launch rule of an installed action implementation (ADR 0065).

A workflow an extension ships as the implementation of an action runs only as
the child of that action's Invoke. Every new job passes ``enqueue_job``; this
gate sits there, so a path that never asks about trigger channels — a test run,
a restart, a webhook or provider trigger created around the checks — still
cannot start one. The Invoke runtime marks its child with the action context it
resolved; nothing else writes that mark.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.application.management.errors import ServiceForbiddenError
from core.application.workflows.workflow_action_contract import (
    COMPOSITE_ACTION_META_KEY,
    is_installed_action_implementation,
)

DIRECT_LAUNCH_CODE = "workflow.action.direct_launch_forbidden"
DIRECT_LAUNCH_MESSAGE = (
    "This workflow implements an extension action and runs only as that action "
    "inside another workflow."
)


class ActionImplementationLaunchError(ServiceForbiddenError):
    """A job of an installed action implementation was not created by its action.

    A service error, so every API that starts a run answers 403 with the code
    instead of failing as an unexpected error.
    """

    code = DIRECT_LAUNCH_CODE

    def __init__(self, workflow_id: str) -> None:
        super().__init__(
            DIRECT_LAUNCH_MESSAGE,
            payload={
                "error": DIRECT_LAUNCH_MESSAGE,
                "code": DIRECT_LAUNCH_CODE,
                "workflow_id": workflow_id,
            },
        )
        self.workflow_id = workflow_id


def _registry_workflow(workflow_id: str, account_id: str) -> Any:
    from core.application.registry.registry_manager import RegistryManager
    from core.application.registry.workflow_lookup import get_workflow_row

    db = getattr(RegistryManager.instance(), "db_store", None)
    if db is None:
        return None
    kwargs = {"account_id": account_id} if account_id else {}
    return get_workflow_row(db, workflow_id, **kwargs)


def is_action_child_launch(meta: Any) -> bool:
    """Whether launch metadata is that of a child the action Invoke created."""
    return (
        isinstance(meta, Mapping)
        and isinstance(meta.get(COMPOSITE_ACTION_META_KEY), Mapping)
        and bool(str(meta.get("invoke_parent_job_id") or "").strip())
    )


def refuse_direct_action_implementation(
    *,
    workflow_id: Any,
    meta: Any,
    account_id: Any = "",
    load_workflow: Callable[[str, str], Any] | None = None,
) -> None:
    """Raise unless *workflow_id* may start with this launch metadata.

    Only projected extension workflows (``extension.<install>.<id>``) are looked
    up; every other launch costs nothing here.
    """
    normalized = str(workflow_id or "").strip()
    if not normalized.startswith("extension.") or is_action_child_launch(meta):
        return
    loader = load_workflow or _registry_workflow
    row = loader(normalized, str(account_id or "").strip())
    if is_installed_action_implementation(row if isinstance(row, Mapping) else None):
        raise ActionImplementationLaunchError(normalized)


__all__ = [
    "DIRECT_LAUNCH_CODE",
    "DIRECT_LAUNCH_MESSAGE",
    "ActionImplementationLaunchError",
    "is_action_child_launch",
    "refuse_direct_action_implementation",
]
