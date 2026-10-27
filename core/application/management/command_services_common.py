# ruff: noqa: F401
# mypy: ignore-errors
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import inspect
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from urllib.parse import unquote

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import (
    ResolvedPrincipal,
    principal_has_required_scopes,
    principal_type_has_minimum_role,
)
from core.application.auth.resolve import normalize_principal
from core.application.extensions.json_schema_subset import (
    validate_json_schema_value as _validate_json_schema_subset,
)
from core.application.extensions.semver import compare_semver, normalize_semver
from core.application.jobs.artifact_cleanup_runtime import (
    finalize_job_artifact_purge,
    purge_job_artifacts,
)
from core.application.jobs.workflow_wait_projection import DURABLE_WAIT_STATUSES
from core.application.management.scope import ScopeContext, normalize_scope, scope_audit_fields
from core.application.orchestration.agent_messages import build_agent_message
from core.application.orchestration.models import (
    ExternalAgentClaimedJobV1,
    ExternalAgentClaimRequestV1,
    ExternalAgentClaimResponseV1,
    ExternalAgentHeartbeatRequestV1,
    ExternalAgentHeartbeatResponseV1,
    ExternalAgentRegisterRequestV1,
    ExternalAgentRegisterResponseV1,
    ExternalAgentReportRequestV1,
    ExternalAgentReportResponseV1,
    build_event_envelope_v1,
)
from core.application.workflows.reaction_scopes import (
    merge_scoped_reaction_rules,
    serialize_scoped_reaction_rows,
)
from core.application.workflows.workflow_reaction_sources import (
    build_workflow_reaction_trigger_sources,
    validate_workflow_reaction_rules,
)
from core.application.workflows.workflow_start_runtime import (
    enqueue_workflow_start,
    normalize_entry_path,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db import model_provider_store, project_connection_store
from core.infrastructure.db.ingress_runtime import (
    build_ingress_dedupe_key,
    normalize_ingress_provider_event,
)
from core.infrastructure.extension_runtime.extension_invocation_audit import (
    log_host_extension_invocation,
)

from .command_models import (
    ApprovalDecisionInput,
    ApprovalResumeAcceptedView,
    CommandBatchInput,
    CommandBatchResultView,
    CommandDispatchInput,
    CommandDispatchResultView,
    EventRedriveResultView,
    ExtensionActionResultView,
    ExtensionArchiveInstallResultView,
    ExtensionArchiveUninstallResultView,
    ExtensionDescriptorImportFileInput,
    ExtensionDescriptorValidateInput,
    ExtensionEnableResultView,
    ExtensionInvocationResultView,
    ExtensionProjectCreateInput,
    ExtensionProjectCreateResultView,
    ExtensionQueryResultView,
    ExtensionReleaseInstallInput,
    ExtensionScanResultView,
    HardDeleteResultView,
    JobAcknowledgeInput,
    JobDecisionOverrideInput,
    JobDecisionOverrideResultView,
    JobDeleteResultView,
    JobLifecycleResultView,
    JobRetryInterruptedStepInput,
    JobStepReplayExecutionInput,
    JobStepReplayExecutionResultView,
    LifecyclePlanView,
    ProjectConnectionCreateInput,
    ProjectConnectionUpdateInput,
    ProjectCreateInput,
    ProjectDeleteResultView,
    ProjectJobsBulkDeleteInput,
    ProjectJobsBulkDeleteResultView,
    ProjectMutationResultView,
    ProjectNotificationDispatchInput,
    ProjectNotificationDispatchResultView,
    ProjectNotificationRoutesUpdateInput,
    ProjectNotificationRoutesUpdateResultView,
    ProjectPolicyResultView,
    ProjectPolicyUpdateInput,
    ProjectScheduledJobCreateInput,
    ProjectScheduledJobDeleteResultView,
    ProjectScheduledJobMutationResultView,
    ProjectScheduledJobRunResultView,
    ProjectScheduledJobToggleInput,
    ProjectScheduledJobUpdateInput,
    ProjectSettingsRestoreInput,
    ProjectSettingsRestoreResultView,
    ProjectTaskEnqueueInput,
    ProjectTaskEnqueueResultView,
    ProjectUpdateInput,
    ProjectWorkflowBindingsInput,
    ProjectWorkflowBindingsResultView,
    ProviderEventIngestInput,
    ProviderEventIngestResultView,
    ProviderWebhookFileInput,
    ProviderWebhookIngestInput,
    ProviderWebhookIngestResultView,
    ReactionEventIngestInput,
    ReactionEventIngestResultView,
    RestoreResultView,
    ToolCloneInput,
    ToolInvocationInput,
    ToolInvocationResultView,
    ToolMutationResultView,
    ToolPatchInput,
    ToolRestoreRevisionInput,
    ToolSaveInput,
    TrashResultView,
    WorkflowReactionsUpdateInput,
    WorkflowRunInput,
)
from .errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
    safe_operator_exception,
)
from .guards import (
    ACCESS_DENIED_MESSAGE,
    require_account_access,
    require_default_workflow_catalog_account,
    require_event_access,
    require_project_access,
    require_workflow_access,
)
from .query_models import ProjectConnectionView, WorkflowReactionRuleView, WorkflowReactionsView

