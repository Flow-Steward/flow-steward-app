# ruff: noqa: F403, F405, RUF012, RUF022
"""Workflow admin route/helper module extracted from workflows."""

from __future__ import annotations

from pydantic import ConfigDict, model_validator
from pydantic_core import PydanticCustomError

from core.application.workflows.workflow_authoring_requests import (
    WorkflowImportRequest,
)
from core.application.workflows.workflow_authoring_requests import (
    validate_workflow_id as _validate_workflow_id,
)
from core.application.workflows.workflow_change_normalization import (
    WorkflowChangeNormalizationError,
    validate_context_inject_sources,
)
from core.application.workflows.workflow_change_normalization import (
    normalize_workflow_participants as _normalize_workflow_participants,
)
from core.application.workflows.workflow_definition_service import preserve_service_owned_settings
from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR

#: Definition fields a metadata body never carries; only the change service writes them.
_DEFINITION_FIELDS = frozenset({"phases", "_phases", "steps", "groups"})


def _refuse_definition_fields(value):
    """A metadata body never carries the definition; say where it goes instead."""
    if isinstance(value, dict):
        definition_fields = sorted(set(value) & _DEFINITION_FIELDS)
        if definition_fields:
            raise PydanticCustomError(
                "workflow_definition_field_unsupported",
                (
                    "Workflow field(s) {fields} are not saved with metadata. "
                    "Change steps, groups and the execution graph through "
                    "/api/workflows/<workflow_id>/changes."
                ),
                {"fields": ", ".join(definition_fields)},
            )
    return value


class WorkflowSave(BaseModel):
    workflow_id: str
    version: str = "1.0.0"
    display_name: str
    description: str = ""
    enabled: bool = False
    account_id: str | None = None
    project_id: str | None = None
    scope_kind: str = "account"
    source_install_id: str | None = None
    source_release_id: str | None = None
    source_projection_id: str | None = None
    local_workflow_id: str | None = None
    projection_mode: str = "manual"
    derived_from_kind: str = ""
    derived_from_ref: str = ""
    sync_status: str = "active"
    availability_status: str = "available"
    availability_reason_code: str = "none"
    availability_reason_text: str = ""
    new_runs_blocked: bool = False
    visible_in_catalog: bool = True
    trigger: dict = {}
    settings: dict = {}
    builder_metadata_jsonb: dict = {}
    participants: list[dict] = []
    tags: list[str] = []
    expected_revision: int | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_definition_fields(cls, value):
        return _refuse_definition_fields(value)

    @field_validator("workflow_id")
    @classmethod
    def validate_workflow_id(cls, value: str) -> str:
        return _validate_workflow_id(value, field_name="workflow_id") or ""


class WorkflowMetadataUpdate(BaseModel):
    """Strict full metadata replacement for PUT; definition edits use the change service."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    version: str = "1.0.0"
    display_name: str
    description: str = ""
    enabled: bool = False
    account_id: str | None = None
    project_id: str | None = None
    scope_kind: str = "account"
    source_install_id: str | None = None
    source_release_id: str | None = None
    source_projection_id: str | None = None
    local_workflow_id: str | None = None
    projection_mode: str = "manual"
    derived_from_kind: str = ""
    derived_from_ref: str = ""
    sync_status: str = "active"
    availability_status: str = "available"
    availability_reason_code: str = "none"
    availability_reason_text: str = ""
    new_runs_blocked: bool = False
    visible_in_catalog: bool = True
    trigger: dict = {}
    settings: dict = {}
    builder_metadata_jsonb: dict = {}
    participants: list[dict] = []
    tags: list[str] = []
    expected_revision: int | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_graph_and_unknown_fields(cls, value):
        if isinstance(value, dict):
            unsupported = sorted(set(value) - set(cls.model_fields))
            if unsupported:
                raise PydanticCustomError(
                    "workflow_metadata_field_unsupported",
                    (
                        "Unsupported workflow metadata field(s): {fields}. "
                        "Change steps, groups and the execution graph through "
                        "/api/workflows/<workflow_id>/changes."
                    ),
                    {"fields": ", ".join(unsupported)},
                )
        return value

    @field_validator("workflow_id")
    @classmethod
    def validate_workflow_id(cls, value: str) -> str:
        return _validate_workflow_id(value, field_name="workflow_id") or ""


class WorkflowPatch(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def reject_definition_fields(cls, value):
        return _refuse_definition_fields(value)

    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    account_id: str | None = None
    project_id: str | None = None
    scope_kind: str | None = None
    source_install_id: str | None = None
    source_release_id: str | None = None
    source_projection_id: str | None = None
    local_workflow_id: str | None = None
    projection_mode: str | None = None
    derived_from_kind: str | None = None
    derived_from_ref: str | None = None
    sync_status: str | None = None
    availability_status: str | None = None
    availability_reason_code: str | None = None
    availability_reason_text: str | None = None
    new_runs_blocked: bool | None = None
    visible_in_catalog: bool | None = None
    trigger: dict | None = None
    settings: dict | None = None
    builder_metadata_jsonb: dict | None = None
    participants: list[dict] | None = None
    tags: list[str] | None = None
    expected_revision: int | None = None


class WorkflowCloneRequest(BaseModel):
    # No `enabled`: a clone is always created disabled (see RegistryDBStore.clone_workflow).
    workflow_id: str | None = None
    display_name: str | None = None
    description: str | None = None
    project_id: str | None = None

    @field_validator("workflow_id")
    @classmethod
    def validate_workflow_id(cls, value: str | None) -> str | None:
        return _validate_workflow_id(value, field_name="workflow_id")


class WorkflowInputSecretWrite(BaseModel):
    """The one payload that carries a secret input's value, on the way in only."""

    model_config = ConfigDict(extra="forbid")

    value: str
    expected_revision: int | None = None


