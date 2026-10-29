# ruff: noqa: B904
# mypy: ignore-errors
"""
Admin API routes — Skills CRUD with DB backend + I/O contracts.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints, field_validator

from core.application.management.command_models import SkillMutationResultView
from core.application.management.query_models import (
    RegistryContractItemView,
    SkillDiffView,
    SkillRecordView,
    SkillValidationView,
)
from core.application.registry.active_workflow_dependencies import (
    enabled_workflow_dependencies,
)
from core.application.registry.catalog_categories import catalog_category
from core.application.registry.contract_validation import (
    format_validation_error_detail,
    validate_contract_items,
    validate_skill_readiness,
)
from core.application.registry.db_store import RestoreValidationError
from core.application.registry.identifier_keys import unique_text_key
from core.application.registry.projected_artifact_rows import (
    projection_aware_catalog_row,
    projection_aware_catalog_rows,
)
from core.entrypoints.admin._readiness import enforce_enabled_save_validation
from core.entrypoints.admin._registry_scope import (
    assert_create_account_scope,
    assert_row_in_account_scope,
    request_scope,
)

SKILL_EXPECTED_INPUTS_VALUE = "skill.expected_inputs"
SKILL_EXPECTED_OUTPUTS_VALUE = "skill.expected_outputs"


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


def _project_skill_contract_fields(skill: dict) -> dict:
    payload = dict(skill or {})
    stored_inputs = payload.get("_inputs")
    stored_outputs = payload.get("_outputs")
    if isinstance(stored_inputs, list):
        payload["expected_inputs"] = list(stored_inputs)
    if isinstance(stored_outputs, list):
        payload["expected_outputs"] = list(stored_outputs)
    return payload


class SkillSave(BaseModel):
    skill_id: str | None = None
    version: str = "1.0.0"
    display_name: str
    description: str = ""
    enabled: bool = False
    category: str = ""
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

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        return catalog_category(v)


class SkillCreate(SkillSave):
    skill_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = Field(...)


class SkillPatch(BaseModel):
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    category: str | None = None
    skill_type: str | None = None
    instructions: list[str] | None = None
    prompt_template: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    examples: list[dict] | None = None
    builder_metadata_jsonb: dict | None = None
    tags: list[str] | None = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str | None) -> str | None:
        return None if v is None else catalog_category(v)


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
    db = _get_db()
    scope = request_scope(request)
    rows = projection_aware_catalog_rows(
        db,
        "skill",
        "skill_id",
        db.list_skills(enabled_only=enabled_only, account_id=scope.account_id),
        account_id=scope.account_id,
        enabled_only=enabled_only,
    )
    if search:
        query = str(search).strip().casefold()
        if query:
            rows = [
                row
                for row in rows
                if query
                in " ".join(
                    str(row.get(field) or "")
                    for field in ("skill_id", "display_name", "description", "category")
                ).casefold()
            ]
    return [_project_skill_contract_fields(row) for row in rows]


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
    db = _get_db()
    scope = request_scope(request)
    skill = projection_aware_catalog_row(
        db,
        "skill",
        "skill_id",
        skill_id,
        db.get_skill(skill_id),
        account_id=scope.account_id,
    )
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    assert_row_in_account_scope(skill, scope, detail=f"Skill '{skill_id}' not found")
    return _project_skill_contract_fields(skill)


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
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    return {"skill_id": skill_id, "validation": _get_skill_validation(db, skill_id)}


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
    db = _get_db()
    scope = request_scope(request)
    skill_id = unique_text_key(
        body.skill_id,
        lambda candidate: db.get_skill(candidate) is not None,
    )

    payload = body.model_dump()
    payload["skill_id"] = skill_id
    assert_create_account_scope(payload, scope)
    # Registry ownership is transport context, not client-controlled SkillSave
    # data. Otherwise non-default-account creates fall back to Main account.
    payload["account_id"] = scope.account_id

    _, input_issues = validate_contract_items(
        payload.get("expected_inputs") or [],
        path_prefix=SKILL_EXPECTED_INPUTS_VALUE,
        default_required=False,
        db=db,
    )
    _, output_issues = validate_contract_items(
        payload.get("expected_outputs") or [],
        path_prefix=SKILL_EXPECTED_OUTPUTS_VALUE,
        default_required=True,
        db=db,
    )
    if input_issues or output_issues:
        validation = {"valid": False, "errors": input_issues + output_issues, "warnings": []}
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="skill",
                entity_id=skill_id,
                action="save",
                validation=validation,
            ),
        )

    with db._transaction():
        db.save_skill(payload, author=scope.actor_name)
        validation = _get_skill_validation(db, skill_id)
        enforce_enabled_save_validation(
            entity_type="skill",
            entity_id=skill_id,
            action="save",
            validation=validation,
            final_enabled=bool(payload.get("enabled", False)),
        )
    _get_registry().reload_all()
    return {"status": "created", "skill_id": skill_id, "validation": validation}


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
    db = _get_db()
    scope = request_scope(request)
    existing = db.get_skill(skill_id)
    if existing:
        assert_row_in_account_scope(existing, scope, detail=f"Skill '{skill_id}' not found")
    data = body.model_dump()
    data["skill_id"] = skill_id
    assert_create_account_scope(data, scope)
    # Full replacement must retain the row's authenticated account ownership.
    data["account_id"] = existing.get("account_id") if existing is not None else scope.account_id
    projected_existing = _project_skill_contract_fields(existing or {})
    if "expected_inputs" not in body.model_fields_set:
        data["expected_inputs"] = list(projected_existing.get("expected_inputs") or [])
    if "expected_outputs" not in body.model_fields_set:
        data["expected_outputs"] = list(projected_existing.get("expected_outputs") or [])

    _, input_issues = validate_contract_items(
        data.get("expected_inputs") or [],
        path_prefix=SKILL_EXPECTED_INPUTS_VALUE,
        default_required=False,
        db=db,
    )
    _, output_issues = validate_contract_items(
        data.get("expected_outputs") or [],
        path_prefix=SKILL_EXPECTED_OUTPUTS_VALUE,
        default_required=True,
        db=db,
    )
    if input_issues or output_issues:
        validation = {"valid": False, "errors": input_issues + output_issues, "warnings": []}
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="skill",
                entity_id=skill_id,
                action="save",
                validation=validation,
            ),
        )

    with db._transaction():
        db.save_skill(data, author=scope.actor_name)
        validation = _get_skill_validation(db, skill_id)
        enforce_enabled_save_validation(
            entity_type="skill",
            entity_id=skill_id,
            action="save",
            validation=validation,
            final_enabled=bool(data.get("enabled", False)),
        )
    _get_registry().reload_all()
    db.materialize_agents_for_skill(skill_id)
    return {"status": "updated", "skill_id": skill_id, "validation": validation}


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
    db = _get_db()
    existing = db.get_skill(skill_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    assert_row_in_account_scope(
        existing, request_scope(request), detail=f"Skill '{skill_id}' not found"
    )

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"status": "no_change", "skill_id": skill_id}

    if bool(existing.get("enabled", False)) and updates.get("enabled") is False:
        dependencies = enabled_workflow_dependencies(
            db,
            agent_ids=db.get_agents_using_skill(skill_id),
        )
        if dependencies:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Active workflow dependency",
                    "message": (
                        f"Skill '{skill_id}' cannot be disabled while enabled workflows "
                        f"depend on an agent using it: {', '.join(dependencies)}. Disable "
                        "or update those workflows first."
                    ),
                    "workflow_ids": dependencies,
                },
            )

    merged = {**existing, **updates, "skill_id": skill_id}
    with db._transaction():
        db.save_skill(merged, author=request_scope(request).actor_name)
        validation = _get_skill_validation(db, skill_id)
        enforce_enabled_save_validation(
            entity_type="skill",
            entity_id=skill_id,
            action="save",
            validation=validation,
            final_enabled=bool(merged.get("enabled", False)),
        )
    _get_registry().reload_all()
    return {"status": "updated", "skill_id": skill_id, "validation": validation}


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
    db = _get_db()
    current = db.get_skill(skill_id)
    if not current:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    assert_row_in_account_scope(
        current, request_scope(request), detail=f"Skill '{skill_id}' not found"
    )

    if bool(current.get("enabled", False)):
        dependencies = enabled_workflow_dependencies(
            db,
            agent_ids=db.get_agents_using_skill(skill_id),
        )
        if dependencies:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Active workflow dependency",
                    "message": (
                        f"Skill '{skill_id}' cannot be disabled while enabled workflows "
                        f"depend on an agent using it: {', '.join(dependencies)}. Disable "
                        "or update those workflows first."
                    ),
                    "workflow_ids": dependencies,
                },
            )

    if not bool(current.get("enabled", False)):
        validation = _get_skill_validation(db, skill_id)
        if not validation.get("valid", False):
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="skill",
                    entity_id=skill_id,
                    action="enable",
                    validation=validation,
                ),
            )

    new_state = db.toggle_skill(skill_id)
    _get_registry().reload_all()
    return {"status": "toggled", "skill_id": skill_id, "enabled": new_state}


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
    db = _get_db()
    scope = request_scope(request)
    existing = db.get_skill(skill_id)
    if existing:
        assert_row_in_account_scope(existing, scope, detail=f"Skill '{skill_id}' not found")
    dependency_lister = getattr(db, "get_skill_dependents", None)
    dependency_rows = dependency_lister(skill_id, limit=26) if callable(dependency_lister) else []
    if not isinstance(dependency_rows, list):
        dependency_rows = []
    dependent_ids = [
        f"{str(row.get('dependency_kind') or '').strip()}:"
        f"{str(row.get('dependency_id') or '').strip()}"
        for row in dependency_rows[:25]
        if isinstance(row, dict)
        and str(row.get("dependency_kind") or "").strip()
        and str(row.get("dependency_id") or "").strip()
    ]
    if dependent_ids:
        action = "Remove or replace every listed dependency before trashing the skill."
        raise HTTPException(
            status_code=409,
            detail={
                "error": "registry_dependency_conflict",
                "entity_type": "registry_skill",
                "entity_id": skill_id,
                "dependent_ids": dependent_ids,
                "action": action,
                **({"dependent_ids_truncated": True} if len(dependency_rows) > 25 else {}),
            },
        )
    try:
        db.delete_skill(skill_id, author=scope.actor_name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0] if e.args else e))
    except Exception as e:
        if "violates foreign key" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"Skill '{skill_id}' is still assigned to agents or workflow steps. Remove associations first.",
            )
        raise HTTPException(status_code=500, detail=str(e))

    _get_registry().reload_all()
    return {"status": "deleted", "skill_id": skill_id}


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
    db = _get_db()
    scope = request_scope(request)
    source = _require_skill_in_scope(db, skill_id, request)

    target_id = unique_text_key(
        body.resolved_target_id() or f"{skill_id}_copy",
        lambda candidate: db.get_skill(candidate) is not None,
    )

    clone_data = {
        "skill_id": target_id,
        "account_id": scope.account_id,
        "display_name": body.display_name or f"{source.get('display_name', skill_id)} (Copy)",
        "description": source.get("description", ""),
        "category": source.get("category"),
        "skill_type": source.get("skill_type", "secondary"),
        "instructions": list(source.get("instructions") or []),
        "prompt_template": str(source.get("prompt_template") or ""),
        "expected_inputs": list(source.get("expected_inputs") or source.get("_inputs") or []),
        "expected_outputs": list(source.get("expected_outputs") or source.get("_outputs") or []),
        "examples": list(source.get("examples") or []),
        "enabled": False,
        "version": source.get("version", "1.0.0"),
        "tags": list(source.get("tags") or []),
    }
    db.save_skill(clone_data, author=scope.actor_name)
    _get_registry().reload_all()
    return {"status": "cloned", "skill_id": target_id, "source_skill_id": skill_id}


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
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    try:
        outcome = db.restore_entity_revision(
            entity_type="skill",
            entity_id=skill_id,
            entry_id=str(body.entry_id),
            author="admin_restore",
            validate_restored=lambda _entity_type, _entity_id, _restored: _get_skill_validation(
                db, skill_id
            ),
        )
    except RestoreValidationError as e:
        validation = (
            e.validation
            if isinstance(e.validation, dict)
            else {
                "valid": False,
                "errors": [],
                "warnings": [],
            }
        )
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="skill",
                entity_id=skill_id,
                action="restore",
                validation=validation,
            ),
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    validation = outcome.get("validation")
    if not isinstance(validation, dict):
        validation = _get_skill_validation(db, skill_id)

    _get_registry().reload_all()
    return {
        "status": "restored",
        "skill_id": skill_id,
        "restored_from_entry_id": str(body.entry_id),
        "validation": validation,
        "snapshot": outcome.get("restored"),
    }


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
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    payload = [i.model_dump() for i in inputs]
    _, issues = validate_contract_items(
        payload,
        path_prefix=SKILL_EXPECTED_INPUTS_VALUE,
        default_required=False,
        db=db,
    )
    if issues:
        validation = {"valid": False, "errors": issues, "warnings": []}
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="skill",
                entity_id=skill_id,
                action="save_inputs",
                validation=validation,
            ),
        )
    db.set_skill_inputs(skill_id, payload)
    _get_registry().reload_all()
    db.materialize_agents_for_skill(skill_id)
    return db.get_skill_inputs(skill_id)


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
    db = _get_db()
    _require_skill_in_scope(db, skill_id, request)
    payload = [o.model_dump() for o in outputs]
    _, issues = validate_contract_items(
        payload,
        path_prefix=SKILL_EXPECTED_OUTPUTS_VALUE,
        default_required=True,
        db=db,
    )
    if issues:
        validation = {"valid": False, "errors": issues, "warnings": []}
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="skill",
                entity_id=skill_id,
                action="save_outputs",
                validation=validation,
            ),
        )
    db.set_skill_outputs(skill_id, payload)
    _get_registry().reload_all()
    db.materialize_agents_for_skill(skill_id)
    return db.get_skill_outputs(skill_id)
