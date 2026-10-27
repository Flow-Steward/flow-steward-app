# ruff: noqa: F403, F405, I001, RUF022
"""Workflow admin route/helper module extracted from workflows."""

from __future__ import annotations

from core.application.workflows.workflow_consumer_guard import WorkflowConsumerConflictError

from core.application.registry.boundary_contracts import (
    validate_workflow_boundary_contracts,
)

from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR
from core.entrypoints.admin.workflows_models import *  # NOSONAR
from core.application.registry.db_store_workflows_mixin import (
    WorkflowCreateConflictError,
    WorkflowRevisionConflictError,
)
from core.application.registry.workflow_secret_defaults import SecretDefaultRejected
from core.application.management.errors import ServiceConflictError
from core.application.workflows.workflow_secret_default_service import (
    WorkflowMetadataValidationError,
    WorkflowSecretMutationError,
)
from core.application.workflows.workflow_sample_file_service import WorkflowSampleFileError
from core.infrastructure.config.workflow_file_limits import workflow_test_file_max_bytes
from core.application.workflows.workflow_activation_service import (
    WorkflowActivationValidationError,
    toggle_workflow_enabled,
)
from fastapi import File, Form, UploadFile


def _workflow_expected_revision(
    body: WorkflowSave | WorkflowMetadataUpdate | WorkflowPatch, existing: dict
) -> int | None:
    return _workflow_revision_guard(body.expected_revision, existing)


def _workflow_revision_guard(expected_revision: int | None, existing: dict) -> int | None:
    """The optimistic-concurrency rule, independent of which body carries it."""
    current_revision = existing.get("revision")
    if current_revision is None:
        return None
    if expected_revision is None:
        raise HTTPException(
            status_code=428,
            detail={
                "code": "workflow_revision_required",
                "message": "Reload the workflow and retry with expected_revision.",
                "current_revision": int(current_revision),
            },
        )
    if int(expected_revision) < 1:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "workflow_revision_invalid",
                "message": "expected_revision must be a positive integer.",
            },
        )
    return int(expected_revision)


def _raise_consumer_conflict(exc: WorkflowConsumerConflictError) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "workflow_consumer_conflict",
            "message": str(exc),
            "workflow_id": exc.workflow_id,
            "consumers": exc.issues,
        },
    ) from exc


def _raise_workflow_revision_conflict(exc: WorkflowRevisionConflictError) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "workflow_revision_conflict",
            "message": "The workflow changed after it was loaded. Reload before saving.",
            "workflow_id": exc.workflow_id,
            "expected_revision": exc.expected_revision,
        },
    ) from exc