CANCELLED_BY_USER_VALUE = "Cancelled by user"
EXTERNAL_COMMENT_RECEIVED_VALUE = "external.comment.received"
PROJECT_NOT_FOUND_MESSAGE = "Project not found"
SCHEDULED_JOB_NOT_FOUND_MESSAGE = "Scheduled job not found."
TARGET_JOB_ID_VALUE = "target.job_id"
UPLOAD_BIN_VALUE = "upload.bin"
WORKSPACE_NOT_FOUND_MESSAGE = "workspace not found"


logger = logging.getLogger(__name__)


MIME_APPLICATION_OCTET_STREAM = "application/octet-stream"
ERR_DATABASE_NOT_CONFIGURED = "Database not configured"
ERR_PROJECT_ID_REQUIRED = "project_id is required"
ERR_PROJECT_NOT_FOUND = PROJECT_NOT_FOUND_MESSAGE
ERR_JOB_NOT_FOUND = "Job not found"
ERR_PROJECT_LEASE_UNAVAILABLE = "Project lease storage is unavailable"
ERR_SCHEDULED_JOBS_SCHEMA_UNAVAILABLE = "Scheduled jobs are not available in this database schema."
ERR_WORKFLOW_RESOLUTION_UNAVAILABLE = "Workflow resolution is not available"
ERR_EXTENSION_NOT_FOUND = "Extension not found"
ERR_EXTENSION_DISABLED = "Extension is disabled"
SQL_UPDATE_PREFIX = "UPDATE "
SQL_WHERE_PREFIX = " WHERE "
_JOB_DELETE_ACTIVE_STATUSES = {
    "pending",
    "queued",
    "resume_pending",
    "in_progress",
    "stuck",
    # A run paused on an approval, a timer or a child workflow is not finished.
    *DURABLE_WAIT_STATUSES,
}
_JOB_DELETE_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
}


def _job_delete_blocker(
    job: dict[str, Any] | None,
    *,
    allow_quiescing: bool = False,
) -> tuple[str, str] | None:
    payload = job or {}
    raw_status = str(payload.get("status") or "").strip()
    normalized_status = raw_status.lower()
    status_label = raw_status or "unknown"
    if normalized_status in _JOB_DELETE_ACTIVE_STATUSES:
        return "active", status_label
    if normalized_status in _JOB_DELETE_TERMINAL_STATUSES:
        if allow_quiescing:
            return None
        if payload.get("finished_at"):
            return None
        return "quiescing", status_label
    if payload.get("finished_at"):
        return None
    return "non_terminal", status_label


