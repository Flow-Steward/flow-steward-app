"""Operator sections (ADR 0075): a published workflow in each project that binds it.

A section is a pair (project, workflow) derived from an active ``project_workflows``
binding of a workflow whose ``settings.operator.published`` is true and whose
callable accepts the ``ui`` channel. Nothing about a section is stored on the
project: the workflow's ``settings.operator`` is the one configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.application.registry.workflow_operator_form import (
    OperatorContract,
    OperatorRuntimeView,
    OperatorSettings,
    operator_contract,
    operator_runtime_view,
    stored_operator_settings,
)


@dataclass(frozen=True)
class OperatorWorkflow:
    """One workflow as the operator surfaces read it."""

    workflow_id: str
    row: dict[str, Any]
    raw_settings: Any
    contract: OperatorContract

    def runtime(self) -> tuple[OperatorSettings | None, OperatorRuntimeView]:
        return operator_runtime_view(self.raw_settings, self.contract)


def operator_workflow_from_row(workflow: dict[str, Any], *, db: Any = None) -> OperatorWorkflow:
    """The contract and operator settings of a registry row."""
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService

    source = workflow
    if db is not None:
        from core.application.secrets.workflow_gate import overlay_workflow_row

        # A secret input bound to a catalog secret is configured (ADR 0057).
        source = overlay_workflow_row(db, workflow)
    interface = WorkflowInterfaceService().interface_from_definition(source)
    workflow_id = str(workflow.get("workflow_id") or "").strip()
    return OperatorWorkflow(
        workflow_id=workflow_id,
        row=dict(workflow),
        raw_settings=stored_operator_settings(workflow),
        contract=operator_contract(
            interface,
            workflow_id=workflow_id,
            name=str(workflow.get("display_name") or workflow.get("name") or workflow_id),
            revision=int(workflow.get("revision") or 0),
            enabled=bool(workflow.get("enabled")) and not workflow.get("deleted_at"),
        ),
    )


def load_operator_workflow(workflow_id: str) -> OperatorWorkflow | None:
    """Read one workflow from the registry; ``None`` when it does not exist."""
    from core.application.registry.registry_manager import RegistryManager
    from core.application.registry.workflow_lookup import get_workflow_row

    db = getattr(RegistryManager.instance(), "db_store", None)
    workflow = get_workflow_row(db, str(workflow_id or "").strip()) if db is not None else None
    if not isinstance(workflow, dict) or workflow.get("deleted_at"):
        return None
    return operator_workflow_from_row(workflow, db=db)


__all__ = [
    "OperatorWorkflow",
    "load_operator_workflow",
    "operator_workflow_from_row",
]