@router.post(
    "",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def create_workflow(body: WorkflowSave, request: Request):
    """Create a workflow's metadata; its steps, groups and graph come through /changes."""
    scope = _request_scope(request)
    require_default_registry_account(scope)
    db = _get_db()
    try:
        workflow_id = body.workflow_id
        if db.get_workflow(workflow_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Workflow '{workflow_id}' already exists",
            )
        portability = _get_workflow_portability_service(db)
        request_payload = body.model_dump()
        request_payload["workflow_id"] = workflow_id
        step_config_issues = validate_workflow_step_config_keys(request_payload)
        step_config_issues.extend(validate_workflow_boundary_contracts(request_payload))
        if step_config_issues:
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="workflow",
                    entity_id=workflow_id,
                    action="create",
                    validation={
                        "valid": False,
                        "errors": step_config_issues,
                        "warnings": [],
                    },
                ),
            )
        payload = portability.public_create_payload(
            _with_service_owned_settings(_with_participants_in_settings(request_payload))
        )
        normalized_project_id = _enforce_project_scope(
            db,
            scope,
            body.project_id or getattr(scope, "project_id", None),
        )
        payload["account_id"] = scope.account_id
        # User-facing workflow creation is account-local. Platform-owned
        # registry records are provisioned outside this endpoint.
        payload["scope_kind"] = "account"
        if normalized_project_id:
            payload["project_id"] = normalized_project_id
        payload["trigger"] = _sanitize_workflow_trigger(payload.get("trigger"))
        with db._transaction():
            db.save_workflow(payload, author=scope.actor_name, create_only=True)
            _bind_workflow_to_project_if_requested(
                db,
                workflow_id=workflow_id,
                project_id=normalized_project_id,
            )
            validation = _normalize_disabled_workflow_validation(
                _get_workflow_validation(
                    db,
                    workflow_id,
                    account_id=scope.account_id,
                ),
                final_enabled=bool(payload.get("enabled", False)),
            )
            # A new workflow starts empty and disabled; its definition is added
            # through /changes. Only an enabled workflow must be valid now.
            enforce_enabled_save_validation(
                entity_type="workflow",
                entity_id=workflow_id,
                action="save",
                validation=validation,
                final_enabled=bool(payload.get("enabled", False)),
            )
    except WorkflowCreateConflictError as exc:
        raise HTTPException(
            status_code=409, detail=f"Workflow '{body.workflow_id}' already exists"
        ) from exc
    except HTTPException:
        raise
    except DATABASE_OPERATION_ERRORS as exc:
        _raise_workflow_backend_unavailable(
            workflow_id=body.workflow_id,
            action="create",
            exc=exc,
        )
    reload_warnings = _reload_registry_after_workflow_mutation(
        workflow_id=workflow_id,
        action="create",
    )
    response = {"status": "created", "workflow_id": workflow_id, "validation": validation}
    if reload_warnings:
        response["warnings"] = reload_warnings
    return response


@router.put(
    "/{workflow_id}",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def update_workflow(workflow_id: str, body: WorkflowMetadataUpdate, request: Request):
    """Full update of workflow metadata (does NOT touch phases)."""
    db = _get_db()
    scoped_workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if scoped_workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    existing = db.get_workflow(workflow_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    _assert_workflow_mutation_allowed(existing)
    expected_revision = _workflow_expected_revision(body, existing)
    try:
        data = _get_workflow_portability_service(db).public_update_payload(
            dict(existing),
            _with_service_owned_settings(
                _with_participants_in_settings(body.model_dump()), existing
            ),
        )
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    data["workflow_id"] = workflow_id
    data["account_id"] = _normalize_text(scoped_workflow.get("account_id")) or scope.account_id
    data["scope_kind"] = "account"
    data["trigger"] = _sanitize_workflow_trigger(data.get("trigger"))
    data.pop("expected_revision", None)
    try:
        account_id = _normalize_text(data.get("account_id")) or scope.account_id
        final_enabled = bool(data.get("enabled", False))
        saved_revision, validation = _workflow_secret_default_service(db).save_workflow_metadata(
            workflow_id=workflow_id,
            account_id=account_id,
            existing=existing,
            final_payload=data,
            author=_request_scope(request).actor_name,
            expected_revision=expected_revision,
            validate_saved=lambda: _get_workflow_validation(db, workflow_id, account_id=account_id),
            final_enabled=final_enabled,
        )
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except WorkflowConsumerConflictError as exc:
        _raise_consumer_conflict(exc)
    except WorkflowMetadataValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="workflow",
                entity_id=workflow_id,
                action="save",
                validation=exc.validation,
            ),
        ) from exc
    except WorkflowSecretMutationError as exc:
        _raise_workflow_secret_mutation_error(exc)
    except SecretDefaultRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "workflow_secret_rejected", "message": str(exc)},
        ) from exc
    _refresh_workflow_cache_after_commit()
    return {
        "status": "updated",
        "workflow_id": workflow_id,
        "revision": saved_revision,
        "validation": validation,
    }


