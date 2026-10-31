# ruff: noqa: B904
# mypy: ignore-errors
"""
Admin API routes — Skills CRUD with DB backend + I/O contracts.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints

from core.application.management import SkillCommandService
from core.application.management.command_models import SkillMutationResultView
from core.application.management.errors import ServiceError
from core.application.management.query_models import (
    RegistryContractItemView,
    SkillDiffView,
    SkillRecordView,
    SkillValidationView,
)
from core.application.registry.catalog_categories import CatalogCategory
from core.application.registry.contract_validation import validate_skill_readiness
from core.entrypoints.admin._registry_scope import (
    assert_row_in_account_scope,
    request_scope,
)
from core.entrypoints.admin._service_errors import raise_service_http_error, service_db_getter

router = APIRouter(prefix="/api/skills", tags=["skills"])

_SKILL_SAVE_REQUEST_EXAMPLE = {
    "skill_id": "draft_release_notes",
    "display_name": "Draft Release Notes",
    "description": "Draft customer-facing release notes from a prepared changelog.",
    "enabled": False,
    "category": "marketing",
    "skill_type": "secondary",
    "instructions": ["Summarize the most important user-visible changes first."],
    "prompt_template": "Turn the changelog into release notes.",
    "expected_inputs": [
        {"name": "changelog_summary", "type": "text", "description": "Prepared changelog summary."}
    ],
    "expected_outputs": [
        {
            "name": "draft_notes",
            "type": "text",
            "description": "Release note draft.",
            "required": True,
        }
    ],
    "tags": ["release", "writing"],
}
_SKILL_PATCH_REQUEST_EXAMPLE = {
    "description": "Draft release notes with a stronger focus on customer value.",
    "enabled": True,
    "instructions": ["Lead with impact, then mention notable fixes."],
}
_SKILL_CLONE_REQUEST_EXAMPLE = {
    "skill_id": "draft_release_notes_copy",
    "display_name": "Draft Release Notes (Copy)",
}
_SKILL_RESTORE_REQUEST_EXAMPLE = {"entry_id": "204"}

_SKILL_CREATE_RESPONSE_EXAMPLE = {
    "status": "created",
    "skill_id": "draft_release_notes",
    "validation": {"valid": True, "errors": [], "warnings": []},
}
_SKILL_UPDATE_RESPONSE_EXAMPLE = {
    "status": "updated",
    "skill_id": "draft_release_notes",
    "validation": {"valid": True, "errors": [], "warnings": []},
}
_SKILL_TOGGLE_RESPONSE_EXAMPLE = {
    "status": "toggled",
    "skill_id": "draft_release_notes",
    "enabled": True,
}
_SKILL_DELETE_RESPONSE_EXAMPLE = {
    "status": "deleted",
    "skill_id": "draft_release_notes",
}
_SKILL_CLONE_RESPONSE_EXAMPLE = {
    "status": "cloned",
    "skill_id": "draft_release_notes_copy",
    "source_skill_id": "draft_release_notes",
}
_SKILL_RESTORE_RESPONSE_EXAMPLE = {
    "status": "restored",
    "skill_id": "draft_release_notes",
    "restored_from_entry_id": "204",
    "validation": {"valid": True, "errors": [], "warnings": []},
    "snapshot": {"skill_id": "draft_release_notes", "display_name": "Draft Release Notes"},
}


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _get_db():
    rm = _get_registry()
    if not rm.db_store:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return rm.db_store


def _get_skill_validation(db, skill_id: str) -> dict:
    return validate_skill_readiness(db, skill_id)


def _skill_command_service() -> SkillCommandService:
    # Late-bound lambdas keep module attributes (``_get_db``, ``_get_registry``,
    # ``_get_skill_validation``) as the single substitution points.
    return SkillCommandService(
        get_db=service_db_getter(lambda: _get_db()),
        reload_registry=lambda: _get_registry().reload_all(),
        get_skill_validation=lambda db, skill_id: _get_skill_validation(db, skill_id),
    )


def _require_skill_in_scope(db, skill_id: str, request: Request) -> dict:
    """Load a skill and enforce caller account scope, else fail closed (404).

    Shared by the skill subresource routes (contract-validation, agents-using,
    clone, history/diff/restore, inputs, outputs) so none of them can read or
    mutate a skill owned by another account.
    """
    skill = db.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    assert_row_in_account_scope(
        skill, request_scope(request), detail=f"Skill '{skill_id}' not found"
    )
    return skill


class SkillSave(BaseModel):
    skill_id: str | None = None
    version: str = "1.0.0"
    display_name: str
    description: str = ""
    enabled: bool = False
    category: CatalogCategory = ""
    skill_type: str = "secondary"
    instructions: list[str] = []
    prompt_template: str = ""
    expected_inputs: list[dict] = []
    expected_outputs: list[dict] = []
    input_schema: dict = {}
    output_schema: dict = {}
    examples: list[dict] = []
    builder_metadata_jsonb: dict = {}
    tags: list[str] = []


class SkillCreate(SkillSave):
    skill_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = Field(...)


class SkillPatch(BaseModel):
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    category: CatalogCategory | None = None
    skill_type: str | None = None
    instructions: list[str] | None = None
    prompt_template: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    examples: list[dict] | None = None
    builder_metadata_jsonb: dict | None = None
    tags: list[str] | None = None


class InputItem(BaseModel):
    name: str
    type: str = "text"
    description: str = ""
    required: bool = False
    contract_id: str | None = None


class OutputItem(BaseModel):
    name: str
    type: str = "text"
    description: str = ""
    required: bool = True
    contract_id: str | None = None


class RestoreRevisionRequest(BaseModel):
    entry_id: str | int


class SkillCloneRequest(BaseModel):
    skill_id: str | None = None
    display_name: str | None = None

    def resolved_target_id(self) -> str | None:
        return str(self.skill_id or "").strip() or None


@router.get(
    "",
    response_model=list[SkillRecordView],
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def list_skills(
    request: Request,
    enabled_only: Annotated[bool, Query()] = False,
    search: Annotated[str | None, Query()] = None,
):
    try:
        return _skill_command_service().list_skills(
            scope=request_scope(request), enabled_only=enabled_only, search=search
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.get(
    "/{skill_id}",
    response_model=SkillRecordView,
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_skill(skill_id: str, request: Request):
    try:
        return _skill_command_service().get_skill(skill_id, scope=request_scope(request))
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.get(
    "/{skill_id}/contract-validation",
    response_model=SkillValidationView,
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_skill_contract_validation(skill_id: str, request: Request):
    try:
        return _skill_command_service().validate_skill(skill_id, scope=request_scope(request))
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.get(
    "/{skill_id}/agents",
    response_model=list[str],
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_agents_using_skill(skill_id: str, request: Request):
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    return db.get_agents_using_skill(skill_id)


@router.post(
    "",
    response_model=SkillMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _SKILL_CREATE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def create_skill(
    request: Request,
    body: Annotated[
        SkillCreate,
        Body(
            openapi_examples={
                "createSkill": {
                    "summary": "Create a skill for drafting release notes.",
                    "value": _SKILL_SAVE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    try:
        return _skill_command_service().create_skill(
            body.model_dump(), scope=request_scope(request)
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.put(
    "/{skill_id}",
    response_model=SkillMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _SKILL_UPDATE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def update_skill(
    skill_id: str,
    request: Request,
    body: Annotated[
        SkillSave,
        Body(
            openapi_examples={
                "replaceSkill": {
                    "summary": "Replace the full skill definition.",
                    "value": _SKILL_SAVE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    try:
        return _skill_command_service().update_skill(
            skill_id,
            body.model_dump(),
            provided_fields=body.model_fields_set,
            scope=request_scope(request),
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.patch(
    "/{skill_id}",
    response_model=SkillMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _SKILL_UPDATE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def patch_skill(
    skill_id: str,
    request: Request,
    body: Annotated[
        SkillPatch,
        Body(
            openapi_examples={
                "patchSkill": {
                    "summary": "Update selected skill fields.",
                    "value": _SKILL_PATCH_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    try:
        return _skill_command_service().patch_skill(
            skill_id, body.model_dump(exclude_none=True), scope=request_scope(request)
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.patch(
    "/{skill_id}/toggle",
    response_model=SkillMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _SKILL_TOGGLE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def toggle_skill(skill_id: str, request: Request):
    try:
        return _skill_command_service().toggle_skill(skill_id, scope=request_scope(request))
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.delete(
    "/{skill_id}",
    response_model=SkillMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _SKILL_DELETE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def delete_skill(skill_id: str, request: Request):
    try:
        return _skill_command_service().delete_skill(skill_id, scope=request_scope(request))
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.post(
    "/{skill_id}/clone",
    response_model=SkillMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _SKILL_CLONE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def clone_skill(
    skill_id: str,
    request: Request,
    body: Annotated[
        SkillCloneRequest,
        Body(
            openapi_examples={
                "cloneSkill": {
                    "summary": "Clone a skill into a new skill id.",
                    "value": _SKILL_CLONE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    try:
        return _skill_command_service().clone_skill(
            skill_id,
            target_skill_id=body.resolved_target_id(),
            display_name=body.display_name,
            scope=request_scope(request),
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.get(
    "/{skill_id}/history",
    response_model=list[SkillRecordView],
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_skill_history(
    skill_id: str, request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
):
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    return db.get_entity_history(entity_type="skill", entity_id=skill_id, limit=limit)


@router.get(
    "/{skill_id}/history/{revision}",
    response_model=SkillRecordView,
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_skill_revision(skill_id: str, revision: int, request: Request):
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    entry = db.get_entity_revision(entity_type="skill", entity_id=skill_id, revision=revision)
    if not entry:
        raise HTTPException(
            status_code=404, detail=f"Revision {revision} not found for skill '{skill_id}'"
        )
    return entry


@router.get(
    "/{skill_id}/diff",
    response_model=SkillDiffView,
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_skill_diff(
    skill_id: str,
    request: Request,
    from_entry_id: Annotated[str | None, Query()] = None,
    to_entry_id: Annotated[str | None, Query()] = None,
):
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    try:
        return db.diff_entity_history(
            entity_type="skill",
            entity_id=skill_id,
            from_entry_id=from_entry_id,
            to_entry_id=to_entry_id,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post(
    "/{skill_id}/restore",
    response_model=SkillMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _SKILL_RESTORE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def restore_skill_revision(
    skill_id: str,
    request: Request,
    body: Annotated[
        RestoreRevisionRequest,
        Body(
            openapi_examples={
                "restoreSkill": {
                    "summary": "Restore a previously recorded skill revision.",
                    "value": _SKILL_RESTORE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    try:
        return _skill_command_service().restore_skill_revision(
            skill_id, body.entry_id, scope=request_scope(request)
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.get(
    "/{skill_id}/inputs",
    response_model=list[RegistryContractItemView],
    response_model_exclude_none=True,
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_skill_inputs(skill_id: str, request: Request):
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    return db.get_skill_inputs(skill_id)


@router.put(
    "/{skill_id}/inputs",
    response_model=list[RegistryContractItemView],
    response_model_exclude_none=True,
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def set_skill_inputs(skill_id: str, request: Request, inputs: list[InputItem]):
    try:
        return _skill_command_service().set_skill_contract_items(
            skill_id,
            "inputs",
            [item.model_dump() for item in inputs],
            scope=request_scope(request),
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


@router.get(
    "/{skill_id}/outputs",
    response_model=list[RegistryContractItemView],
    response_model_exclude_none=True,
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_skill_outputs(skill_id: str, request: Request):
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    return db.get_skill_outputs(skill_id)


@router.put(
    "/{skill_id}/outputs",
    response_model=list[RegistryContractItemView],
    response_model_exclude_none=True,
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def set_skill_outputs(skill_id: str, request: Request, outputs: list[OutputItem]):
    try:
        return _skill_command_service().set_skill_contract_items(
            skill_id,
            "outputs",
            [item.model_dump() for item in outputs],
            scope=request_scope(request),
        )
    except ServiceError as exc:
        raise_service_http_error(exc)
