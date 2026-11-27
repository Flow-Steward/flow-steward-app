"""Operator sections (ADR 0075): a published workflow in each project that binds it.

A section is a pair (project, workflow) derived from an active ``project_workflows``
binding of a workflow whose ``settings.operator.published`` is true and whose
callable accepts the ``ui`` channel. Nothing about a section is stored on the
project: the workflow's ``settings.operator`` is the one configuration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import (
    require_account_access,
    require_project_access,
    require_workflow_access,
    workflow_in_scope_limit,
)
from core.application.management.scope import ScopeContext
from core.application.registry.workflow_operator_form import (
    OperatorRuntimeView,
    operator_runtime_view,
)
from core.application.registry.workflow_operator_form_checks import stored_operator_settings
from core.application.registry.workflow_operator_form_model import (
    OperatorContract,
    OperatorIssue,
    OperatorSettings,
    operator_contract,
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


# --- Menu -----------------------------------------------------------------------


class OperatorSectionView(BaseModel):
    """One menu item: a published workflow in one project the user can see."""

    project_id: str
    project_name: str = ""
    workspace_id: str = ""
    workflow_id: str
    workflow_name: str = ""
    #: ``settings.operator.menu_title`` or the workflow name.
    title: str
    #: Whether the form can submit now; the history is readable either way.
    runnable: bool = False
    #: Why the form is blocked, each with a path into ``settings.operator``.
    blocked_reasons: list[OperatorIssue] = Field(default_factory=list)


class OperatorSectionDetailView(OperatorSectionView):
    """The section page: the menu item plus the resolved form, list and result settings."""

    workflow_revision: int = 0
    effects_acknowledged: bool = False
    operator: OperatorRuntimeView = Field(default_factory=OperatorRuntimeView)


def _blocked_reasons(view: OperatorRuntimeView, contract: OperatorContract) -> list[OperatorIssue]:
    reasons = [issue for issue in view.issues if issue.severity == "error"]
    if not contract.enabled and not any(
        issue.code == "operator.workflow_disabled" for issue in reasons
    ):
        reasons.insert(
            0,
            OperatorIssue(
                code="operator.workflow_disabled",
                message="The workflow is disabled",
                path="settings.operator.published",
            ),
        )
    return reasons


def _shown_in_menu(settings: OperatorSettings | None, contract: OperatorContract) -> bool:
    """Published with the UI channel; a disabled or broken one stays, with its reasons."""
    return bool(settings and settings.published and contract.ui_channel)


def _project_visible(scope: ScopeContext, project: dict[str, Any]) -> bool:
    if scope.workspace_id and str(project.get("workspace_id") or "") != str(scope.workspace_id):
        return False
    try:
        require_project_access(scope, project)
    except ServiceForbiddenError:
        return False
    return True


def _workflow_visible(scope: ScopeContext, workflow: OperatorWorkflow) -> bool:
    try:
        require_workflow_access(scope, workflow.workflow_id)
        if workflow.row.get("account_id") or workflow.row.get("scope_kind") != "system":
            require_account_access(scope, workflow.row)
    except ServiceForbiddenError:
        return False
    return True


WorkflowLoader = Callable[[str], "OperatorWorkflow | None"]


def _section_view(
    binding: dict[str, Any], workflow: OperatorWorkflow, view: OperatorRuntimeView
) -> OperatorSectionView:
    return OperatorSectionView(
        project_id=str(binding.get("project_id") or ""),
        project_name=str(binding.get("project_name") or ""),
        workspace_id=str(binding.get("workspace_id") or ""),
        workflow_id=workflow.workflow_id,
        workflow_name=workflow.contract.name,
        title=view.title,
        runnable=view.runnable,
        blocked_reasons=_blocked_reasons(view, workflow.contract),
    )


def operator_sections(
    bindings: Iterable[dict[str, Any]],
    scope: ScopeContext,
    *,
    load_workflow: WorkflowLoader | None = None,
) -> list[OperatorSectionView]:
    """Menu pairs for this user, from the account's active project bindings.

    A pair appears when the user may see the project and the workflow (including a
    token's ``allowed_project_ids`` / ``allowed_workflow_ids``) and the workflow is
    published with the UI channel. Each workflow is read once.
    """
    loader = load_workflow or load_operator_workflow
    cache: dict[
        str, tuple[OperatorWorkflow, OperatorSettings | None, OperatorRuntimeView] | None
    ] = {}
    sections: list[OperatorSectionView] = []
    for binding in bindings:
        project = {
            "id": binding.get("project_id"),
            "account_id": binding.get("account_id"),
            "workspace_id": binding.get("workspace_id"),
        }
        workflow_id = str(binding.get("workflow_id") or "").strip()
        if not workflow_id or not _project_visible(scope, project):
            continue
        if not workflow_in_scope_limit(scope, binding.get("project_id"), workflow_id):
            continue
        if workflow_id not in cache:
            loaded = loader(workflow_id)
            if loaded is None or not _workflow_visible(scope, loaded):
                cache[workflow_id] = None
            else:
                settings, view = loaded.runtime()
                cache[workflow_id] = (loaded, settings, view)
        entry = cache[workflow_id]
        if entry is None or not _shown_in_menu(entry[1], entry[0].contract):
            continue
        sections.append(_section_view(binding, entry[0], entry[2]))
    return sorted(
        sections, key=lambda item: (item.project_name.lower(), item.project_id, item.title.lower())
    )


def operator_section_detail(
    binding: dict[str, Any],
    scope: ScopeContext,
    *,
    load_workflow: WorkflowLoader | None = None,
) -> OperatorSectionDetailView | None:
    """One section, or ``None`` when it is not in this user's menu."""
    loader = load_workflow or load_operator_workflow
    project = {
        "id": binding.get("project_id"),
        "account_id": binding.get("account_id"),
        "workspace_id": binding.get("workspace_id"),
    }
    workflow_id = str(binding.get("workflow_id") or "").strip()
    if not workflow_id or not _project_visible(scope, project):
        return None
    if not workflow_in_scope_limit(scope, project.get("id"), workflow_id):
        return None
    loaded = loader(workflow_id)
    if loaded is None or not _workflow_visible(scope, loaded):
        return None
    settings, view = loaded.runtime()
    if not _shown_in_menu(settings, loaded.contract):
        return None
    brief = _section_view(binding, loaded, view)
    return OperatorSectionDetailView(
        **brief.model_dump(),
        workflow_revision=loaded.contract.revision,
        effects_acknowledged=view.effects_acknowledged,
        operator=view,
    )


# --- Who may start a run ------------------------------------------------------------


def is_operator_person(scope: ScopeContext) -> bool:
    """A person below admin, in a browser session or with their own token.

    Admins keep the Builder's checks; service accounts keep their scopes; an
    internal call without a principal is nobody's request.
    """
    principal_type = str(scope.principal_type or "").strip().lower()
    if not principal_type or principal_type in {"service_account", "system_actor"}:
        return False
    if str(scope.auth_source or "").strip().lower() == "machine":
        return False
    return not principal_type_has_minimum_role(principal_type, "admin")


def require_operator_can_run(
    scope: ScopeContext,
    *,
    project_id: str,
    workflow_id: str,
    workflow_row: dict[str, Any],
    project_bindings: Iterable[Any],
    db: Any = None,
) -> None:
    """An operator starts only a published, acknowledged workflow bound to the project.

    The same rule holds for the form's launch route and the API/MCP launch route.
    Admins, service accounts and internal calls keep their own checks.
    """
    if not is_operator_person(scope):
        return
    workflow = operator_workflow_from_row({**workflow_row, "workflow_id": workflow_id}, db=db)
    settings, view = workflow.runtime()
    bound = any(
        isinstance(row, dict)
        and str(row.get("workflow_id") or "").strip() == workflow_id
        and row.get("enabled", True)
        and not row.get("deleted_at")
        for row in project_bindings
    )
    reasons: list[OperatorIssue] = []
    if not (settings and settings.published):
        reasons.append(
            OperatorIssue(
                code="operator.not_published",
                message="This workflow is not published to operators",
                path="settings.operator.published",
            )
        )
    if not bound:
        reasons.append(
            OperatorIssue(
                code="operator.not_bound",
                message="This workflow is not linked to the project",
                path="project_workflows",
            )
        )
    if settings and settings.published:
        reasons.extend(_blocked_reasons(view, workflow.contract))
    if reasons:
        raise ServiceForbiddenError(
            reasons[0].message,
            payload={
                "error": reasons[0].message,
                "code": "operator_run_not_allowed",
                "project_id": str(project_id),
                "workflow_id": workflow.workflow_id,
                "reasons": [reason.model_dump(mode="json") for reason in reasons],
            },
        )


__all__ = [
    "OperatorSectionDetailView",
    "OperatorSectionView",
    "OperatorWorkflow",
    "is_operator_person",
    "load_operator_workflow",
    "operator_section_detail",
    "operator_sections",
    "operator_workflow_from_row",
    "require_operator_can_run",
]