@router.patch(
    "/{workflow_id}",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def patch_workflow(workflow_id: str, body: WorkflowPatch, request: Request):
    """Partial update of workflow metadata."""
    db = _get_db()
    scoped_workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if scoped_workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    existing = db.get_workflow(workflow_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    _assert_workflow_mutation_allowed(existing)
    expected_revision = _workflow_expected_revision(body, existing)

    updates = body.model_dump(exclude_none=True)
    updates.pop("expected_revision", None)
    if not updates:
        return {"status": "no_change", "workflow_id": workflow_id}
    try:
        updates = _get_workflow_portability_service(db).public_patch_payload(
            dict(existing),
            updates,
        )
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    if "trigger" in updates:
        existing_trigger = (
            existing.get("trigger") if isinstance(existing.get("trigger"), dict) else {}
        )
        sanitized_trigger = _sanitize_workflow_trigger(updates.get("trigger"))
        updates["trigger"] = {
            **existing_trigger,
            **sanitized_trigger,
        }
    if "settings" in updates:
        updates["settings"] = _merge_patch_dicts(existing.get("settings"), updates.get("settings"))

    merged = {**existing, **updates}
    merged = _with_service_owned_settings(_with_participants_in_settings(merged), existing)
    merged["workflow_id"] = workflow_id
    merged["account_id"] = (
        _normalize_text(scoped_workflow.get("account_id"))
        or _normalize_text(existing.get("account_id"))
        or scope.account_id
    )
    merged["scope_kind"] = "account"
    # Strip phases — PATCH only updates metadata, not phases/steps
    for k in ("phases", "_phases"):
        merged.pop(k, None)
    try:
        account_id = _normalize_text(merged.get("account_id")) or scope.account_id
        final_enabled = bool(merged.get("enabled", False))
        saved_revision, validation = _workflow_secret_default_service(db).save_workflow_metadata(
            workflow_id=workflow_id,
            account_id=account_id,
            existing=existing,
            final_payload=merged,
            author=_request_scope(request).actor_name,
            expected_revision=expected_revision,
            validate_saved=lambda: _get_workflow_validation(db, workflow_id, account_id=account_id),
            final_enabled=final_enabled,
        )
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except WorkflowConsumerConflictError as exc:
        _raise_consumer_conflict(exc)
    except WorkflowMetadataValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="workflow",
                entity_id=workflow_id,
                action="save",
                validation=exc.validation,
            ),
        ) from exc
    except WorkflowSecretMutationError as exc:
        _raise_workflow_secret_mutation_error(exc)
    except SecretDefaultRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "workflow_secret_rejected", "message": str(exc)},
        ) from exc
    _refresh_workflow_cache_after_commit()
    return {
        "status": "updated",
        "workflow_id": workflow_id,
        "revision": saved_revision,
        "validation": validation,
    }


@router.patch(
    "/{workflow_id}/toggle",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def toggle_workflow(workflow_id: str, request: Request):
    """Toggle a workflow's enabled state."""
    db = _get_db()
    scoped_workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if scoped_workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    try:
        new_state = toggle_workflow_enabled(
            db,
            workflow_id,
            authorize=_assert_workflow_mutation_allowed,
            validate=lambda current: _get_workflow_validation(
                db,
                workflow_id,
                account_id=_normalize_text(current.get("account_id")) or scope.account_id,
            ),
        )
    except WorkflowActivationValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="workflow",
                entity_id=workflow_id,
                action="enable",
                validation=exc.validation,
            ),
        ) from exc
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found") from e
    _get_registry().reload_all()
    return {"status": "toggled", "workflow_id": workflow_id, "enabled": new_state}


