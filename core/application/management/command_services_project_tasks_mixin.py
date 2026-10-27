# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .command_services_common import *  # NOSONAR


def _idempotency_result_expires_at(*, retention_days: int) -> datetime:
    """Keep accepted replay results at least as long as durable jobs survive."""
    return datetime.now(UTC) + timedelta(days=min(3650, max(1, retention_days)))


logger = logging.getLogger(__name__)


class ProjectCommandServiceTasksMixin:
    def enqueue_manual_task(
        self,
        command: ProjectTaskEnqueueInput,
        *,
        allow_implicit_project: bool = False,
        scope: ScopeContext | None = None,
        trusted_internal: bool = False,
    ) -> ProjectTaskEnqueueResultView:
        task_id = str(command.task_id or "").strip()
        if not task_id:
            message = "task_id is required"
            raise ServiceValidationError(
                message,
                payload={"error": message, "code": "task_id_required"},
            )
        resolved = self._scope(
            scope, project_id=command.project_id, workflow_id=command.workflow_id
        )
        bl = self._logger()
        if self._enqueue_task is None:
            raise ServiceUnavailableError("Task enqueue is unavailable")

        attachment_rows = self._validated_attachment_rows(command)
        project_id, project, project_name, project_config, project_created = (
            self._resolve_submission_project(
                bl,
                command,
                allow_implicit_project=allow_implicit_project,
            )
        )
        if project and not project_created:
            require_project_access(resolved, project)

        sanitized_task_summary = self._sanitize_external_input(
            str(command.task_summary or "").strip()
        )
        requested_workflow_id = str(command.workflow_id or "").strip()
        from core.application.orchestration.state_path_guards import (
            strip_reserved_runtime_state,
        )

        extra_state = strip_reserved_runtime_state(command.extra_state or {})
        from core.application.management.task_submission import (
            TaskSubmissionValidationError,
            normalize_workflow_inputs,
        )

        try:
            normalized_submission = normalize_workflow_inputs(
                inputs=command.inputs,
                extra_state=extra_state,
                job_meta=command.job_meta,
            )
        except TaskSubmissionValidationError as exc:
            raise ServiceValidationError(
                exc.message,
                payload={"error": exc.message, "code": exc.code},
            ) from exc
        submitted_extra_state = dict(normalized_submission.extra_state)
        provided_task_envelope = (
            dict(submitted_extra_state.get("task_envelope") or {})
            if isinstance(submitted_extra_state.get("task_envelope"), dict)
            else {}
        )
        # Public callers may only influence the task's descriptive contract.
        # Trusted application callers use an explicit boundary flag; their
        # server-built context (for example MCP client_context) is preserved.
        if trusted_internal:
            extra_state = submitted_extra_state
            job_meta = dict(normalized_submission.job_meta)
        else:
            extra_state = {
                key: submitted_extra_state[key]
                for key in ("description", "origin", "priority", "task_type")
                if key in submitted_extra_state
            }
            job_meta = {}
        client_context = dict(command.client_context or {})
        job_meta.pop("client_context", None)
        if client_context:
            # Exact caller context is execution-only. Job metadata is exposed
            # through public API and UI projections, so it must never retain
            # request headers, bodies, or neutral-name secret values.
            extra_state["client_context"] = client_context
        selected_workflow_id, selection_source = self._resolve_selected_workflow(
            project_config=project_config,
            requested_workflow_id=requested_workflow_id,
        )
        if self._validate_explicit_workflow_override is not None:
            try:
                self._validate_explicit_workflow_override(
                    selected_workflow_id=selected_workflow_id,
                    selection_source=selection_source,
                    task_summary=sanitized_task_summary,
                    project_name=project_name or str(project.get("name") or "").strip(),
                    project_config=project_config,
                    extra_state=extra_state,
                )
            except ValueError as exc:
                raise ServiceValidationError(str(exc)) from exc

        if selected_workflow_id:
            require_workflow_access(resolved, selected_workflow_id)

        self._assert_can_start_workflow(
            workflow_id=selected_workflow_id,
            account_id=str(
                (project or {}).get("account_id") or resolved.account_id or "acc_default"
            ).strip(),
            project_id=str(project_id),
        )

        normalized_task_source = str(command.task_source or "manual").strip() or "manual"
        entry_path = normalize_entry_path(
            command.entry_path,
            task_source=normalized_task_source,
            default="manual_api",
        )
        source_system = "manual" if entry_path == "manual_ui" else "api"

        test_mode = bool(command.test_mode)
        if self._parse_test_mode is not None:
            test_mode = self._parse_test_mode(command.test_mode, False)

        attachment_metadata: list[dict[str, Any]] = []
        file_refs: list[dict[str, Any]] = []
        plane = _resolve_artifact_plane(self._artifact_plane)
        has_file_storage = bool(plane is not None)
        if attachment_rows and not has_file_storage:
            raise ServiceUnavailableError("File storage is unavailable")

        from core.application.orchestration.task_envelope import TaskEnvelope
        from core.application.registry.workflow_contract import RUNTIME_PAYLOAD_KEYS
        from core.application.registry.workflow_contract_service import WorkflowInterfaceService

        envelope_inputs = (
            dict(provided_task_envelope.get("inputs") or {})
            if isinstance(provided_task_envelope.get("inputs"), dict)
            else {}
        )
        provided_workflow_inputs = dict(normalized_submission.inputs)
        preserve_rich_envelope_inputs = False
        interface_service = WorkflowInterfaceService()
        if selected_workflow_id:
            try:
                workflow_interface = interface_service.interface_for_workflow_id(
                    selected_workflow_id
                )
            except Exception as exc:
                message = "Workflow contract service is unavailable; retry later"
                raise ServiceUnavailableError(
                    message,
                    payload={"error": message, "code": "workflow_contract_unavailable"},
                ) from exc
        else:
            from core.application.registry.workflow_contract import WorkflowInterface

            workflow_interface = WorkflowInterface(enabled=False, output_mode="none")
        declared_direct_inputs = self._bind_direct_workflow_inputs(
            command, trusted_internal, workflow_interface, provided_workflow_inputs, attachment_rows
        )
        if not provided_workflow_inputs and envelope_inputs:
            if (
                workflow_interface.input_mode in {"json_schema", "passthrough"}
                or not bool(workflow_interface.enabled)
                or not workflow_interface.expected_inputs
            ):
                provided_workflow_inputs = dict(envelope_inputs)
                preserve_rich_envelope_inputs = True
            elif not bool(workflow_interface.strict_inputs):
                provided_workflow_inputs = dict(envelope_inputs)
            else:
                declared_names = {
                    row.name for row in workflow_interface.expected_inputs if row.name
                }
                allowed_names = declared_names | set(RUNTIME_PAYLOAD_KEYS)
                provided_workflow_inputs = {
                    key: value for key, value in envelope_inputs.items() if key in allowed_names
                }
        if bool(workflow_interface.enabled) and workflow_interface.expected_inputs:
            workflow_inputs = provided_workflow_inputs
        else:
            workflow_inputs = dict(provided_workflow_inputs)
        if not workflow_inputs and not declared_direct_inputs:
            workflow_inputs = {
                "title": sanitized_task_summary,
                "description": str(
                    extra_state.get("description") or sanitized_task_summary
                ).strip(),
                "priority": str(extra_state.get("priority") or "medium").strip().lower()
                or "medium",
            }
        validation_inputs = workflow_inputs
        if (
            preserve_rich_envelope_inputs
            and bool(workflow_interface.enabled)
            and workflow_interface.input_mode == "fields"
            and not workflow_interface.expected_inputs
        ):
            validation_inputs = {
                key: value for key, value in workflow_inputs.items() if key in RUNTIME_PAYLOAD_KEYS
            }
        validation = interface_service.validate_inputs(
            workflow_interface,
            validation_inputs,
            channel="mcp" if normalized_task_source == "mcp_workflow" else entry_path,
        )
        if not validation.ok:
            messages = [
                str(item.get("message") or item.get("code") or "").strip()
                for item in validation.errors[:5]
                if isinstance(item, dict)
            ]
            detail = "; ".join([item for item in messages if item])
            raise ServiceValidationError(
                f"Workflow input validation failed{': ' + detail if detail else ''}"
            )
        if not preserve_rich_envelope_inputs and bool(workflow_interface.enabled):
            workflow_inputs = validation.inputs
        from core.application.registry.workflow_secret_defaults import (
            workflow_secret_launch_snapshot,
        )

        secret_ref_snapshot = workflow_secret_launch_snapshot(
            workflow_interface,
            inputs=workflow_inputs,
        )
        extra_state["inputs"] = dict(workflow_inputs)

        task_envelope = TaskEnvelope(
            entry_path=entry_path,
            source_system=source_system,
            source_event_id=task_id,
            source_object_id=task_id,
            task_type=str(workflow_inputs.get("task_type") or extra_state.get("task_type") or "")
            .strip()
            .lower(),
            title=str(workflow_inputs.get("title") or sanitized_task_summary).strip(),
            description=str(workflow_inputs.get("description") or sanitized_task_summary).strip(),
            priority=str(workflow_inputs.get("priority") or extra_state.get("priority") or "medium")
            .strip()
            .lower()
            or "medium",
            project_id=str(project_id),
            requested_workflow_id=requested_workflow_id,
            inputs=workflow_inputs,
            files=[],
            metadata={"task_source": normalized_task_source},
            entry_context={"task_source": normalized_task_source},
        ).to_dict()
        # A job is created before a multipart pre-publish hook persists its
        # artifacts.  Recovery must therefore distinguish a complete no-file
        # payload from a job that crashed while its attachments were in flight.
        task_envelope["payload_complete"] = not bool(attachment_rows)
        original_envelope_inputs = provided_task_envelope.get("inputs")
        accepted_envelope_inputs = task_envelope.get("inputs")
        if isinstance(original_envelope_inputs, dict):
            dropped_descriptive_inputs = {
                key: value
                for key, value in original_envelope_inputs.items()
                if key not in (accepted_envelope_inputs or {})
                and str(key).strip().lower() not in {"verification_commands", "commands"}
            }
            if dropped_descriptive_inputs:
                # Preserve only safe context rejected by a strict callable
                # contract. Accepted inputs remain in the canonical envelope.
                task_envelope["metadata"]["original_task_envelope_inputs"] = (
                    dropped_descriptive_inputs
                )
        if isinstance(task_envelope.get("files"), list):
            file_refs[:] = [
                dict(item) for item in task_envelope.get("files") or [] if isinstance(item, dict)
            ]
        from core.application.orchestration.state import promote_callable_input_state

        extra_state = promote_callable_input_state(
            {
                **extra_state,
                "task_envelope": task_envelope,
                "inputs": dict(workflow_inputs),
            }
        )
        for promoted_key in (
            "request",
            "original_request",
            "initial_requirements",
            "inputs",
            "task_envelope",
            # `origin` is one of the four public annotations the submission
            # allowlist advertises, but unlike description, priority and
            # task_type it feeds nothing downstream. Without this it was
            # accepted, validated, and then dropped before the job was written,
            # so an integrator who labelled their submissions found the label
            # nowhere afterwards. It is a caller-supplied label and is kept as
            # one - trusted provenance stays in task_envelope.source_system.
            "origin",
        ):
            if promoted_key in extra_state:
                job_meta[promoted_key] = extra_state[promoted_key]

        idempotency_claim = None

        def _renew_idempotency_lease(connection=None) -> None:
            if idempotency_claim is None or not idempotency_claim.claimed:
                return
            from core.application.idempotency import (
                RequestIdempotencyRepository,
                RequestIdempotencyService,
            )
            from core.infrastructure.db.db_pool import transaction_connection

            if connection is None:
                with transaction_connection(bl.conn) as conn:
                    renewed = RequestIdempotencyService(RequestIdempotencyRepository(conn)).renew(
                        claim=idempotency_claim
                    )
            else:
                renewed = RequestIdempotencyService(RequestIdempotencyRepository(connection)).renew(
                    claim=idempotency_claim
                )
            if not renewed:
                raise ServiceConflictError(
                    "Idempotency lease was lost before queue dispatch",
                    payload={
                        "error": "Idempotency lease was lost before queue dispatch",
                        "code": "idempotency_lease_lost",
                        "retry_after": 2,
                    },
                )

        request_fingerprint = ""
        idempotency_key = str(command.idempotency_key or "").strip()
        if idempotency_key:
            from core.application.idempotency import (
                IdempotencyScope,
                RequestIdempotencyRepository,
                RequestIdempotencyService,
            )
            from core.application.management.task_submission import (
                NormalizedTaskSubmission,
                TaskAttachmentDescriptor,
                canonical_task_fingerprint,
            )
            from core.infrastructure.db.db_pool import transaction_connection

            attachment_descriptors = [
                TaskAttachmentDescriptor(
                    filename=str(row.get("filename") or ""),
                    media_type=str(row.get("mime_type") or "application/octet-stream"),
                    size=int(row.get("size_bytes") or 0),
                    sha256=str(row.get("sha256") or "").strip().lower(),
                )
                for row in attachment_rows
            ]
            request_fingerprint = canonical_task_fingerprint(
                NormalizedTaskSubmission(
                    inputs=dict(workflow_inputs),
                    extra_state=dict(extra_state),
                    job_meta=dict(job_meta),
                ),
                project_id=str(project_id),
                workflow_selector=requested_workflow_id or "default",
                resolved_workflow_id=selected_workflow_id,
                task_summary=sanitized_task_summary,
                mode=command.mode,
                test_mode=test_mode,
                task_source=normalized_task_source,
                attachments=attachment_descriptors,
            )
            idempotency_scope = IdempotencyScope(
                operation="task.launch",
                account_id=str(
                    (project or {}).get("account_id") or resolved.account_id or DEFAULT_ACCOUNT_ID
                ),
                dimensions=(
                    ("project", str(project_id)),
                    ("workflow", requested_workflow_id or "default"),
                ),
            )
            accepted_response_template = {
                "project_id": str(project_id),
                "task_id": task_id,
                "status": "pending",
                "queue": f"platform.project.{project_id}",
                "file_count": len(attachment_rows),
                "project_created": project_created,
                "deduplicated": False,
            }
            adopted_response = None
            recovery_dispatch = None
            incomplete_recovery_error: ServiceConflictError | None = None
            with transaction_connection(bl.conn) as conn:
                idempotency_repository = RequestIdempotencyRepository(conn)

                def _can_reclaim_idempotency(_row: dict[str, Any]) -> bool:
                    durable = idempotency_repository.find_job_by_request_identity(
                        scope_key=idempotency_scope.key(),
                        key_digest=RequestIdempotencyService.digest_key(idempotency_key),
                    )
                    durable_job_id = str((durable or {}).get("id") or "").strip()
                    if not durable_job_id:
                        return True
                    from core.application.jobs.dispatch_outbox_repository import (
                        DispatchOutboxRepository,
                    )

                    return not DispatchOutboxRepository(conn).has_live_preparation(
                        job_id=durable_job_id
                    )

                idempotency_service = RequestIdempotencyService(
                    idempotency_repository,
                    reclaim_guard=_can_reclaim_idempotency,
                )
                idempotency_claim = idempotency_service.claim(
                    scope=idempotency_scope,
                    opaque_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    request_id=uuid.uuid4().hex,
                )
                if idempotency_claim.claimed and idempotency_claim.recovered:
                    durable_job = idempotency_repository.find_job_by_request_identity(
                        scope_key=idempotency_claim.scope_key,
                        key_digest=idempotency_claim.key_digest,
                    )
                    if durable_job:
                        durable_meta = durable_job.get("meta") or {}
                        if isinstance(durable_meta, str):
                            durable_meta = json.loads(durable_meta)
                        durable_identity = (
                            dict(durable_meta.get("idempotency") or {})
                            if isinstance(durable_meta, dict)
                            else {}
                        )
                        durable_fingerprint = str(
                            durable_identity.get("request_fingerprint") or ""
                        ).strip()
                        if durable_fingerprint != request_fingerprint:
                            raise ServiceConflictError(
                                "Durable idempotent job fingerprint mismatch",
                                payload={
                                    "error": "Idempotency key payload mismatch",
                                    "code": "idempotency_payload_mismatch",
                                },
                            )
                        queue_delivery = (
                            durable_meta.get("queue_delivery")
                            if isinstance(durable_meta, dict)
                            else {}
                        )
                        queue_delivery = (
                            dict(queue_delivery) if isinstance(queue_delivery, dict) else {}
                        )
                        dispatch_state = (
                            str(queue_delivery.get("dispatch_state") or "").strip().lower()
                        )
                        dispatch_confirmed = (
                            dispatch_state == "publisher_confirmed"
                            or bool(queue_delivery.get("delivery_succeeded"))
                            or str(queue_delivery.get("state") or "").strip().lower()
                            == "delivery_succeeded"
                        )
                        job_status = str(durable_job.get("status") or "").strip().lower()
                        worker_id = str(durable_job.get("worker_id") or "").strip()
                        # Worker ownership is stronger evidence than the best-effort
                        # delivery marker.  A stale retry may only adopt it, never
                        # overwrite its metadata or transition it to triage.
                        dispatch_confirmed = dispatch_confirmed or bool(worker_id)
                        # A matching job with a confirmed dispatch is the durable
                        # result of this key, including terminal jobs. Replaying it
                        # must never create or enqueue a second execution.
                        if not dispatch_confirmed:
                            delivery_state = str(queue_delivery.get("state") or "").strip().lower()
                            durable_envelope = durable_meta.get("task_envelope")
                            durable_envelope = (
                                dict(durable_envelope) if isinstance(durable_envelope, dict) else {}
                            )
                            if not bool(durable_envelope.get("payload_complete")):
                                from core.application.jobs.queue_delivery_runtime import (
                                    queue_dead_letter_queue_name,
                                    record_queue_delivery_terminal_failure,
                                )
                                from core.application.jobs.terminal_persistence import (
                                    TerminalPersistenceConflict,
                                    TerminalPersistenceService,
                                )

                                queue_name = str(
                                    queue_delivery.get("queue_name")
                                    or f"platform.project.{durable_job.get('project_id') or project_id}"
                                )
                                failed_meta, _failed_state = record_queue_delivery_terminal_failure(
                                    durable_meta,
                                    queue_name=queue_name,
                                    dead_letter_queue_name=queue_dead_letter_queue_name(queue_name),
                                    project_policy=None,
                                    failure_class="idempotency_payload_incomplete",
                                    failure_code="idempotency_payload_incomplete",
                                    failure_message=(
                                        "Multipart payload persistence did not complete before "
                                        "queue dispatch."
                                    ),
                                    count_attempt=False,
                                )
                                if job_status == "pending":
                                    try:
                                        TerminalPersistenceService(bl).complete_failure(
                                            job_id=str(durable_job.get("id") or ""),
                                            status="failed",
                                            error_message=(
                                                "Multipart payload persistence did not complete "
                                                "before queue dispatch."
                                            ),
                                            error_code="idempotency_payload_incomplete",
                                            category="submission",
                                            retryable=True,
                                            require_pending_unowned=True,
                                            meta_patch={
                                                "queue_delivery": dict(
                                                    failed_meta.get("queue_delivery") or {}
                                                ),
                                                "runtime_journal": list(
                                                    failed_meta.get("runtime_journal") or []
                                                ),
                                            },
                                        )
                                    except TerminalPersistenceConflict as exc:
                                        raise ServiceConflictError(
                                            "The durable job was claimed while incomplete payload recovery was fencing it",
                                            payload={
                                                "error": (
                                                    "The durable job was claimed while incomplete payload "
                                                    "recovery was fencing it"
                                                ),
                                                "code": "idempotency_recovery_claimed",
                                                "job_id": str(durable_job.get("id") or ""),
                                            },
                                        ) from exc
                                elif job_status != "failed" or worker_id:
                                    raise ServiceConflictError(
                                        "The incomplete durable job is not safe to retire",
                                        payload={
                                            "error": "Incomplete payload recovery is not safe for this job state",
                                            "code": "idempotency_recovery_not_safe",
                                            "job_id": str(durable_job.get("id") or ""),
                                        },
                                    )
                                from core.application.jobs.dispatch_outbox_repository import (
                                    DispatchOutboxRepository,
                                )

                                DispatchOutboxRepository(conn).mark(
                                    job_id=str(durable_job.get("id") or ""),
                                    status="failed",
                                    last_error="idempotency_payload_incomplete",
                                )
                                pending_artifacts = durable_meta.get(
                                    "attachment_persistence_artifacts"
                                )
                                pending_artifacts = (
                                    [
                                        dict(item)
                                        for item in pending_artifacts
                                        if isinstance(item, dict)
                                    ]
                                    if isinstance(pending_artifacts, list)
                                    else []
                                )
                                delete_artifact = getattr(plane, "delete_artifact", None)
                                undeleted_refs: list[dict[str, Any]] = []
                                for artifact_ref in pending_artifacts:
                                    artifact_id = str(artifact_ref.get("artifact_id") or "").strip()
                                    if not artifact_id:
                                        continue
                                    try:
                                        if not callable(delete_artifact) or not delete_artifact(
                                            artifact_id
                                        ):
                                            undeleted_refs.append(dict(artifact_ref))
                                    except Exception:
                                        logger.exception(
                                            "Failed to clean incomplete attachment artifact_id=%s",
                                            artifact_id,
                                        )
                                        undeleted_refs.append(dict(artifact_ref))
                                latest_job = bl.get_job(str(durable_job.get("id") or ""))
                                latest_meta = (
                                    dict(latest_job.get("meta") or {})
                                    if isinstance(latest_job, dict)
                                    and isinstance(latest_job.get("meta"), dict)
                                    else {}
                                )
                                if job_status == "failed":
                                    latest_meta["queue_delivery"] = dict(
                                        failed_meta.get("queue_delivery") or {}
                                    )
                                    latest_meta["runtime_journal"] = list(
                                        failed_meta.get("runtime_journal") or []
                                    )
                                if undeleted_refs:
                                    latest_meta["attachment_persistence_artifacts"] = undeleted_refs
                                else:
                                    latest_meta.pop("attachment_persistence_artifacts", None)
                                # This job never had a complete payload and was never
                                # dispatched.  Keep its identity for audit, but remove
                                # it from canonical recovery lookup so a later retry of
                                # the same request can create a fresh job safely.
                                abandoned_identity = latest_meta.pop("idempotency", None)
                                if isinstance(abandoned_identity, dict):
                                    latest_meta["abandoned_idempotency"] = {
                                        **abandoned_identity,
                                        "reason": "payload_incomplete_before_dispatch",
                                    }
                                bl.update_job(str(durable_job.get("id") or ""), meta=latest_meta)
                                idempotency_service.fail(
                                    claim=idempotency_claim,
                                    error_code="idempotency_payload_incomplete",
                                )
                                incomplete_recovery_error = ServiceConflictError(
                                    "The previous upload stopped before its payload was durable; retry the request",
                                    payload={
                                        "error": (
                                            "The previous upload stopped before its payload was durable; "
                                            "retry the request"
                                        ),
                                        "code": "idempotency_payload_incomplete_retryable",
                                        "job_id": str(durable_job.get("id") or ""),
                                        "retry_after": 1,
                                    },
                                )
                            if incomplete_recovery_error is None:
                                can_redeliver = (
                                    self._enqueue_task_in_queue is not None
                                    and job_status == "pending"
                                    and not worker_id
                                    and delivery_state in {"queued", "retrying_delivery"}
                                    and dispatch_state == "not_started"
                                )
                                if not can_redeliver:
                                    raise ServiceConflictError(
                                        "The durable idempotency reservation has no safely recoverable dispatch",
                                        payload={
                                            "error": "Idempotency recovery requires a confirmed or safely redeliverable queue dispatch",
                                            "code": "idempotency_recovery_not_safe",
                                            "job_id": str(durable_job.get("id") or ""),
                                        },
                                    )
                                # The encrypted outbox contains the exact original queue
                                # message. Recovery never reconstructs it from redacted
                                # snapshots or current secrets.
                                recovery_dispatch = {"job_id": str(durable_job.get("id") or "")}
                        if incomplete_recovery_error is None:
                            recovery_response = dict(
                                durable_identity.get("accepted_response")
                                or accepted_response_template
                            )
                            recovery_response["job_id"] = str(durable_job.get("id") or "")
                            if recovery_dispatch is None:
                                idempotency_service.accept(
                                    claim=idempotency_claim,
                                    job_id=recovery_response["job_id"],
                                    response=recovery_response,
                                    result_expires_at=_idempotency_result_expires_at(
                                        retention_days=self._idempotency_result_retention_days
                                    ),
                                )
                                adopted_response = recovery_response
            if incomplete_recovery_error is not None:
                raise incomplete_recovery_error
            if recovery_dispatch is not None:
                _renew_idempotency_lease()
                from core.application.jobs.task_enqueue_runtime import (
                    redeliver_pre_execution_job,
                )

                try:
                    redelivered = redeliver_pre_execution_job(
                        bl=bl,
                        job_id=recovery_dispatch["job_id"],
                        enqueue_task_fn=self._enqueue_task_in_queue,
                        logger=logger,
                        log_prefix="idempotency_recovery",
                    )
                except Exception as publish_err:
                    try:
                        with transaction_connection(bl.conn) as conn:
                            RequestIdempotencyService(RequestIdempotencyRepository(conn)).fail(
                                claim=idempotency_claim,
                                error_code="queue_publish_failed",
                            )
                    except Exception as idempotency_error:
                        # A concurrent retry can reclaim an expired claim while the
                        # queue call fails. Preserve the client-visible delivery
                        # failure rather than replacing it with a stale-owner error.
                        logger.warning(
                            "Could not mark failed idempotency claim after recovery "
                            "publish failure: %s",
                            idempotency_error,
                        )
                    raise ServiceUnavailableError(
                        "RabbitMQ task queue is unavailable; the job was not enqueued."
                    ) from publish_err
                if not redelivered:
                    raise ServiceConflictError(
                        "The durable job changed while recovery was preparing redelivery",
                        payload={
                            "error": "The durable job changed while recovery was preparing redelivery",
                            "code": "idempotency_recovery_claimed",
                            "job_id": recovery_dispatch["job_id"],
                        },
                    )
                with transaction_connection(bl.conn) as conn:
                    RequestIdempotencyService(RequestIdempotencyRepository(conn)).accept(
                        claim=idempotency_claim,
                        job_id=recovery_response["job_id"],
                        response=recovery_response,
                        result_expires_at=_idempotency_result_expires_at(
                            retention_days=self._idempotency_result_retention_days
                        ),
                    )
                adopted_response = recovery_response
            if adopted_response is not None:
                adopted_response["deduplicated"] = True
                return ProjectTaskEnqueueResultView(**adopted_response)
            if idempotency_claim.outcome == "replay":
                replay = dict(idempotency_claim.response or {})
                replay["deduplicated"] = True
                return ProjectTaskEnqueueResultView(**replay)
            if idempotency_claim.outcome == "mismatch":
                raise ServiceConflictError(
                    "Idempotency key was used with a different task submission",
                    payload={
                        "error": "Idempotency key payload mismatch",
                        "code": "idempotency_payload_mismatch",
                    },
                )
            if idempotency_claim.outcome == "expired":
                raise ServiceError(
                    "Idempotency replay result has expired",
                    status_code=410,
                    payload={
                        "error": "Idempotency replay result has expired",
                        "code": "idempotency_result_expired",
                    },
                )
            if not idempotency_claim.claimed:
                raise ServiceConflictError(
                    "An identical task submission is still in progress",
                    payload={
                        "error": "Idempotent task submission is in progress",
                        "code": "idempotency_in_progress",
                        "retry_after": 2,
                    },
                )
            job_meta["idempotency"] = {
                "scope_key": idempotency_claim.scope_key,
                "key_digest": idempotency_claim.key_digest,
                "request_id": idempotency_claim.request_id,
                "request_fingerprint": request_fingerprint,
                "accepted_response": accepted_response_template,
            }

        def _persist_attachments(
            job_id: str, enqueue_logger: Any, preparation_heartbeat=None
        ) -> dict[str, Any]:
            preparation_heartbeat = preparation_heartbeat or (lambda: None)
            nonlocal attachment_metadata
            if not attachment_rows:
                return {}

            def _journal_created_artifact(artifact_ref: dict[str, str]) -> None:
                from core.infrastructure.db.db_pool import transaction_connection

                with transaction_connection(enqueue_logger.conn):
                    job_row = enqueue_logger.get_job(str(job_id))
                    if not isinstance(job_row, dict):
                        raise ServiceError(
                            "Created job could not be reloaded for attachment journal"
                        )
                    current_meta = job_row.get("meta") or {}
                    if isinstance(current_meta, str):
                        current_meta = json.loads(current_meta)
                    if not isinstance(current_meta, dict):
                        raise ServiceError("Created job metadata is invalid for attachment journal")
                    clean_id = str(artifact_ref.get("artifact_id") or "").strip()
                    object_key = str(artifact_ref.get("object_key") or "").strip()
                    pending_refs = current_meta.get("attachment_persistence_artifacts")
                    pending_refs = list(pending_refs) if isinstance(pending_refs, list) else []
                    pending_refs = [
                        dict(item)
                        for item in pending_refs
                        if isinstance(item, dict)
                        and str(item.get("artifact_id") or "").strip() != clean_id
                    ]
                    if clean_id:
                        pending_refs.append({"artifact_id": clean_id, "object_key": object_key})
                    current_meta["attachment_persistence_artifacts"] = pending_refs
                    enqueue_logger.update_job(str(job_id), meta=current_meta)

            attachment_metadata = _persist_file_refs(
                _enqueue_logger=enqueue_logger,
                job_id=str(job_id),
                file_rows=attachment_rows,
                artifact_plane=plane,
                artifact_owner_scope={
                    **_scope_audit_fields(resolved),
                    "entry_path": entry_path,
                    "project_id": str(project_id),
                    "job_id": str(job_id),
                },
                artifact_run_id=f"job:{job_id}",
                artifact_step_id="manual_enqueue",
                source_ref_prefix="manual_file",
                lease_heartbeat=preparation_heartbeat,
                on_artifact_created=_journal_created_artifact,
            )
            existing_refs = [dict(item) for item in file_refs if isinstance(item, dict)]
            file_refs[:] = [
                *existing_refs,
                *[dict(row) for row in attachment_metadata if isinstance(row, dict)],
            ]
            task_envelope["files"] = file_refs
            task_envelope["payload_complete"] = True
            job_row = enqueue_logger.get_job(str(job_id)) or {}
            meta_patch = self._attachment_metadata_patch(job_row, attachment_metadata)
            meta_patch["files"] = [dict(row) for row in attachment_metadata]
            if declared_direct_inputs:
                final_inputs = dict(workflow_inputs)
                for uploaded, stored in zip(attachment_rows, attachment_metadata, strict=True):
                    final_inputs[uploaded["input_name"]] = dict(stored)
                meta_patch["inputs"] = final_inputs
                task_envelope["inputs"] = final_inputs
            meta_patch["task_envelope"] = dict(task_envelope)
            return meta_patch

        created_job_id = ""

        def _persist_initial_public_progress(
            job_id: str, enqueue_logger: Any, preparation_heartbeat=None
        ) -> dict[str, Any]:
            preparation_heartbeat = preparation_heartbeat or (lambda: None)
            nonlocal created_job_id
            created_job_id = str(job_id)
            attachment_patch = _persist_attachments(job_id, enqueue_logger, preparation_heartbeat)
            _renew_idempotency_lease()
            return attachment_patch

        try:
            job_id = enqueue_workflow_start(
                enqueue_task_fn=self._enqueue_task,
                task_id=task_id,
                task_summary=sanitized_task_summary,
                task_source=normalized_task_source,
                project_id=str(project_id),
                project_name=project_name or str(project.get("name") or "").strip(),
                project_config=project_config,
                selected_workflow_id=selected_workflow_id,
                workflow_selection_source=selection_source,
                entry_path=entry_path,
                input_contract="task_envelope",
                source_system=source_system,
                requested_workflow_id=requested_workflow_id,
                source_event_id=task_id,
                source_object_id=task_id,
                mode=str(command.mode or "").strip(),
                test_mode=test_mode,
                task_envelope=task_envelope,
                files=file_refs,
                extra_state=extra_state or None,
                workflow_secret_ref_fingerprints=secret_ref_snapshot,
                job_meta=job_meta or None,
                pre_publish_fn=_persist_initial_public_progress,
                preparation_additional_heartbeat=_renew_idempotency_lease,
                fail_on_pre_publish_error=True,
            )
        except Exception as exc:
            preserve_recoverable_artifacts = False
            if idempotency_claim is not None and idempotency_claim.claimed and created_job_id:
                created_job = bl.get_job(created_job_id) if hasattr(bl, "get_job") else None
                created_meta = (
                    (created_job or {}).get("meta") if isinstance(created_job, dict) else {}
                )
                if isinstance(created_meta, str):
                    created_meta = json.loads(created_meta)
                created_delivery = (
                    created_meta.get("queue_delivery") if isinstance(created_meta, dict) else {}
                )
                delivery_state = str((created_delivery or {}).get("state") or "").strip().lower()
                preserve_recoverable_artifacts = (
                    str((created_job or {}).get("status") or "").strip().lower() == "pending"
                    and delivery_state in {"queued", "retrying_delivery"}
                    and not bool((created_delivery or {}).get("delivery_succeeded"))
                )
            delete_artifact = getattr(plane, "delete_artifact", None)
            if callable(delete_artifact) and not preserve_recoverable_artifacts:
                for attachment in attachment_metadata:
                    artifact_id = str(attachment.get("artifact_id") or "").strip()
                    if artifact_id:
                        try:
                            if not delete_artifact(artifact_id):
                                logger.error(
                                    "Staged attachment rollback did not complete artifact_id=%s",
                                    artifact_id,
                                )
                        except Exception:
                            logger.exception(
                                "Failed to roll back staged attachment artifact_id=%s",
                                artifact_id,
                            )
            if idempotency_claim is not None and idempotency_claim.claimed:
                from core.application.idempotency import (
                    RequestIdempotencyRepository,
                    RequestIdempotencyService,
                )
                from core.infrastructure.db.db_pool import transaction_connection

                try:
                    with transaction_connection(bl.conn) as conn:
                        RequestIdempotencyService(RequestIdempotencyRepository(conn)).fail(
                            claim=idempotency_claim,
                            error_code="task_enqueue_failed",
                        )
                except Exception as idempotency_error:
                    # The primary failure (including an ownership-fencing 409) is
                    # authoritative. A reclaimed lease must not be masked by a
                    # best-effort transition of the former owner's reservation.
                    logger.warning(
                        "Could not mark failed idempotency claim after enqueue failure: %s",
                        safe_operator_exception(idempotency_error),
                    )
            if isinstance(exc, ServiceError):
                raise
            from core.application.jobs.task_enqueue_runtime import QueueTransportUnavailableError

            if isinstance(exc, QueueTransportUnavailableError):
                raise ServiceUnavailableError("RabbitMQ task queue is unavailable") from exc
            if (not_runnable := _workflow_not_runnable_error(exc)) is not None:
                raise not_runnable from exc
            if attachment_rows and _is_attachment_storage_misconfigured(exc):
                raise ServiceUnavailableError(
                    "Attachment storage is unavailable (FS_SECRET_KEY / cryptography required)"
                ) from exc
            if _is_artifact_storage_misconfigured(exc):
                raise ServiceUnavailableError(artifact_storage_misconfigured_message()) from exc
            logger.exception(
                "Task enqueue failed task_id=%s workflow_id=%s",
                task_id,
                selected_workflow_id,
            )
            raise ServiceError("Failed to enqueue task") from exc

        result = ProjectTaskEnqueueResultView(
            project_id=str(project_id),
            task_id=task_id,
            job_id=str(job_id),
            queue=f"platform.project.{project_id}",
            file_count=len(attachment_metadata),
            project_created=project_created,
        )
        if idempotency_claim is not None and idempotency_claim.claimed:
            from core.application.idempotency import (
                RequestIdempotencyRepository,
                RequestIdempotencyService,
            )
            from core.infrastructure.db.db_pool import transaction_connection

            with transaction_connection(bl.conn) as conn:
                RequestIdempotencyService(RequestIdempotencyRepository(conn)).accept(
                    claim=idempotency_claim,
                    job_id=str(job_id),
                    response=result.dump(),
                    result_expires_at=_idempotency_result_expires_at(
                        retention_days=self._idempotency_result_retention_days
                    ),
                )
        return result


__all__ = [name for name in globals() if not name.startswith("__")]
