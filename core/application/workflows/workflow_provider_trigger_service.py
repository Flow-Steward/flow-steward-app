"""Application commands for workflow provider-trigger mutations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.application.management.errors import ServiceNotFoundError
from core.application.workflows.workflow_trigger_guards import workflow_trigger_transaction


class WorkflowProviderTriggerError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class WorkflowProviderTriggerCreateCommand:
    workflow_id: str
    trigger_id: str
    project_id: str | None
    provider_key: str
    trigger_type_id: str
    display_name: str
    enabled: bool
    trigger_config: dict[str, Any]


@dataclass(frozen=True)
class WorkflowProviderTriggerUpdateCommand:
    workflow_id: str
    trigger_id: str
    project_id: str | None
    provider_key: str | None
    trigger_type_id: str | None
    display_name: str | None
    enabled: bool | None
    trigger_config: dict[str, Any]


class WorkflowProviderTriggerService:
    def __init__(
        self,
        *,
        find_definition: Callable[..., dict[str, Any] | None],
        validate_config: Callable[..., tuple[str, list[Any], str]],
        create_trigger: Callable[..., dict[str, Any]],
        update_trigger: Callable[..., dict[str, Any]],
        delete_trigger: Callable[..., bool],
        get_trigger: Callable[..., dict[str, Any] | None],
        resolve_project_binding: Callable[..., tuple[str | None, dict[str, Any]]],
        load_workflow: Callable[[str], dict[str, Any]],
        is_scope_conflict: Callable[[Exception], bool],
    ) -> None:
        self._find_definition = find_definition
        self._validate_config = validate_config
        self._create_trigger = create_trigger
        self._update_trigger = update_trigger
        self._delete_trigger = delete_trigger
        self._get_trigger = get_trigger
        self._resolve_project_binding = resolve_project_binding
        self._load_workflow = load_workflow
        self._is_scope_conflict = is_scope_conflict

    @staticmethod
    def _definition_or_raise(definition: dict[str, Any] | None) -> dict[str, Any]:
        if not definition:
            raise WorkflowProviderTriggerError(
                "provider_trigger_definition_not_found",
                "Provider trigger definition not found",
                status_code=404,
            )
        return definition

    @staticmethod
    def _project_or_raise(project_id: str | None) -> str:
        normalized = str(project_id or "").strip()
        if not normalized:
            raise WorkflowProviderTriggerError(
                "workflow_project_not_linked",
                "Workflow is not linked to the selected project",
                status_code=404,
            )
        return normalized

    @staticmethod
    def _project_account_or_raise(bl: Any, project_id: str) -> str:
        getter = getattr(bl, "get_project", None)
        project = getter(project_id) if callable(getter) else None
        account_id = str((project or {}).get("account_id") or "").strip()
        if not account_id:
            raise WorkflowProviderTriggerError(
                "workflow_project_not_linked",
                "Selected project tenant could not be resolved",
                status_code=404,
            )
        return account_id

    def _validated_fields(
        self,
        *,
        definition: dict[str, Any],
        config: dict[str, Any],
        enabled: bool,
    ) -> tuple[str, list[Any], str]:
        readiness, errors, event_name = self._validate_config(
            definition,
            config,
            enabled=enabled,
        )
        if enabled and errors:
            raise WorkflowProviderTriggerError(
                "provider_trigger_config_incomplete",
                "Provider trigger config is incomplete",
                status_code=400,
            )
        return readiness, errors, event_name

    def create(
        self,
        *,
        bl: Any,
        command: WorkflowProviderTriggerCreateCommand,
        authorize_workflow: Callable[[str], None],
        authorize_project: Callable[[str], None],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._load_workflow(command.workflow_id)
        authorize_workflow(command.workflow_id)
        project_id, _ = self._resolve_project_binding(
            bl,
            workflow_id=command.workflow_id,
            explicit_project_id=command.project_id,
        )
        project_id = self._project_or_raise(project_id)
        authorize_project(project_id)
        account_id = self._project_account_or_raise(bl, project_id)
        definition = self._definition_or_raise(
            self._find_definition(
                bl,
                provider_key=command.provider_key.strip().lower(),
                trigger_type_id=command.trigger_type_id,
            )
        )
        readiness, errors, event_name = self._validated_fields(
            definition=definition,
            config=command.trigger_config,
            enabled=command.enabled,
        )
        try:
            with workflow_trigger_transaction(bl, command.workflow_id, account_id=account_id):
                created = self._create_trigger(
                    bl,
                    trigger_id=command.trigger_id,
                    workflow_id=command.workflow_id,
                    project_id=project_id,
                    provider_key=str(definition.get("provider_key") or "").strip().lower(),
                    trigger_type_id=str(definition.get("trigger_type_id") or "").strip(),
                    display_name=str(
                        command.display_name or definition.get("display_name") or ""
                    ).strip(),
                    enabled=command.enabled,
                    event_name=event_name,
                    trigger_config_jsonb=dict(command.trigger_config),
                    readiness_status=readiness,
                    validation_errors_jsonb=errors,
                    account_id=account_id,
                )
        except ServiceNotFoundError as exc:
            raise WorkflowProviderTriggerError(
                "workflow_not_found", "Workflow not found", status_code=404
            ) from exc
        except Exception as exc:
            if self._is_scope_conflict(exc):
                raise WorkflowProviderTriggerError(
                    "provider_trigger_conflict",
                    "Provider trigger already exists for this workflow, project, and event binding",
                    status_code=409,
                ) from exc
            raise
        return created, definition

    def update(
        self,
        *,
        bl: Any,
        command: WorkflowProviderTriggerUpdateCommand,
        authorize_workflow: Callable[[str], None],
        authorize_project: Callable[[str], None],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._load_workflow(command.workflow_id)
        authorize_workflow(command.workflow_id)
        existing = self._get_trigger(bl, trigger_id=command.trigger_id)
        if not existing or str(existing.get("workflow_id") or "").strip() != command.workflow_id:
            raise WorkflowProviderTriggerError(
                "provider_trigger_not_found", "Provider trigger not found", status_code=404
            )
        authorize_project(str(existing.get("project_id") or "").strip())
        project_id, _ = self._resolve_project_binding(
            bl,
            workflow_id=command.workflow_id,
            explicit_project_id=str(command.project_id or existing.get("project_id") or "").strip()
            or None,
        )
        project_id = self._project_or_raise(project_id)
        authorize_project(project_id)
        account_id = self._project_account_or_raise(bl, project_id)
        definition = self._definition_or_raise(
            self._find_definition(
                bl,
                provider_key=str(command.provider_key or existing.get("provider_key") or "")
                .strip()
                .lower(),
                trigger_type_id=str(
                    command.trigger_type_id or existing.get("trigger_type_id") or ""
                ).strip(),
            )
        )
        config = dict(existing.get("trigger_config_jsonb") or {})
        config.update(command.trigger_config)
        enabled = bool(
            existing.get("enabled", True) if command.enabled is None else command.enabled
        )
        readiness, errors, event_name = self._validated_fields(
            definition=definition,
            config=config,
            enabled=enabled,
        )
        try:
            with workflow_trigger_transaction(bl, command.workflow_id, account_id=account_id):
                updated = self._update_trigger(
                    bl,
                    trigger_id=command.trigger_id,
                    project_id=project_id,
                    provider_key=str(definition.get("provider_key") or "").strip().lower(),
                    trigger_type_id=str(definition.get("trigger_type_id") or "").strip(),
                    display_name=str(
                        command.display_name
                        or existing.get("display_name")
                        or definition.get("display_name")
                        or ""
                    ).strip(),
                    enabled=enabled,
                    event_name=event_name,
                    trigger_config_jsonb=config,
                    readiness_status=readiness,
                    validation_errors_jsonb=errors,
                    account_id=account_id,
                )
        except ServiceNotFoundError as exc:
            raise WorkflowProviderTriggerError(
                "workflow_not_found", "Workflow not found", status_code=404
            ) from exc
        except Exception as exc:
            if self._is_scope_conflict(exc):
                raise WorkflowProviderTriggerError(
                    "provider_trigger_conflict",
                    "Provider trigger already exists for this workflow, project, and event binding",
                    status_code=409,
                ) from exc
            raise
        return updated, definition

    def delete(
        self,
        *,
        bl: Any,
        workflow_id: str,
        trigger_id: str,
        authorize_workflow: Callable[[str], None],
        authorize_project: Callable[[str], None],
    ) -> bool:
        self._load_workflow(workflow_id)
        authorize_workflow(workflow_id)
        try:
            with workflow_trigger_transaction(bl, workflow_id, include_trashed=True):
                existing = self._get_trigger(bl, trigger_id=trigger_id)
                if not existing or str(existing.get("workflow_id") or "").strip() != workflow_id:
                    raise WorkflowProviderTriggerError(
                        "provider_trigger_not_found", "Provider trigger not found", status_code=404
                    )
                authorize_project(str(existing.get("project_id") or "").strip())
                return bool(self._delete_trigger(bl, trigger_id=trigger_id))
        except ServiceNotFoundError as exc:
            raise WorkflowProviderTriggerError(
                "provider_trigger_not_found", "Provider trigger not found", status_code=404
            ) from exc


def create_workflow_provider_trigger_service(
    *,
    resolve_project_binding: Callable[..., tuple[str | None, dict[str, Any]]],
    load_workflow: Callable[[str], dict[str, Any]],
    is_scope_conflict: Callable[[Exception], bool],
    find_definition: Callable[..., dict[str, Any] | None] | None = None,
    validate_config: Callable[..., tuple[str, list[Any], str]] | None = None,
) -> WorkflowProviderTriggerService:
    from core.application.workflows import workflow_provider_trigger_repository
    from core.application.workflows.workflow_trigger_catalog import (
        find_provider_trigger_definition,
        validate_provider_trigger_config,
    )

    return WorkflowProviderTriggerService(
        find_definition=find_definition or find_provider_trigger_definition,
        validate_config=validate_config or validate_provider_trigger_config,
        create_trigger=workflow_provider_trigger_repository.create_workflow_provider_trigger,
        update_trigger=workflow_provider_trigger_repository.update_workflow_provider_trigger,
        delete_trigger=workflow_provider_trigger_repository.delete_workflow_provider_trigger,
        get_trigger=workflow_provider_trigger_repository.get_workflow_provider_trigger,
        resolve_project_binding=resolve_project_binding,
        load_workflow=load_workflow,
        is_scope_conflict=is_scope_conflict,
    )


__all__ = [
    "WorkflowProviderTriggerCreateCommand",
    "WorkflowProviderTriggerError",
    "WorkflowProviderTriggerService",
    "WorkflowProviderTriggerUpdateCommand",
    "create_workflow_provider_trigger_service",
]