def _job_delete_conflict_message(
    job_id: Any,
    job: dict[str, Any] | None,
    *,
    action: str = "delete",
    allow_quiescing: bool = False,
) -> str | None:
    blocker = _job_delete_blocker(job, allow_quiescing=allow_quiescing)
    if blocker is None:
        return None

    blocker_type, status_label = blocker
    if blocker_type == "active":
        return f"Cannot {action} active job #{job_id} (status={status_label})"
    if blocker_type == "quiescing":
        return f"Cannot {action} job #{job_id} before shutdown completes (status={status_label})"
    return f"Cannot {action} non-terminal job #{job_id} (status={status_label})"


def _is_attachment_storage_misconfigured(exc: Exception) -> bool:
    message = str(exc or "").strip().lower()
    if not message:
        return False
    return (
        "fs_secret_key is required for encrypted attachment storage" in message
        or "cryptography/aesgcm is required for encrypted attachment storage" in message
    )


# A storage backend that is configured but unusable fails deep inside the SDK, so the
# operator sees a bare 500 unless the cause is recognised here and named.
_ARTIFACT_STORAGE_EXCEPTIONS = frozenset(
    {
        "NoCredentialsError",
        "PartialCredentialsError",
        "NoRegionError",
        "InvalidRegionError",
        "EndpointConnectionError",
    }
)
_ARTIFACT_STORAGE_MARKERS = (
    "unable to locate credentials",
    "you must specify a region",
    "invalid endpoint",
    "could not connect to the endpoint url",
    "the aws access key id you provided does not exist",
    "invalidaccesskeyid",
    "signaturedoesnotmatch",
)


def _workflow_not_runnable_error(exc: BaseException) -> ServiceConflictError | None:
    """A definition that cannot run, as a conflict the caller can fix, or None.

    The pin refuses it with its issues; that is not an internal error.
    """
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionPinError,
    )

    if not isinstance(exc, WorkflowDefinitionPinError):
        return None
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowRevisionChangedError,
    )

    if isinstance(exc, WorkflowRevisionChangedError):
        return ServiceConflictError(
            str(exc),
            payload={
                "error": str(exc),
                "code": "workflow_revision_conflict",
                "workflow_id": exc.workflow_id,
                "expected_revision": exc.expected_revision,
                "revision": exc.revision,
            },
        )
    return ServiceConflictError(
        str(exc),
        payload={"error": str(exc), "code": "workflow_not_runnable", "issues": list(exc.issues)},
    )