@router.delete(
    "/{workflow_id}",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def delete_workflow(workflow_id: str, request: Request):
    """Delete a workflow and all its phases/steps (CASCADE)."""
    db = _get_db()
    scoped_workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if scoped_workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    try:
        existing = scoped_workflow
        _assert_workflow_mutation_allowed(existing)
        if existing and not db.get_workflow(workflow_id):
            policy = _get_workflow_portability_service(db).export_policy_for_workflow(existing)
            if not policy.get("structure_change_allowed"):
                raise WorkflowPortabilityError(
                    "Installed extension workflows cannot be deleted.",
                    code="workflow_delete_protected",
                    status_code=403,
                )
        _get_workflow_portability_service(db).assert_delete_allowed(workflow_id)
        db.delete_workflow(workflow_id, author=scope.actor_name)
    except HTTPException:
        raise
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    except ServiceConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    _get_registry().reload_all()
    return {"status": "deleted", "workflow_id": workflow_id}


@router.post(
    "/{workflow_id}/clone",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def clone_workflow(workflow_id: str, body: WorkflowCloneRequest, request: Request):
    """Clone an existing workflow (including phases and steps)."""
    request_account_scope = _request_scope(request)
    require_default_registry_account(request_account_scope)
    db = _get_db()
    scoped_workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if scoped_workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    target_id = body.workflow_id or f"{workflow_id}_copy"
    if db.get_workflow(target_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow '{target_id}' already exists",
        )
    normalized_project_id = _enforce_project_scope(
        db,
        scope,
        body.project_id or getattr(scope, "project_id", None),
    )

    try:
        existing = scoped_workflow
        if existing and not db.get_workflow(workflow_id):
            policy = _get_workflow_portability_service(db).export_policy_for_workflow(existing)
            if not policy.get("clone_allowed"):
                raise WorkflowPortabilityError(
                    "Protected extension workflows cannot be cloned.",
                    code="workflow_clone_protected",
                    status_code=403,
                )
        _get_workflow_portability_service(db).assert_clone_allowed(workflow_id)
        from core.application.workflows.workflow_canvas_annotation_service import (
            WorkflowCanvasAnnotationService,
        )
        from core.entrypoints.admin.deps import get_artifact_plane

        with (
            WorkflowCanvasAnnotationService(db, get_artifact_plane()).clone_annotations(
                scoped_workflow, target_workflow_id=target_id, account_id=scope.account_id
            ) as annotations,
            db._transaction(),
        ):
            db.clone_workflow(
                source_workflow_id=workflow_id,
                target_workflow_id=target_id,
                display_name=body.display_name,
                description=body.description,
                account_id=scope.account_id,
                scope_kind="account",
                author=_request_scope(request).actor_name,
                **({"canvas_annotations": annotations} if annotations else {}),
            )
            _bind_workflow_to_project_if_requested(
                db,
                workflow_id=target_id,
                project_id=normalized_project_id,
            )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found") from e
    except WorkflowPortabilityError as e:
        _raise_portability_http_error(e)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    _get_registry().reload_all()
    return {
        "status": "cloned",
        "workflow_id": target_id,
        "source_workflow_id": workflow_id,
        "enabled": False,
        "message": "Cloned as disabled — enable it when it is ready.",
    }


__all__ = [
    "create_workflow",
    "update_workflow",
    "patch_workflow",
    "toggle_workflow",
    "delete_workflow",
    "clone_workflow",
]


# ---------------------------------------------------------------------------
# Secret input defaults
#
# A secret input's value is accepted here and nowhere else. It is encrypted on
# arrival and the workflow keeps only the reference key, so no later read of the
# workflow — an export, a reload, a job payload, a Test Run — has a value to
# leak. Everything on this path is deliberately one-way: there is no endpoint
# that returns a configured secret, only one that says whether one exists.
# ---------------------------------------------------------------------------


def _raise_workflow_secret_mutation_error(exc: WorkflowSecretMutationError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


@router.put(
    "/{workflow_id}/inputs/{field_name}/secret",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        428: {"description": "Revision required"},
    },
)
def set_workflow_input_secret(
    workflow_id: str, field_name: str, body: WorkflowInputSecretWrite, request: Request
):
    """Set or replace the configured value of a secret workflow input."""
    db = _get_db()
    scoped_workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if scoped_workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    _assert_workflow_mutation_allowed(scoped_workflow)
    account_id = _normalize_text(scoped_workflow.get("account_id")) or scope.account_id
    try:
        result = _workflow_secret_default_service(db).set_secret(
            workflow_id=workflow_id,
            field_name=_normalize_text(field_name),
            plaintext=body.value,
            account_id=account_id,
            author=scope.actor_name,
            expected_revision=body.expected_revision,
        )
    except WorkflowSecretMutationError as exc:
        _raise_workflow_secret_mutation_error(exc)
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except WorkflowConsumerConflictError as exc:
        _raise_consumer_conflict(exc)
    except SecretDefaultRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "workflow_secret_rejected", "message": str(exc)},
        ) from exc
    _refresh_workflow_cache_after_commit()
    return result


