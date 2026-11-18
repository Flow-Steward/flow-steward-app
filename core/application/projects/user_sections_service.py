"""Loading, validating and storing project user sections (ADR 0075).

Sections live in `projects.config.user_sections`. Saving changes that one key of
the stored JSON in the transaction of the project update; reading resolves each
section against the workflow's current canonical contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from core.application.management.errors import ServiceValidationError
from core.application.projects.user_sections import (
    ContractLoader,
    ProjectUserSection,
    ProjectUserSectionView,
    SectionContract,
    parse_sections,
    resolve_sections,
    stored_sections,
    validate_sections,
)


def linked_workflow_ids(bl: Any, project_id: str) -> set[str]:
    """The project's enabled workflow links; empty when the store cannot say."""
    getter = getattr(bl, "get_project_workflows", None)
    if not callable(getter):
        return set()
    try:
        rows = getter(str(project_id), enabled_only=True, include_deleted=False) or []
    except Exception:
        return set()
    return {
        str(row.get("workflow_id") or "").strip()
        for row in rows
        if isinstance(row, dict) and row.get("enabled", True) and not row.get("deleted_at")
    }


def registry_contract_loader(linked: Iterable[str]) -> ContractLoader:
    """Read each workflow once from the registry and project it for sections."""
    linked_ids = set(linked)
    cache: dict[str, SectionContract] = {}

    def load(workflow_id: str) -> SectionContract:
        if workflow_id not in cache:
            cache[workflow_id] = _load_contract(workflow_id, linked=workflow_id in linked_ids)
        return cache[workflow_id]

    return load


def _load_contract(workflow_id: str, *, linked: bool) -> SectionContract:
    from core.application.registry.registry_manager import RegistryManager
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService
    from core.application.registry.workflow_lookup import get_workflow_row
    from core.application.secrets.workflow_gate import overlay_workflow_row

    db = getattr(RegistryManager.instance(), "db_store", None)
    workflow = get_workflow_row(db, workflow_id) if db is not None else None
    if not isinstance(workflow, dict):
        return SectionContract(workflow_id=workflow_id, found=False, linked=linked)
    service = WorkflowInterfaceService()
    interface = service.interface_from_definition(overlay_workflow_row(db, workflow))
    return SectionContract(
        workflow_id=workflow_id,
        name=str(workflow.get("name") or workflow_id),
        revision=int(workflow.get("revision") or 0),
        enabled=bool(workflow.get("enabled")) and not workflow.get("deleted_at"),
        linked=linked,
        ui_channel=bool(interface.enabled and "ui" in interface.trigger_channels),
        input_schema=service.input_json_schema(interface),
        output_schema=service.output_json_schema(interface),
        secret_inputs=[row.name for row in interface.expected_inputs if row.is_secret],
    )


def sections_for_update(raw: Any, *, load_contract: ContractLoader) -> list[ProjectUserSection]:
    """Submitted sections, refused with every issue before anything is written."""
    try:
        sections = parse_sections(raw)
    except ValueError as exc:
        raise ServiceValidationError(f"Invalid user sections: {exc}") from exc
    issues = validate_sections(sections, load_contract=load_contract)
    if issues:
        detail = "; ".join(f"{issue.path}: {issue.message}" for issue in issues[:20])
        raise ServiceValidationError(f"Invalid user sections: {detail}")
    return sections


def save_user_sections(
    conn: Any, *, project_id: str, account_id: str, sections: list[ProjectUserSection]
) -> None:
    """Replace only `config.user_sections` in the caller's transaction."""
    from core.infrastructure.db.project_user_sections_store import save_project_user_sections

    save_project_user_sections(
        conn,
        project_id=str(project_id),
        account_id=str(account_id),
        sections=[section.model_dump(mode="json") for section in sections],
    )


def project_section_views(
    bl: Any,
    project: dict[str, Any],
    *,
    include_hidden: bool,
    allowed_workflow_ids: Iterable[str] = (),
    load_contract: ContractLoader | None = None,
) -> list[ProjectUserSectionView]:
    """The project's sections with their runtime contract, filtered by scope."""
    allowed = set(allowed_workflow_ids)
    sections = [
        section
        for section in stored_sections(project.get("config"))
        if not allowed or section.workflow_id in allowed
    ]
    if not sections:
        return []
    loader = load_contract or registry_contract_loader(
        linked_workflow_ids(bl, str(project.get("id") or ""))
    )
    return resolve_sections(sections, load_contract=loader, include_hidden=include_hidden)


__all__ = [
    "linked_workflow_ids",
    "project_section_views",
    "registry_contract_loader",
    "save_user_sections",
    "sections_for_update",
]
