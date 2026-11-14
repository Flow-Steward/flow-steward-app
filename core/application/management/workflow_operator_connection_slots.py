"""A project's connection for each slot of an extension workflow (ADR 0068).

Reads and writes need the workflow linked to the project in the caller's
scope; a write also needs write authority, as a hook binding does. Both
delegate to the one slot service the pin at submission uses.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .errors import ServiceNotFoundError


class WorkflowOperatorConnectionSlotsMixin:
    """Mixed into ``WorkflowOperatorService``; uses its scope and registry helpers."""

    _get_logger: Callable[[], Any]

    def _registry_db(self) -> Any:  # pragma: no cover - provided by the service
        raise NotImplementedError

    def _require_project_workflow(
        self,
        project_id: Any,
        workflow_id: str,
        *,
        scope: Any = None,
        allow_disabled: bool = False,
    ) -> str:  # pragma: no cover - provided by the service
        raise NotImplementedError

    def _require_hook_admin(self, scope: Any, action: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _release_workflow(
        self, workflow_id: str, release_id: str
    ) -> Mapping[str, Any] | None:  # pragma: no cover - provided by the hooks mixin
        raise NotImplementedError

    def _slot_service(self, scope: Any):
        from core.application.workflows.workflow_connection_slot_service import (
            WorkflowConnectionSlotService,
        )

        return WorkflowConnectionSlotService(
            self._registry_db(),
            account_id=str(getattr(scope, "account_id", "") or ""),
            actor=str(getattr(scope, "actor_name", "") or ""),
        )

    def _slot_workflow(self, workflow_id: str, scope: Any) -> dict[str, Any]:
        from core.application.registry.workflow_lookup import get_workflow_row

        account_id = str(getattr(scope, "account_id", "") or "")
        kwargs = {"account_id": account_id} if account_id else {}
        row = get_workflow_row(self._registry_db(), workflow_id, **kwargs)
        if not isinstance(row, Mapping):
            raise ServiceNotFoundError(f"Workflow '{workflow_id}' not found")
        return dict(row)

    def _release_slots(self, workflow_id: str, release_id: str) -> list[Any] | None:
        """The slots a pending release declares, so a project can choose before updating."""
        if not release_id:
            return None
        from core.application.workflows.workflow_connection_slots import (
            ConnectionSlotContractError,
            connection_slots_of,
        )

        artifact = self._release_workflow(workflow_id, release_id)
        if artifact is None:
            raise ServiceNotFoundError(
                f"Release '{release_id}' has no workflow for '{workflow_id}'"
            )
        try:
            return list(connection_slots_of(artifact))
        except ConnectionSlotContractError as exc:
            raise ServiceNotFoundError(str(exc)) from exc

    def connection_slots_get(
        self, *, project_id: Any, workflow_id: str, scope: Any
    ) -> dict[str, Any]:
        workflow_id = self._require_project_workflow(
            project_id, workflow_id, scope=scope, allow_disabled=True
        )
        workflow = self._slot_workflow(workflow_id, scope)
        return self._slot_service(scope).describe(workflow, str(project_id))

    def connection_slot_set(
        self,
        *,
        project_id: Any,
        workflow_id: str,
        slot: str,
        connection_id: str | None,
        release_id: str = "",
        scope: Any,
    ) -> dict[str, Any]:
        self._require_hook_admin(scope, "Choosing a workflow's connection")
        workflow_id = self._require_project_workflow(
            project_id, workflow_id, scope=scope, allow_disabled=True
        )
        workflow = self._slot_workflow(workflow_id, scope)
        return self._slot_service(scope).choose(
            workflow,
            str(project_id),
            slot=str(slot or "").strip(),
            connection_id=str(connection_id or "").strip(),
            slots=self._release_slots(workflow_id, str(release_id or "").strip()),
        )


__all__ = ["WorkflowOperatorConnectionSlotsMixin"]