def _is_artifact_storage_misconfigured(exc: BaseException | None) -> bool:
    """Detect an artifact backend whose credentials or endpoint cannot work."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _ARTIFACT_STORAGE_EXCEPTIONS:
            return True
        message = str(current or "").strip().lower()
        if any(marker in message for marker in _ARTIFACT_STORAGE_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def artifact_storage_misconfigured_message() -> str:
    """Name the configured backend and the supported way to correct it."""
    from core.infrastructure.config.runtime_env import runtime_env

    backend = str(runtime_env.get("ARTIFACT_STORAGE_BACKEND", "") or "").strip() or "unset"
    return (
        f"Artifact storage is configured as ARTIFACT_STORAGE_BACKEND={backend} but it cannot "
        "be used: its credentials or endpoint are missing or rejected. If this installation "
        "stores artifacts on local disk, correct the backend with "
        "'flow-steward config set --file-storage local_fs' followed by "
        "'flow-steward config apply --confirm'; for S3, supply the endpoint, region and "
        "credentials. No task is queued until artifact storage works."
    )


def _is_invalid_uuid_lookup_error(exc: Exception) -> bool:
    return "invalid input syntax for type uuid" in str(exc or "").strip().lower()


def _is_foreign_key_violation(exc: Exception) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if str(getattr(current, "sqlstate", "") or "").strip() == "23503":
            return True
        current = current.__cause__ or current.__context__
    return False


def _service_scope(
    scope: ScopeContext | None,
    *,
    project_id: Any = None,
    workflow_id: Any = None,
) -> ScopeContext:
    return normalize_scope(scope, project_id=project_id, workflow_id=workflow_id)


def _scope_audit_fields(scope: ScopeContext | None) -> dict[str, Any]:
    return scope_audit_fields(scope)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _attachment_filename_is_safe(filename: str) -> bool:
    normalized = str(filename or "").strip()
    if not normalized:
        return False
    candidates = {normalized, unquote(normalized)}
    for candidate in candidates:
        if not candidate or "\x00" in candidate:
            return False
        if os.path.basename(candidate) != candidate:
            return False
        if "/" in candidate or "\\" in candidate:
            return False
    return True


def _attachment_type_allowed(
    *,
    filename: str,
    mime_type: str,
    allowed_extensions: set[str],
    allowed_mimes: set[str],
) -> bool:
    normalized_mime = str(mime_type or MIME_APPLICATION_OCTET_STREAM).strip().lower()
    extension = os.path.splitext(str(filename or "").strip())[1].lower()

    if allowed_extensions:
        if not extension or extension not in allowed_extensions:
            return False
        if allowed_mimes and normalized_mime != MIME_APPLICATION_OCTET_STREAM:
            return normalized_mime in allowed_mimes
        return True

    if allowed_mimes:
        return normalized_mime in allowed_mimes
    return True


def _resolve_artifact_plane(artifact_plane: Any) -> Any | None:
    if artifact_plane is None:
        return None
    if callable(artifact_plane):
        try:
            return artifact_plane()
        except Exception:
            return None
    return artifact_plane


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _required_entity_id(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ServiceValidationError(f"{field_name} is required")
    return normalized


def _normalize_file_ref_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row or {}) if isinstance(row, dict) else {}
    source_ref = str(payload.get("source_ref") or "").strip()
    return {
        "artifact_id": str(payload.get("artifact_id") or "").strip(),
        "filename": str(payload.get("filename") or "").strip() or UPLOAD_BIN_VALUE,
        "content_type": str(payload.get("content_type") or MIME_APPLICATION_OCTET_STREAM).strip()
        or MIME_APPLICATION_OCTET_STREAM,
        "size_bytes": _coerce_int(payload.get("size_bytes"), 0),
        "sha256": str(payload.get("sha256") or "").strip(),
        "object_key": str(payload.get("object_key") or "").strip(),
        "source_ref": source_ref,
        "retention_class": str(payload.get("retention_class") or "run_window").strip()
        or "run_window",
    }


def _persist_file_refs(
    *,
    _enqueue_logger: Any,
    job_id: Any,
    file_rows: list[dict[str, Any]],
    artifact_plane: Any | None,
    artifact_owner_scope: dict[str, Any],
    artifact_run_id: str,
    artifact_step_id: str,
    source_ref_prefix: str,
    lease_heartbeat: Callable[[], None] | None = None,
    on_artifact_created: Callable[[dict[str, str]], None] | None = None,
) -> list[dict[str, Any]]:
    plane = _resolve_artifact_plane(artifact_plane)
    if plane is None:
        return []

    artifact_refs: list[dict[str, Any]] = []
    created_artifact_ids: list[str] = []
    try:
        for index, file_row in enumerate(file_rows):
            filename = str(file_row.get("filename") or UPLOAD_BIN_VALUE).strip() or UPLOAD_BIN_VALUE
            content_type = (
                str(file_row.get("mime_type") or MIME_APPLICATION_OCTET_STREAM).strip()
                or MIME_APPLICATION_OCTET_STREAM
            )
            staged_path = str(file_row.get("content_path") or "").strip()
            expected_size = int(file_row.get("size_bytes") or 0)
            expected_sha = str(file_row.get("sha256") or "").strip().lower()
            reserved_artifact_id = f"art_{uuid.uuid4().hex[:24]}"
            created_artifact_ids.append(reserved_artifact_id)
            if on_artifact_created is not None:
                on_artifact_created(
                    {
                        "artifact_id": reserved_artifact_id,
                        "object_key": f"artifacts/{reserved_artifact_id}/{filename}",
                    }
                )
            if staged_path:

                def _staged_chunks(path: str = staged_path):
                    with open(path, "rb") as staged_file:
                        while True:
                            if lease_heartbeat is not None:
                                lease_heartbeat()
                            chunk = staged_file.read(1024 * 1024)
                            if not chunk:
                                break
                            yield chunk

                handle, meta = plane.create_artifact_from_stream(
                    artifact_id=reserved_artifact_id,
                    owner_scope=dict(artifact_owner_scope or {}),
                    run_id=str(artifact_run_id or "").strip() or f"job:{job_id!s}",
                    step_id=str(artifact_step_id or "manual_upload").strip() or "manual_upload",
                    filename=filename,
                    content_type=content_type,
                    retention_class="run_window",
                    chunks=_staged_chunks(),
                    max_size_bytes=max(1, expected_size),
                    allow_in_memory=(
                        str(artifact_owner_scope.get("entry_path") or "").strip() == "manual_ui"
                    ),
                )
            else:
                if lease_heartbeat is not None:
                    lease_heartbeat()
                body = bytes(file_row.get("content") or b"")
                expected_size = len(body)
                expected_sha = hashlib.sha256(body).hexdigest()
                handle, meta = plane.create_artifact(
                    artifact_id=reserved_artifact_id,
                    owner_scope=dict(artifact_owner_scope or {}),
                    run_id=str(artifact_run_id or "").strip() or f"job:{job_id!s}",
                    step_id=str(artifact_step_id or "manual_upload").strip() or "manual_upload",
                    filename=filename,
                    content_type=content_type,
                    retention_class="run_window",
                    body=body,
                )
            if lease_heartbeat is not None:
                lease_heartbeat()
            artifact_id = str(handle.artifact_id)
            if artifact_id != reserved_artifact_id:
                raise RuntimeError("artifact_identity_error")
            stored_size = meta.get("size_bytes")
            try:
                actual_size = int(stored_size) if stored_size is not None else -1
            except (TypeError, ValueError):
                actual_size = -1
            if (staged_path or file_row.get("input_name")) and (
                actual_size != expected_size
                or str(meta.get("sha256") or "").strip().lower() != expected_sha
            ):
                raise RuntimeError("artifact_integrity_error")
            artifact_refs.append(
                {
                    "artifact_id": artifact_id,
                    "filename": str(
                        meta.get("filename") or file_row.get("filename") or UPLOAD_BIN_VALUE
                    ),
                    "content_type": str(
                        meta.get("content_type")
                        or file_row.get("mime_type")
                        or MIME_APPLICATION_OCTET_STREAM
                    ),
                    "size_bytes": _coerce_int(meta.get("size_bytes"), expected_size),
                    "sha256": str(meta.get("sha256") or "").strip(),
                    "object_key": str(meta.get("object_key") or "").strip(),
                    "source_ref": f"{str(source_ref_prefix or 'file').strip()}:{index}",
                    "retention_class": str(meta.get("retention_class") or "run_window").strip()
                    or "run_window",
                }
            )
    except Exception:
        delete_artifact = getattr(plane, "delete_artifact", None)
        if callable(delete_artifact):
            for artifact_id in created_artifact_ids:
                with contextlib.suppress(Exception):
                    delete_artifact(artifact_id)
        raise
    return artifact_refs


def _json_schema_type_matches(expected_type: str, value: Any) -> bool:
    normalized_type = str(expected_type or "").strip().lower()
    if normalized_type == "object":
        return isinstance(value, dict)
    if normalized_type == "array":
        return isinstance(value, list)
    if normalized_type == "string":
        return isinstance(value, str)
    if normalized_type == "boolean":
        return isinstance(value, bool)
    if normalized_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized_type == "null":
        return value is None
    return True


def _json_schema_validate_value(
    *,
    schema: dict[str, Any],
    value: Any,
    path: str,
    errors: list[str],
) -> None:
    """Delegate to the single executed-subset validator.

    This was a near-identical copy of the one in extensions.lifecycle_ports and
    had already drifted from it. Two validators for one contract means the
    contract is whichever one you happened to call.
    """
    _validate_json_schema_subset(schema=schema, value=value, path=path, errors=errors)


def _require_role(scope: ScopeContext, minimum_role: str) -> None:
    """Role gate based on explicit principal-type hierarchy."""
    ptype = str(scope.principal_type or "").strip().lower()
    required = str(minimum_role or "").strip().lower()
    if required not in {"viewer", "operator", "admin"}:
        raise ServiceForbiddenError(f"Unknown role requirement: {minimum_role}")
    if not ptype:
        raise ServiceForbiddenError("Authentication required")
    if principal_type_has_minimum_role(ptype, required):
        return
    if required == "admin":
        raise ServiceForbiddenError("Admin role required")
    if required == "operator":
        raise ServiceForbiddenError("Operator role required")
    raise ServiceForbiddenError("Viewer role required")


def _require_global_extension_lifecycle_scope(scope: ScopeContext) -> None:
    _require_role(scope, "admin")
    normalized_account_id = (
        str(scope.account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    )
    if normalized_account_id != DEFAULT_ACCOUNT_ID:
        raise ServiceValidationError(
            "Extension install/uninstall is deployment-scoped and is only supported for the default account"
        )


def _external_agent_principal(
    principal: ResolvedPrincipal | dict[str, Any] | None,
    *,
    required_scope: str,
) -> ResolvedPrincipal:
    resolved = normalize_principal(principal)
    if not isinstance(resolved, ResolvedPrincipal) or not bool(resolved.is_active):
        raise ServiceForbiddenError("Authentication required")
    if str(resolved.principal_type or "").strip().lower() != "service_account":
        raise ServiceForbiddenError("Service account principal required")
    if not principal_has_required_scopes(resolved.model_dump(), [required_scope], require_all=True):
        raise ServiceForbiddenError(f"Missing required scope '{required_scope}'")
    return resolved


def _authoritative_scope(
    scope: ScopeContext | None,
    principal: ResolvedPrincipal,
    *,
    project_id: Any = None,
    workflow_id: Any = None,
) -> ScopeContext:
    return normalize_scope(
        scope,
        account_id=principal.account_id,
        project_id=project_id,
        workflow_id=workflow_id,
        actor_id=str(
            principal.service_account_key
            or principal.username
            or principal.display_name
            or "service_account"
        ).strip()
        or "service_account",
        principal_type=principal.principal_type,
        allowed_project_ids=frozenset(principal.allowed_project_ids or []),
        allowed_workflow_ids=frozenset(principal.allowed_workflow_ids or []),
    )


def _process_extension_response_envelope(
    invocation: dict[str, Any],
    extension: dict[str, Any],
    extension_id: str,
    *,
    scope: ScopeContext | None = None,
    audit: dict[str, Any] | None = None,
    bl: Any = None,
    get_logger: Callable[[], Any] | None = None,
    event_command_service_factory: Callable[[], Any] | None = None,
) -> None:
    """Process commands[] and events[] from a normalized extension response.

    Called after a successful extension invocation. Normalizes the raw subprocess
    response into v1.1 canonical envelope, dispatches valid commands, and emits
    valid events through the domain event ledger.

    Mutates *invocation* by adding ``_envelope`` key with the normalized shape.
    """
    raw_response = invocation.get("response")
    if not isinstance(raw_response, dict) or not raw_response:
        return

    from core.infrastructure.db.extension_response import (
        filter_valid_commands,
        filter_valid_events,
        normalize_extension_response,
    )

    env = normalize_extension_response(raw_response)

    # Attach normalized envelope to invocation for callers
    invocation["_envelope"] = env.model_dump(mode="json")

    from core.application.extensions.response_envelope import process_project_state_updates

    audit = dict(audit or {})
    process_project_state_updates(
        invocation,
        extension=extension,
        extension_id=extension_id,
        db=bl,
        account_id=str(
            audit.get("account_id") or getattr(scope, "account_id", None) or DEFAULT_ACCOUNT_ID
        ).strip()
        or DEFAULT_ACCOUNT_ID,
        project_id=str(audit.get("project_id") or getattr(scope, "project_id", None) or "").strip(),
        actor_id=str(audit.get("actor_id") or getattr(scope, "actor_id", None) or "").strip()
        or None,
        connection_id=str(audit.get("connection_id") or "").strip() or None,
    )
    if invocation.get("status") == "failed":
        return

    compiled = extension.get("compiled_manifest") or {}
    supported_events = list(
        compiled.get("supported_events") or extension.get("supported_events") or []
    )

    # Process commands through unified dispatch
    if env.commands:
        from core.infrastructure.extension_runtime.extension_runtime import command_scope

        # An extension may only dispatch host commands whose command-scope it
        # declared in required_scopes (what the operator approved at install).
        declared_scopes = _extension_declared_scopes(extension)
        valid_commands = filter_valid_commands(list(env.commands))
        for cmd in valid_commands:
            command_type = str(cmd.get("command_type") or "").strip()
            needed_scope = command_scope(command_type)
            if needed_scope and needed_scope not in declared_scopes:
                logger.warning(
                    "Extension %s attempted command '%s' without declared scope '%s' — skipped",
                    extension_id,
                    command_type,
                    needed_scope,
                )
                continue
            try:
                from .command_models import CommandDispatchInput
                from .command_services_dispatch import CommandDispatchService

                cmd_svc = CommandDispatchService(
                    get_logger=get_logger or (lambda: bl),
                    event_command_service_factory=event_command_service_factory,
                )
                cmd_svc.dispatch(
                    CommandDispatchInput(
                        command_type=str(cmd.get("command_type") or "").strip(),
                        target=dict(cmd.get("target") or {}),
                        payload=dict(cmd.get("payload") or {}),
                        source=f"extension:{extension_id}",
                        correlation_id=str(cmd.get("correlation_id") or "").strip(),
                    ),
                    scope=scope,
                )
            except Exception as exc:
                logger.warning(
                    "Extension command dispatch failed: ext=%s cmd_type=%s error=%s",
                    extension_id,
                    cmd.get("command_type"),
                    exc,
                )

    # Process events through domain event ledger
    if env.events:
        from core.infrastructure.extension_runtime.extension_runtime import emit_extension_event

        valid_events = filter_valid_events(list(env.events), declared_event_types=supported_events)
        runtime_ctx = {**audit}
        if bl is not None:
            runtime_ctx["logger"] = bl
        for evt in valid_events:
            try:
                if _route_extension_event_through_event_command_service(
                    extension_id=extension_id,
                    event=evt,
                    scope=scope,
                    runtime_context=runtime_ctx,
                    event_command_service_factory=event_command_service_factory,
                ):
                    continue
                emit_extension_event(
                    extension,
                    event_type=str(evt.get("event_type") or "").strip(),
                    correlation_id=str(evt.get("correlation_id") or "").strip(),
                    scope=scope,
                    project_id=str(audit.get("project_id") or "").strip() or None,
                    summary=str(evt.get("summary") or "").strip(),
                    severity=str(evt.get("severity") or "info").strip(),
                    payload=dict(evt.get("payload") or {}),
                    runtime_context=runtime_ctx,
                )
            except Exception as exc:
                logger.warning(
                    "Extension event emission failed: ext=%s evt_type=%s error=%s",
                    extension_id,
                    evt.get("event_type"),
                    exc,
                )


def _route_extension_event_through_event_command_service(
    *,
    extension_id: str,
    event: dict[str, Any],
    scope: ScopeContext | None,
    runtime_context: dict[str, Any] | None = None,
    event_command_service_factory: Callable[[], Any] | None = None,
) -> bool:
    event_payload = dict(event or {})
    project_id = str(
        (runtime_context or {}).get("project_id") or getattr(scope, "project_id", None) or ""
    ).strip()
    if not project_id:
        return False
    event_payload["project_id"] = project_id

    if event_command_service_factory is None:
        return False

    event_command_service = event_command_service_factory()
    if event_command_service is None or not hasattr(
        event_command_service, "ingest_provider_events"
    ):
        return False

    event_scope = normalize_scope(
        scope,
        project_id=project_id,
        workflow_id=str(event_payload.get("workflow_id") or "").strip() or None,
    )
    event_command_service.ingest_provider_events(
        ProviderEventIngestInput(
            provider_key=str(extension_id or "").strip().lower(),
            project_id=project_id,
            result={},
            events=[event_payload],
        ),
        scope=event_scope,
    )
    return True


# ---------------------------------------------------------------------------
# Trash lifecycle service
# ---------------------------------------------------------------------------

_TRASH_ENTITY_CONFIG: dict[str, dict[str, Any]] = {
    "workspace": {
        "table": "workspaces",
        "id_col": "id",
    },
    "project": {
        "table": "projects",
        "id_col": "id",
    },
    "job": {
        "table": "jobs",
        "id_col": "id",
    },
    "user": {
        "table": "users",
        "id_col": "id",
    },
    "registry_agent": {
        "table": "registry_agents",
        "id_col": "agent_id",
    },
    "registry_tool": {
        "table": "registry_tools",
        "id_col": "tool_id",
    },
    "mcp_connector": {
        "table": "mcp_connectors",
        "id_col": "connector_id",
    },
    "registry_skill": {
        "table": "registry_skills",
        "id_col": "skill_id",
    },
    "registry_workflow": {
        "table": "registry_workflows",
        "id_col": "workflow_id",
    },
    "registry_guardrail": {
        "table": "registry_guardrails",
        "id_col": "id",
        "deployment_global": True,
    },
    "registry_data_contract": {
        "table": "registry_data_contracts",
        "id_col": "contract_id",
        "deployment_global": True,
    },
    "dataset": {
        "table": "registry_datasets",
        "id_col": "dataset_id",
    },
    "workspace_data_connection": {
        "table": "workspace_data_connections",
        "id_col": "connection_id",
    },
    "project_settings_revision": {
        "table": "project_settings_revisions",
        "id_col": "id",
    },
}

_PROJECT_CONNECTION_LIFECYCLE_DELETED = "__lifecycle_deleted__"


# ---------------------------------------------------------------------------
# Model provider command service (self-hosted BYOK)
# ---------------------------------------------------------------------------

_SELF_HOSTED_ENV = "SELF_HOSTED_MODE"


def _self_hosted_mode_enabled() -> bool:
    return _runtime_env.get(_SELF_HOSTED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _extension_declared_scopes(extension: Any) -> set[str]:
    """Declared (operator-approved) scope envelope for an extension.

    Self-contained extraction (extension.required_scopes + compiled_manifest
    .required_scopes) that avoids the wildcard-import helpers used by
    ``extension_runtime_entrypoints.extension_required_scopes``.
    """
    ext = extension if isinstance(extension, dict) else {}
    manifest = ext.get("compiled_manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    scopes: set[str] = set()
    for source in (ext.get("required_scopes"), manifest.get("required_scopes")):
        if isinstance(source, (list, tuple, set)):
            scopes.update(str(item).strip() for item in source if str(item or "").strip())
    return scopes


def _extension_operation_declared_scopes(
    extension: Any, *, operation_type: str, operation_id: str
) -> set[str]:
    """Declared required_scopes for one query/action operation of *extension*."""
    op_id = str(operation_id or "").strip()
    op_type = str(operation_type or "").strip().lower()
    if not op_id or op_type not in {"query", "action"}:
        return set()
    ext = extension if isinstance(extension, dict) else {}
    manifest = ext.get("compiled_manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    key = "query_definitions" if op_type == "query" else "action_definitions"
    id_field = "query_id" if op_type == "query" else "action_id"
    rows: list[dict] = []
    for source in (ext.get(key), manifest.get(key)):
        if isinstance(source, (list, tuple)):
            rows.extend(row for row in source if isinstance(row, dict))
    scopes: set[str] = set()
    for row in rows:
        row_id = str(row.get(id_field) or row.get("id") or "").strip()
        if row_id != op_id:
            continue
        source = row.get("required_scopes")
        if isinstance(source, (list, tuple, set)):
            scopes.update(str(item).strip() for item in source if str(item or "").strip())
    return scopes


__all__ = [name for name in globals() if not name.startswith("__")]