class RestoreRevisionRequest(BaseModel):
    entry_id: str | int
    expected_revision: int | None = None


def _sanitize_workflow_trigger(trigger: dict | None) -> dict:
    """Validate and normalize generic workflow trigger payloads."""
    if not isinstance(trigger, dict):
        return {}
    forbidden = sorted(key for key in FORBIDDEN_TRIGGER_KEYS if key in trigger)
    if forbidden:
        joined = ", ".join(forbidden)
        raise HTTPException(
            status_code=400,
            detail=(
                f"workflow trigger rejected unsupported routing keys: {joined}. "
                "Use authored workflow config, gates, variables, and explicit bindings instead."
            ),
        )
    cleaned = dict(trigger)
    if "confidence_threshold" in cleaned:
        try:
            raw_threshold = cleaned.get("confidence_threshold")
            value = float(raw_threshold) if isinstance(raw_threshold, (int, float, str)) else 0.0
        except (TypeError, ValueError):
            value = 0.0
        cleaned["confidence_threshold"] = max(0.0, min(1.0, value))
    return cleaned


def _with_participants_in_settings(payload: dict) -> dict:
    data = dict(payload or {})
    settings = data.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    marker = object()
    participants_raw = data.get("participants", marker)
    if participants_raw is marker or participants_raw is None:
        participants = _normalize_workflow_participants(settings.get("participants"))
    else:
        participants = _normalize_workflow_participants(participants_raw)
    settings["participants"] = participants
    data["settings"] = settings
    data["participants"] = participants
    return data


def _with_service_owned_settings(payload: dict, existing: Any = None) -> dict:
    """Keep caller-supplied settings out of the keys the change service owns."""
    return preserve_service_owned_settings(payload, existing)


def _merge_patch_dicts(existing: Any, updates: Any) -> Any:
    if not isinstance(existing, dict) or not isinstance(updates, dict):
        return updates
    merged = dict(existing)
    for key, value in updates.items():
        existing_value = merged.get(key)
        if isinstance(existing_value, dict) and isinstance(value, dict):
            merged[key] = _merge_patch_dicts(existing_value, value)
        else:
            merged[key] = value
    return merged


def _validate_context_inject_sources(context_inject: dict | None) -> None:
    try:
        validate_context_inject_sources(context_inject)
    except WorkflowChangeNormalizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


__all__ = [
    "_with_service_owned_settings",
    "WorkflowInputSecretWrite",
    "WorkflowSave",
    "WorkflowMetadataUpdate",
    "WorkflowPatch",
    "WorkflowCloneRequest",
    "WorkflowImportRequest",
    "RestoreRevisionRequest",
    "_sanitize_workflow_trigger",
    "_normalize_workflow_participants",
    "_with_participants_in_settings",
    "_merge_patch_dicts",
    "_validate_context_inject_sources",
]
