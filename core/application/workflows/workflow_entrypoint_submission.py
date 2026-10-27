"""Validated workflow-entrypoint submission and dispatch coordination."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from core.application.management.errors import safe_operator_exception
from core.application.workflows.workflow_entrypoint_idempotency_service import (
    WorkflowEntrypointIdempotencyService,
)
from core.application.workflows.workflow_entrypoint_input_service import (
    WorkflowEntrypointMappingError,
)
from core.application.workflows.workflow_entrypoint_launch import WorkflowEntrypointLaunchService
from core.application.workflows.workflow_entrypoint_models import (
    WorkflowEntrypointFailureKind,
    WorkflowEntrypointIdempotencyResolution,
)


def _canonicalize_submission_value(value: Any) -> Any:
    from core.infrastructure.security.secret_reference import as_secret_reference

    reference = as_secret_reference(value)
    if reference is not None:
        from core.infrastructure.security.secret_codec import (
            decrypt_secret,
            secret_value_fingerprint,
        )

        identity = (
            secret_value_fingerprint(decrypt_secret(reference.ciphertext))
            if reference.is_inline
            else secret_value_fingerprint(reference.ref_key)
        )
        return {"__type__": "workflow-secret-fingerprint.v1", "identity": identity}
    if isinstance(value, dict):
        return {
            str(key): _canonicalize_submission_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize_submission_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _staged_file_descriptors(staged_files: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "source_ref": "multipart:files",
            "filename": str(getattr(item, "filename", "") or ""),
            "content_type": str(getattr(item, "mime_type", "") or ""),
            "size_bytes": int(getattr(item, "size_bytes", 0) or 0),
            "sha256": str(getattr(item, "sha256", "") or "").strip().lower(),
        }
        for item in staged_files
    ]


def _submission_fingerprint(
    *,
    entry_key: str,
    workflow_id: str,
    workflow_inputs: dict[str, Any],
    file_descriptors: list[dict[str, Any]],
) -> str:
    canonical_files = sorted(
        (_canonicalize_submission_value(item) for item in file_descriptors),
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
    effective_inputs = dict(workflow_inputs)
    effective_inputs["files"] = canonical_files
    canonical = {
        "entry_key": str(entry_key or "").strip(),
        "workflow_id": str(workflow_id or "").strip(),
        "workflow_inputs": _canonicalize_submission_value(effective_inputs),
        "attachments": [
            {
                "source_ref": str(item.get("source_ref") or ""),
                "size_bytes": int(item.get("size_bytes") or 0),
                "sha256": str(item.get("sha256") or "").lower(),
            }
            for item in canonical_files
        ],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "workflow-entrypoint:v2:" + hashlib.sha256(encoded).hexdigest()


def _extract_file_path(value: Any, path: str) -> Any:
    current = value
    for segment in (part for part in str(path or "").split(".") if part):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


@dataclass(frozen=True)
class WorkflowEntrypointSubmissionOutcome:
    job_id: str = ""
    task_summary: str = ""
    failure_kind: WorkflowEntrypointFailureKind | None = None
    code: str = ""
    error: str = ""
    operator_error: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    replay_job_id: str = ""
    deduplicated: bool = False
    recoverable_job_id: str = ""
    file_count: int = 0
    request_fingerprint: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.job_id) and not self.failure_kind


@dataclass(frozen=True)
class _ValidatedWorkflowSubmission:
    inputs: dict[str, Any]
    interface: dict[str, Any]
    file_descriptors: list[dict[str, Any]]
    request_fingerprint: str


@dataclass
class _IdempotencyOwnership:
    service: WorkflowEntrypointIdempotencyService
    owned_keys: list[str] = field(default_factory=list)
    identities: list[dict[str, str]] = field(default_factory=list)
    primary_identity: dict[str, str] = field(default_factory=dict)


class WorkflowEntrypointSubmissionUseCase:
    """Own validated public submission from staged descriptors through dispatch."""

    def __init__(
        self,
        *,
        build_inputs: Callable[..., dict[str, Any]],
        validate_inputs: Callable[..., Any],
        idempotency_service_factory: Callable[[str], WorkflowEntrypointIdempotencyService],
        artifact_lifecycle_factory: Callable[[str, list[str]], Any],
        launch_service: WorkflowEntrypointLaunchService,
    ) -> None:
        self._build_inputs = build_inputs
        self._validate_inputs = validate_inputs
        self._idempotency_service_factory = idempotency_service_factory
        self._artifact_lifecycle_factory = artifact_lifecycle_factory
        self._launch_service = launch_service

    @staticmethod
    def _failure(
        *,
        failure_kind: WorkflowEntrypointFailureKind,
        code: str,
        error: str,
        exc: BaseException | None = None,
        errors: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> WorkflowEntrypointSubmissionOutcome:
        return WorkflowEntrypointSubmissionOutcome(
            failure_kind=failure_kind,
            code=code,
            error=error,
            operator_error=safe_operator_exception(exc) if exc is not None else "",
            errors=list(errors or []),
            **kwargs,
        )

    def _prepare_submission(
        self,
        *,
        entry_key: str,
        workflow_id: str,
        mapping: dict[str, Any],
        sources: dict[str, Any],
        fallback_body: Any,
        staged_files: list[Any],
    ) -> _ValidatedWorkflowSubmission | WorkflowEntrypointSubmissionOutcome:
        file_descriptors = _staged_file_descriptors(staged_files)
        staged_sources = {**sources, "files": file_descriptors}
        try:
            workflow_inputs = self._build_inputs(
                workflow_id=workflow_id,
                mapping=mapping,
                sources=staged_sources,
                fallback_body=fallback_body,
                file_refs=file_descriptors,
            )
        except Exception as exc:
            mapping_error = isinstance(exc, WorkflowEntrypointMappingError)
            return self._failure(
                failure_kind="invalid_request" if mapping_error else "unavailable",
                code="mapping_failed" if mapping_error else "workflow_contract_unavailable",
                error=(
                    "Invalid workflow input mapping"
                    if mapping_error
                    else "Workflow contract service is unavailable; retry later"
                ),
                exc=exc,
            )
        try:
            validation = self._validate_inputs(workflow_id, workflow_inputs, channel="webhook")
        except Exception as exc:
            return self._failure(
                failure_kind="unavailable",
                code="workflow_contract_unavailable",
                error="Workflow contract service is unavailable; retry later",
                exc=exc,
            )
        if not bool(validation.ok):
            return self._failure(
                failure_kind="invalid_request",
                code="workflow_input_validation_failed",
                error="Workflow input validation failed",
                errors=list(validation.errors or []),
            )
        interface = dict(validation.interface or {})
        if bool(interface.get("enabled")):
            workflow_inputs = dict(validation.inputs)
        return _ValidatedWorkflowSubmission(
            inputs=workflow_inputs,
            interface=interface,
            file_descriptors=file_descriptors,
            request_fingerprint=_submission_fingerprint(
                entry_key=entry_key,
                workflow_id=workflow_id,
                workflow_inputs=workflow_inputs,
                file_descriptors=file_descriptors,
            ),
        )

    def _resolve_idempotency(
        self,
        *,
        mode: str,
        header_key: str,
        replay_key: str,
        request_fingerprint: str,
    ) -> _IdempotencyOwnership | WorkflowEntrypointSubmissionOutcome:
        ownership = _IdempotencyOwnership(
            service=self._idempotency_service_factory(request_fingerprint)
        )
        if mode == "header":
            if not str(header_key or "").strip():
                return self._failure(
                    failure_kind="invalid_request",
                    code="idempotency_key_required",
                    error="Idempotency-Key header is required for this workflow entrypoint",
                    request_fingerprint=request_fingerprint,
                )
            resolution = ownership.service.resolve(header_key)
            resolved = self._apply_idempotency_resolution(
                ownership,
                key=header_key,
                resolution=resolution,
                request_fingerprint=request_fingerprint,
                replay=False,
            )
            if resolved is not None:
                return resolved
        if replay_key:
            resolution = ownership.service.resolve(replay_key)
            resolved = self._apply_idempotency_resolution(
                ownership,
                key=replay_key,
                resolution=resolution,
                request_fingerprint=request_fingerprint,
                replay=True,
            )
            if resolved is not None:
                return resolved
        return ownership

    def _apply_idempotency_resolution(
        self,
        ownership: _IdempotencyOwnership,
        *,
        key: str,
        resolution: WorkflowEntrypointIdempotencyResolution,
        request_fingerprint: str,
        replay: bool,
    ) -> WorkflowEntrypointSubmissionOutcome | None:
        if resolution.failure_kind:
            failure_code = (
                "replay_payload_mismatch"
                if replay and resolution.code == "idempotency_payload_mismatch"
                else resolution.code or "replay_reservation_failed"
            )
            self._fail_owned_keys(ownership, failure_code)
            error_message = "Webhook replay protection failed; retry later"
            if replay and resolution.code == "idempotency_payload_mismatch":
                error_message = "Replay key was used with a different payload"
            elif not replay:
                error_message = resolution.message
            return self._failure(
                failure_kind=resolution.failure_kind,
                code=resolution.code,
                error=error_message,
                replay_job_id=resolution.replay_job_id,
                request_fingerprint=request_fingerprint,
            )
        if resolution.replay_job_id:
            for owned_key in ownership.owned_keys:
                with suppress(Exception):
                    ownership.service.accept(owned_key, resolution.replay_job_id)
            return WorkflowEntrypointSubmissionOutcome(
                job_id=resolution.replay_job_id,
                replay_job_id=resolution.replay_job_id,
                deduplicated=True,
                request_fingerprint=request_fingerprint,
            )
        if resolution.claimed:
            ownership.owned_keys.append(key)
            identity = dict(resolution.identity)
            if identity:
                ownership.identities.append(identity)
                if not ownership.primary_identity:
                    ownership.primary_identity = identity
        return None

    @staticmethod
    def _fail_owned_keys(ownership: _IdempotencyOwnership, reason: str) -> None:
        for key in ownership.owned_keys:
            with suppress(Exception):
                ownership.service.fail(key, reason)

    def _persist_submission_files(
        self,
        *,
        staged_files: list[Any],
        request_fingerprint: str,
        ownership: _IdempotencyOwnership,
    ) -> tuple[Any, list[dict[str, Any]]] | WorkflowEntrypointSubmissionOutcome:
        lifecycle = self._artifact_lifecycle_factory(request_fingerprint, ownership.owned_keys)
        try:
            return lifecycle, lifecycle.persist(staged_files) if staged_files else []
        except Exception as exc:
            self._fail_owned_keys(ownership, "artifact_persistence_failed")
            return self._failure(
                failure_kind="unavailable",
                code="artifact_persistence_failed",
                error="Attachment persistence failed",
                exc=exc,
                request_fingerprint=request_fingerprint,
            )

    def submit(
        self,
        *,
        entry_key: str,
        task_id: str,
        project_id: str,
        run_config: dict[str, Any],
        workflow_id: str,
        request_id: str,
        idempotency_mode: str,
        idempotency_key: str,
        replay_key: str,
        mapping: dict[str, Any],
        sources: dict[str, Any],
        fallback_body: Any,
        staged_files: list[Any],
        request_context: dict[str, Any],
    ) -> WorkflowEntrypointSubmissionOutcome:
        prepared = self._prepare_submission(
            entry_key=entry_key,
            workflow_id=workflow_id,
            mapping=mapping,
            sources=sources,
            fallback_body=fallback_body,
            staged_files=staged_files,
        )
        if isinstance(prepared, WorkflowEntrypointSubmissionOutcome):
            return prepared
        ownership = self._resolve_idempotency(
            mode=idempotency_mode,
            header_key=idempotency_key,
            replay_key=replay_key,
            request_fingerprint=prepared.request_fingerprint,
        )
        if isinstance(ownership, WorkflowEntrypointSubmissionOutcome):
            return ownership
        persisted = self._persist_submission_files(
            staged_files=staged_files,
            request_fingerprint=prepared.request_fingerprint,
            ownership=ownership,
        )
        if isinstance(persisted, WorkflowEntrypointSubmissionOutcome):
            return persisted
        lifecycle, file_refs = persisted
        final_inputs = dict(prepared.inputs)
        for target, source in mapping.items():
            root, _, tail = str(source or "").partition(".")
            if root == "files":
                final_inputs[str(target)] = _extract_file_path(file_refs, tail)
        final_inputs["files"] = file_refs
        try:
            launch_result = self._launch_service.launch(
                entry_key=entry_key,
                task_id=task_id,
                project_id=project_id,
                run_config=run_config,
                workflow_id=workflow_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                workflow_inputs=final_inputs,
                workflow_interface=prepared.interface,
                files=file_refs,
                request_context=request_context,
                idempotency_identities=ownership.identities,
                primary_idempotency_identity=ownership.primary_identity,
            )
        except Exception as exc:
            from core.application.jobs.task_enqueue_runtime import QueueTransportUnavailableError

            recoverable = isinstance(exc, QueueTransportUnavailableError) and bool(
                exc.recoverable and exc.job_id
            )
            if not recoverable:
                lifecycle.rollback(file_refs)
                self._fail_owned_keys(ownership, "workflow_dispatch_failed")
            return self._failure(
                failure_kind="unavailable" if recoverable else "internal",
                code="workflow_dispatch_failed",
                error=(
                    "Webhook queue is unavailable; retry later"
                    if recoverable
                    else "Workflow dispatch failed"
                ),
                exc=exc,
                recoverable_job_id=str(getattr(exc, "job_id", "")) if recoverable else "",
                file_count=len(file_refs),
                request_fingerprint=prepared.request_fingerprint,
            )

        for key in ownership.owned_keys:
            with suppress(Exception):
                ownership.service.accept(key, launch_result.job_id)
        return WorkflowEntrypointSubmissionOutcome(
            job_id=launch_result.job_id,
            task_summary=launch_result.task_summary,
            file_count=len(file_refs),
            request_fingerprint=prepared.request_fingerprint,
        )


__all__ = [
    "WorkflowEntrypointSubmissionOutcome",
    "WorkflowEntrypointSubmissionUseCase",
]