@router.delete(
    "/{workflow_id}/inputs/{field_name}/secret",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        428: {"description": "Revision required"},
    },
)
def clear_workflow_input_secret(
    workflow_id: str,
    field_name: str,
    request: Request,
    expected_revision: Annotated[int | None, Query()] = None,
):
    """Clear the configured value of a secret workflow input."""
    db = _get_db()
    scoped_workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if scoped_workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    _assert_workflow_mutation_allowed(scoped_workflow)
    account_id = _normalize_text(scoped_workflow.get("account_id")) or scope.account_id
    try:
        result = _workflow_secret_default_service(db).clear_secret(
            workflow_id=workflow_id,
            field_name=_normalize_text(field_name),
            account_id=account_id,
            author=scope.actor_name,
            expected_revision=expected_revision,
        )
    except WorkflowSecretMutationError as exc:
        _raise_workflow_secret_mutation_error(exc)
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except WorkflowConsumerConflictError as exc:
        _raise_consumer_conflict(exc)
    except SecretDefaultRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "workflow_secret_rejected", "message": str(exc)},
        ) from exc
    _refresh_workflow_cache_after_commit()
    return result


# ---------------------------------------------------------------------------
# A file input's test-run file. Attached once, copied into every test run that
# does not pass the input; real runs always pass their own file.
# ---------------------------------------------------------------------------

_SAMPLE_FILE_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Bad request"},
    403: {"description": "Forbidden"},
    404: {"description": "Not found"},
    409: {"description": "Conflict"},
    413: {"description": "File too large"},
    428: {"description": "Revision required"},
    503: {"description": "File storage unavailable"},
}


def _sample_file_scope(request: Request, workflow_id: str) -> tuple[Any, str, str]:
    db = _get_db()
    scoped_workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if scoped_workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    _assert_workflow_mutation_allowed(scoped_workflow)
    account_id = _normalize_text(scoped_workflow.get("account_id")) or scope.account_id
    return db, account_id, scope.actor_name


@router.put("/{workflow_id}/inputs/{field_name}/sample-file", responses=_SAMPLE_FILE_RESPONSES)
def set_workflow_input_sample_file(
    workflow_id: str,
    field_name: str,
    request: Request,
    file: Annotated[UploadFile, File(...)],
    expected_revision: Annotated[int | None, Form()] = None,
):
    """Attach or replace the test-run file of a file input."""
    db, account_id, author = _sample_file_scope(request, workflow_id)
    # One byte past the limit is enough to refuse it without reading the rest.
    body = file.file.read(workflow_test_file_max_bytes() + 1)
    try:
        result = _workflow_sample_file_service(db).set_sample_file(
            workflow_id=workflow_id,
            field_name=_normalize_text(field_name),
            account_id=account_id,
            filename=str(file.filename or ""),
            content_type=str(file.content_type or ""),
            body=body,
            author=author,
            expected_revision=expected_revision,
        )
    except WorkflowSampleFileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except WorkflowConsumerConflictError as exc:
        _raise_consumer_conflict(exc)
    _refresh_workflow_cache_after_commit()
    return result


@router.delete("/{workflow_id}/inputs/{field_name}/sample-file", responses=_SAMPLE_FILE_RESPONSES)
def clear_workflow_input_sample_file(
    workflow_id: str,
    field_name: str,
    request: Request,
    expected_revision: Annotated[int | None, Query()] = None,
):
    """Remove the test-run file of a file input."""
    db, account_id, author = _sample_file_scope(request, workflow_id)
    try:
        result = _workflow_sample_file_service(db).clear_sample_file(
            workflow_id=workflow_id,
            field_name=_normalize_text(field_name),
            account_id=account_id,
            author=author,
            expected_revision=expected_revision,
        )
    except WorkflowSampleFileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except WorkflowConsumerConflictError as exc:
        _raise_consumer_conflict(exc)
    _refresh_workflow_cache_after_commit()
    return result
