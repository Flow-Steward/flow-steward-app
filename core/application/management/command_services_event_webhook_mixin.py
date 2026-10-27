# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.workflows.notification_recipients import (
    validate_notification_reaction_actions,
)

from .command_services_common import *  # NOSONAR

_MESSAGE_PROVIDER_WEBHOOK_IS_NOT_AUTHORIZED_FOR_THIS_PROJECT = (
    "Provider webhook is not authorized for this project"
)
_MESSAGE_PROJECT_RUNTIME_CONFIGURATION_IS_UNAVAILABLE = (
    "Project runtime configuration is unavailable"
)


class EventCommandServiceWebhookMixin:
    def _project_provider_binding_matches(
        self,
        *,
        binding: dict[str, Any],
        provider_key: str,
        project_account_id: str | None,
    ) -> bool:
        candidate_key = (
            str(
                binding.get("provider_key")
                or binding.get("extension_id")
                or binding.get("integration_key")
                or ""
            )
            .strip()
            .lower()
        )
        if not candidate_key or candidate_key != provider_key:
            return False
        if bool(binding.get("enabled", True)) is False:
            return False
        binding_account_id = str(binding.get("account_id") or "").strip() or None
        if binding_account_id and project_account_id and binding_account_id != project_account_id:
            raise ServiceForbiddenError(
                _MESSAGE_PROVIDER_WEBHOOK_IS_NOT_AUTHORIZED_FOR_THIS_PROJECT
            )
        return True

    def _require_provider_webhook_project_binding(
        self,
        bl: Any,
        *,
        provider_key: str,
        project_id: str,
        project: dict[str, Any],
        project_config: dict[str, Any],
    ) -> None:
        project_account_id = str((project or {}).get("account_id") or "").strip() or None

        provider = None
        if hasattr(bl, "get_provider"):
            try:
                candidate = bl.get_provider(provider_key)
            except Exception:
                candidate = None
            if isinstance(candidate, dict):
                provider = candidate
        provider_account_id = str((provider or {}).get("account_id") or "").strip() or None
        if provider_account_id and project_account_id and provider_account_id != project_account_id:
            raise ServiceForbiddenError(
                _MESSAGE_PROVIDER_WEBHOOK_IS_NOT_AUTHORIZED_FOR_THIS_PROJECT
            )

        if hasattr(bl, "get_project_connection"):
            try:
                connection = bl.get_project_connection(
                    project_id=project_id,
                    extension_id=provider_key,
                    enabled_only=True,
                )
            except Exception:
                connection = None
            if isinstance(connection, dict) and self._project_provider_binding_matches(
                binding=connection,
                provider_key=provider_key,
                project_account_id=project_account_id,
            ):
                return

        for binding in project_config.get("project_connections") or ():
            if isinstance(binding, dict) and self._project_provider_binding_matches(
                binding=binding,
                provider_key=provider_key,
                project_account_id=project_account_id,
            ):
                return
        for binding in project_config.get("project_providers") or ():
            if isinstance(binding, dict) and self._project_provider_binding_matches(
                binding=binding,
                provider_key=provider_key,
                project_account_id=project_account_id,
            ):
                return

        raise ServiceForbiddenError(_MESSAGE_PROVIDER_WEBHOOK_IS_NOT_AUTHORIZED_FOR_THIS_PROJECT)

    def ingest_provider_webhook(
        self,
        command: ProviderWebhookIngestInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProviderWebhookIngestResultView:
        resolved_scope = _service_scope(scope, project_id=command.project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        if self._enqueue_task is None:
            raise ServiceUnavailableError("Task enqueue is unavailable")

        project_id = str(command.project_id or "").strip()
        if not project_id:
            raise ServiceValidationError(ERR_PROJECT_ID_REQUIRED)

        provider_key = str(command.provider_key or "").strip().lower()
        if not provider_key:
            raise ServiceValidationError("provider_key is required")

        project = bl.get_project(project_id) if hasattr(bl, "get_project") else None
        if not project:
            raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)

        project_config = dict(command.project_config or {})
        if not project_config:
            if self._get_project_run_config is None:
                raise ServiceUnavailableError(_MESSAGE_PROJECT_RUNTIME_CONFIGURATION_IS_UNAVAILABLE)
            try:
                project_config = dict(self._get_project_run_config(project_id) or {})
            except Exception as exc:
                raise ServiceUnavailableError(
                    _MESSAGE_PROJECT_RUNTIME_CONFIGURATION_IS_UNAVAILABLE
                ) from exc
            if not project_config:
                raise ServiceUnavailableError(_MESSAGE_PROJECT_RUNTIME_CONFIGURATION_IS_UNAVAILABLE)

        project_name = str(project_config.get("project_name") or project.get("name") or "").strip()
        if not project_name:
            raise ServiceError("Project name is not configured")

        self._require_provider_webhook_project_binding(
            bl,
            provider_key=provider_key,
            project_id=project_id,
            project=project,
            project_config=project_config,
        )

        task_id = str(command.task_id or command.source_id or "").strip()
        if not task_id:
            raise ServiceValidationError("task_id is required")

        title = str(command.title or "").strip()
        description = str(command.description or "").strip()
        metadata = self._to_json_obj(command.metadata) or {}
        trigger_config = self._to_json_obj(command.trigger_config) or {}
        normalized_task_type = str(command.task_type or "").strip().lower()
        is_comment_event = self._is_provider_comment_event(
            metadata=metadata,
            trigger_config=trigger_config,
        )

        if is_comment_event:
            comment_text = self._sanitize_external_input(
                str(
                    metadata.get("comment_text")
                    or metadata.get("text")
                    or description
                    or title
                    or ""
                ).strip()
            )
            if not comment_text:
                return ProviderWebhookIngestResultView(
                    provider_key=provider_key,
                    task_id=task_id,
                    status="ignored",
                )

            job = self._find_active_provider_job(
                bl,
                provider_key=provider_key,
                task_id=task_id,
                project_id=project_id,
            )
            if not job:
                return ProviderWebhookIngestResultView(
                    provider_key=provider_key,
                    task_id=task_id,
                    status="ignored",
                )
            job_id_value = str(job.get("id") or "").strip() or None
            if job_id_value is None:
                return ProviderWebhookIngestResultView(
                    provider_key=provider_key,
                    task_id=task_id,
                    status="ignored",
                )

            comment_id = str(
                metadata.get("comment_id") or metadata.get("event_id") or command.source_id or ""
            ).strip()
            event_key = str(metadata.get("event") or trigger_config.get("event") or "").strip()
            workflow_id = (
                str(
                    (self._to_json_obj(job.get("meta")) or {}).get("selected_workflow_id")
                    or (self._to_json_obj(job.get("meta")) or {}).get("requested_workflow_id")
                    or (self._to_json_obj(job.get("meta")) or {}).get("workflow_id")
                    or ""
                ).strip()
                or None
            )
            reaction = self.ingest_reaction_event(
                ReactionEventIngestInput(
                    event_type=EXTERNAL_COMMENT_RECEIVED_VALUE,
                    job_id=job_id_value,
                    workflow_id=workflow_id,
                    source=provider_key,
                    source_id=comment_id or task_id,
                    event_key=event_key,
                    dedupe_key=self._provider_comment_dedupe_key(
                        provider_key=provider_key,
                        task_id=task_id,
                        comment_id=comment_id,
                        comment_text=comment_text,
                    ),
                    payload={
                        "text": comment_text,
                        "task_id": task_id,
                        "comment_id": comment_id,
                        "event": event_key,
                    },
                ),
                scope=resolved_scope,
            )
            return ProviderWebhookIngestResultView(
                provider_key=provider_key,
                task_id=task_id,
                job_id=job_id_value,
                status="accepted",
                comment_length=len(comment_text),
                reaction=reaction.dump(),
            )

        requested_workflow_id = str(
            metadata.get("requested_workflow_id")
            or metadata.get("workflow_id")
            or trigger_config.get("requested_workflow_id")
            or trigger_config.get("workflow_id")
            or ""
        ).strip()
        selected_workflow_id, selection_source = self._resolve_selected_workflow(
            project_config=project_config,
            requested_workflow_id=requested_workflow_id,
        )
        account_id = (
            str((project or {}).get("account_id") or "").strip()
            or str(resolved_scope.account_id or "").strip()
            or "acc_default"
        )
        self._assert_can_start_workflow(
            workflow_id=selected_workflow_id,
            account_id=account_id,
            project_id=str(project_id),
        )

        attachment_inputs = self._resolved_provider_attachments(
            bl=bl,
            command=command,
            metadata=metadata,
            trigger_config=trigger_config,
            scope=resolved_scope,
        )
        attachment_rows = self._validated_provider_attachment_rows(attachment_inputs)
        attachment_metadata: list[dict[str, Any]] = []
        file_refs: list[dict[str, Any]] = []
        plane = _resolve_artifact_plane(self._artifact_plane)
        has_file_storage = bool(plane is not None)
        if attachment_rows and not has_file_storage:
            raise ServiceUnavailableError("File storage is unavailable")

        source_event_id = str(command.source_id or metadata.get("event_id") or task_id).strip()
        source_object_id = str(
            metadata.get("object_id")
            or metadata.get("task_id")
            or metadata.get("issue_id")
            or task_id
        ).strip()

        if title and description:
            task_summary = self._sanitize_external_input(f"{title}: {description}")
        else:
            task_summary = self._sanitize_external_input(title or description or task_id)

        from core.application.orchestration.task_envelope import TaskEnvelope

        task_envelope = TaskEnvelope(
            entry_path="task_ingress",
            source_system=provider_key,
            source_event_id=source_event_id,
            source_object_id=source_object_id,
            task_type=normalized_task_type,
            title=title or task_summary,
            description=description or task_summary,
            project_id=str(project_id),
            requested_workflow_id=requested_workflow_id,
            inputs={
                "title": title or task_summary,
                "description": description or task_summary,
                **({"task_type": normalized_task_type} if normalized_task_type else {}),
            },
            files=[],
            metadata=metadata,
            entry_context={
                "provider_key": provider_key,
                "trigger_config": trigger_config,
            },
        ).to_dict()
        task_envelope["payload_complete"] = not bool(attachment_rows)

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
                            "Created provider job could not be reloaded for attachment journal"
                        )
                    current_meta = job_row.get("meta") or {}
                    if isinstance(current_meta, str):
                        current_meta = json.loads(current_meta)
                    if not isinstance(current_meta, dict):
                        raise ServiceError("Created provider job metadata is invalid")
                    artifact_id = str(artifact_ref.get("artifact_id") or "").strip()
                    object_key = str(artifact_ref.get("object_key") or "").strip()
                    pending = current_meta.get("attachment_persistence_artifacts")
                    pending = list(pending) if isinstance(pending, list) else []
                    pending = [
                        dict(item)
                        for item in pending
                        if isinstance(item, dict)
                        and str(item.get("artifact_id") or "").strip() != artifact_id
                    ]
                    if artifact_id:
                        pending.append({"artifact_id": artifact_id, "object_key": object_key})
                    current_meta["attachment_persistence_artifacts"] = pending
                    enqueue_logger.update_job(str(job_id), meta=current_meta)

            attachment_metadata = _persist_file_refs(
                _enqueue_logger=enqueue_logger,
                job_id=str(job_id),
                file_rows=attachment_rows,
                artifact_plane=plane,
                artifact_owner_scope={
                    **_scope_audit_fields(resolved_scope),
                    "entry_path": "task_ingress",
                    "provider_key": provider_key,
                },
                artifact_run_id=task_id,
                artifact_step_id="provider_webhook_ingest",
                source_ref_prefix="provider_file",
                lease_heartbeat=preparation_heartbeat,
                on_artifact_created=_journal_created_artifact,
            )
            file_refs[:] = [dict(row) for row in attachment_metadata if isinstance(row, dict)]
            task_envelope["files"] = file_refs
            task_envelope["payload_complete"] = True
            job_row = enqueue_logger.get_job(str(job_id)) or {}
            from .command_services_project_core_mixin import ProjectCommandServiceCoreMixin

            meta_patch = ProjectCommandServiceCoreMixin._attachment_metadata_patch(
                job_row, attachment_metadata
            )
            meta_patch["files"] = [dict(row) for row in attachment_metadata]
            meta_patch["task_envelope"] = dict(task_envelope)
            return meta_patch

        try:
            job_id = enqueue_workflow_start(
                enqueue_task_fn=self._enqueue_task,
                task_id=task_id,
                task_summary=task_summary,
                task_source="provider",
                project_id=project_id,
                project_name=project_name,
                project_config=project_config,
                selected_workflow_id=selected_workflow_id,
                workflow_selection_source=selection_source,
                entry_path="task_ingress",
                input_contract="task_envelope",
                source_system=provider_key,
                requested_workflow_id=requested_workflow_id,
                source_event_id=source_event_id,
                source_object_id=source_object_id,
                task_envelope=task_envelope,
                files=file_refs,
                extra_state={
                    "external_provider_key": provider_key,
                    "external_source_id": source_event_id,
                    "external_task_type": str(command.task_type or "").strip(),
                    "scope_actor": resolved_scope.actor_name,
                },
                job_meta={
                    "provider_key": provider_key,
                },
                pre_publish_fn=_persist_attachments if attachment_rows else None,
                fail_on_pre_publish_error=bool(attachment_rows),
            )
        except Exception as exc:
            delete_artifact = getattr(plane, "delete_artifact", None)
            if callable(delete_artifact):
                for attachment in attachment_metadata:
                    artifact_id = str(attachment.get("artifact_id") or "").strip()
                    if artifact_id:
                        try:
                            delete_artifact(artifact_id)
                        except Exception:
                            logger.exception(
                                "Failed to roll back provider attachment artifact_id=%s",
                                artifact_id,
                            )
            if attachment_rows and _is_attachment_storage_misconfigured(exc):
                raise ServiceUnavailableError(
                    "Attachment storage is unavailable (FS_SECRET_KEY / cryptography required)"
                ) from exc
            raise ServiceError("Failed to enqueue provider task") from exc

        return ProviderWebhookIngestResultView(
            provider_key=provider_key,
            task_id=task_id,
            job_id=str(job_id),
            queue=f"platform.project.{project_id}",
            file_count=len(attachment_metadata),
        )

    def ingest_reaction_event(
        self,
        command: ReactionEventIngestInput,
        *,
        scope: ScopeContext | None = None,
        expected_project_id: Any | None = None,
    ) -> ReactionEventIngestResultView:
        resolved_scope = _service_scope(scope, project_id=None, workflow_id=command.workflow_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)

        resolved_workflow_id = str(command.workflow_id or "").strip()
        resolved_project_id: Any | None = None
        job_id = str(command.job_id or "").strip() if command.job_id is not None else None
        notification_deliveries: list[dict[str, Any]] = []
        # The callback/ingress registration pins the project the reaction was
        # issued for. An attacker-supplied job_id can point at a *different*
        # same-account project's job (FS-DEEP-CAND-048 / FS-DEEP-CAND-039);
        # account-level require_project_access alone does not catch that. Bind
        # the resolved job's project to the registration project and fail closed
        # on mismatch before resuming/dispatching.
        normalized_expected_project_id = (
            str(expected_project_id).strip() if expected_project_id is not None else ""
        )

        if job_id is not None:
            job_store = self._get_job_store() if self._get_job_store else bl
            job = job_store.get_job(job_id) if hasattr(job_store, "get_job") else None
            if not job:
                raise ServiceNotFoundError(ERR_JOB_NOT_FOUND)
            resolved_project_id = str(job.get("project_id") or "").strip() or None
            if not resolved_project_id:
                raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)
            if (
                normalized_expected_project_id
                and str(resolved_project_id) != normalized_expected_project_id
            ):
                raise ServiceForbiddenError(
                    "Reaction target job does not belong to the callback registration project"
                )
            if not hasattr(bl, "get_project"):
                raise ServiceUnavailableError("Project storage is unavailable")
            project = bl.get_project(str(resolved_project_id))
            if not project:
                raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)
            require_project_access(resolved_scope, dict(project))
            if not resolved_workflow_id:
                meta = self._to_json_obj((job or {}).get("meta")) or {}
                if isinstance(meta, dict):
                    resolved_workflow_id = str(
                        meta.get("selected_workflow_id")
                        or meta.get("requested_workflow_id")
                        or meta.get("workflow_id")
                        or ""
                    ).strip()

        if resolved_workflow_id and not self._workflow_exists(resolved_workflow_id):
            raise ServiceNotFoundError("Workflow not found")
        if resolved_workflow_id:
            require_workflow_access(resolved_scope, resolved_workflow_id)
        if not hasattr(bl, "log_job_reaction_event"):
            raise ServiceUnavailableError("Reaction event persistence is not available")

        try:
            event = bl.log_job_reaction_event(
                job_id=job_id,
                workflow_id=resolved_workflow_id or None,
                event_type=command.event_type,
                source=command.source,
                source_id=command.source_id,
                event_key=command.event_key,
                payload=command.payload,
                dedupe_key=command.dedupe_key,
                processed=False,
            )
            normalized_correlation_id = (
                str(command.dedupe_key or "").strip()
                or str(command.source_id or "").strip()
                or (
                    f"job:{job_id}:{str(command.event_type or '').strip()}"
                    if job_id is not None
                    else f"workflow:{resolved_workflow_id or 'event'}:{str(command.event_type or '').strip()}"
                )
            )
            from core.application.orchestration.models import build_event_envelope_v1

            event_envelope = build_event_envelope_v1(
                event_type=command.event_type,
                account_id=resolved_scope.account_id,
                correlation_id=normalized_correlation_id,
                severity=str((command.payload or {}).get("severity") or "info"),
                summary=str(
                    (command.payload or {}).get("summary") or command.event_type or ""
                ).strip()
                or command.event_type,
                payload={
                    "source": command.source,
                    "source_id": command.source_id,
                    "event_key": command.event_key,
                    "payload": command.payload,
                },
                project_id=resolved_project_id,
                workflow_id=resolved_workflow_id or None,
                job_id=job_id,
                source=command.source,
                aggregate_type="job" if job_id is not None else "workflow",
                aggregate_id=str(job_id if job_id is not None else resolved_workflow_id or ""),
                actor={
                    "type": str(resolved_scope.principal_type or "system_actor").strip()
                    or "system_actor",
                    "id": str(
                        resolved_scope.actor_id or resolved_scope.actor_name or "system"
                    ).strip()
                    or "system",
                },
                metadata={
                    "trace_id": f"job:{job_id}" if job_id is not None else None,
                    "event_key": str(command.event_key or "").strip() or None,
                    "source_id": str(command.source_id or "").strip() or None,
                },
                event_id=str((event or {}).get("event_id") or "").strip() or None,
            )
            if hasattr(bl, "enqueue_domain_event"):
                bl.enqueue_domain_event(
                    event_type=command.event_type,
                    aggregate_type="job" if job_id is not None else "workflow",
                    aggregate_id=str(job_id if job_id is not None else resolved_workflow_id or ""),
                    payload={
                        "job_id": job_id,
                        "workflow_id": resolved_workflow_id or None,
                        "source": command.source,
                        "source_id": command.source_id,
                        "event_key": command.event_key,
                        "payload": command.payload,
                    },
                    account_id=resolved_scope.account_id,
                    project_id=resolved_project_id,
                    workflow_id=resolved_workflow_id or None,
                    job_id=job_id,
                    severity=str((command.payload or {}).get("severity") or "info"),
                    summary=str((event_envelope or {}).get("summary") or command.event_type),
                    actor=(event_envelope or {}).get("actor"),
                    envelope=event_envelope,
                    correlation_id=normalized_correlation_id,
                )
            reactions: list[dict[str, Any]] = []
            approval_resolution: dict[str, Any] | None = None
            if isinstance(event, dict) and not bool(event.get("processed")):
                event["workflow_id"] = (
                    str(event.get("workflow_id") or resolved_workflow_id or "").strip() or None
                )
                event["account_id"] = resolved_scope.account_id
                event["correlation_id"] = normalized_correlation_id
                event["event_id"] = (
                    str((event_envelope or {}).get("event_id") or "").strip() or None
                )
                event["envelope"] = dict(event_envelope)
                reactions = (
                    self._evaluate_workflow_reactions(
                        bl,
                        event,
                        scope=resolved_scope,
                    )
                    if scope is not None
                    else self._evaluate_workflow_reactions(bl, event)
                )
                did_execute = any(self._reaction_action_result_executed(item) for item in reactions)
                from .approval_outcome import build_approval_resolution

                approval_resolution = build_approval_resolution(
                    event_type=command.event_type,
                    source=command.source,
                    payload=dict(command.payload or {}),
                    reactions=[dict(item) for item in reactions if isinstance(item, dict)],
                )
                event_id_value = str(event.get("id") or "").strip() or None
                if (
                    (did_execute or approval_resolution is not None)
                    and hasattr(bl, "mark_job_reaction_event_processed")
                    and event_id_value is not None
                ):
                    updated = bl.mark_job_reaction_event_processed(event_id_value, processed=True)
                    if isinstance(updated, dict):
                        event = updated
                        event["workflow_id"] = (
                            str(event.get("workflow_id") or resolved_workflow_id or "").strip()
                            or None
                        )
                        event["account_id"] = resolved_scope.account_id
                        event["correlation_id"] = normalized_correlation_id
                        event["event_id"] = (
                            str((event_envelope or {}).get("event_id") or "").strip() or None
                        )
                        event["envelope"] = dict(event_envelope)
            if resolved_project_id:
                from .approval_outcome import is_approval_decision

                approval_decision = is_approval_decision(
                    command.event_type, dict(command.payload or {})
                )
                if approval_decision:
                    if approval_resolution is not None:
                        from .approval_outcome import (
                            approval_resolution_direct_route,
                            approval_resolution_event_id,
                        )

                        direct_route = approval_resolution_direct_route(approval_resolution)
                        if direct_route is not None:
                            notification_deliveries = list(
                                self._dispatch_project_notification_event(
                                    bl,
                                    project_id=resolved_project_id,
                                    event_type="approval.resolved",
                                    severity=(
                                        "info"
                                        if bool(approval_resolution.get("applied"))
                                        else "warning"
                                    ),
                                    workflow_id=resolved_workflow_id or None,
                                    job_id=job_id,
                                    payload=approval_resolution,
                                    event_id=approval_resolution_event_id(
                                        normalized_correlation_id
                                    ),
                                    account_id=resolved_scope.account_id,
                                    correlation_id=(
                                        f"{normalized_correlation_id}:approval.resolved"
                                    ),
                                    direct_routes=[direct_route],
                                    ignore_filters=True,
                                )
                                or []
                            )
                else:
                    notification_deliveries = list(
                        self._dispatch_project_notification_event(
                            bl,
                            project_id=resolved_project_id,
                            event_type=command.event_type,
                            severity=str((command.payload or {}).get("severity") or "info"),
                            workflow_id=resolved_workflow_id or None,
                            job_id=job_id,
                            payload={
                                "source": command.source,
                                "source_id": command.source_id,
                                "event_key": command.event_key,
                                "payload": command.payload,
                            },
                            event_id=str((event_envelope or {}).get("event_id") or "").strip()
                            or None,
                            account_id=resolved_scope.account_id,
                            correlation_id=normalized_correlation_id,
                        )
                        or []
                    )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("Failed to ingest reaction event") from exc

        return ReactionEventIngestResultView(
            event=event if isinstance(event, dict) else {},
            reactions=[dict(row) for row in reactions if isinstance(row, dict)],
            notification_deliveries=[
                dict(row) for row in (notification_deliveries or []) if isinstance(row, dict)
            ],
        )

    def update_workflow_reactions(
        self,
        workflow_id: str,
        command: WorkflowReactionsUpdateInput,
        *,
        scope_kind: str = "workflow",
        scope_id: str = "",
        scope: ScopeContext | None = None,
    ) -> WorkflowReactionsView:
        resolved = _service_scope(scope, workflow_id=workflow_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        if not self._workflow_exists(workflow_id):
            raise ServiceNotFoundError("Workflow not found")
        require_workflow_access(resolved, workflow_id)
        # Reaction rules that resume/approve/reject/cancel/retry jobs are privileged
        # automation: workflow access proves the caller can edit the workflow, not
        # that they may persist job-control side effects. Require operator role when
        # any submitted rule carries such an action type.
        privileged_reaction_actions = {
            "resume_job",
            "resume_orchestrator",
            "approve_job",
            "reject_job",
            "cancel_job",
            "retry_step",
        }
        requested_action_types = {
            str((getattr(rule, "action", None) or {}).get("type") or "").strip().lower()
            for rule in command.reactions
        }
        if requested_action_types & privileged_reaction_actions:
            _require_role(resolved, "operator")
        if not hasattr(bl, "replace_scoped_workflow_reactions"):
            raise ServiceError("Workflow reaction persistence is not available")
        if resolved.project_id is not None:
            if not hasattr(bl, "get_project"):
                raise ServiceUnavailableError("Project storage is not available")
            project = bl.get_project(resolved.project_id)
            if not project:
                raise ServiceNotFoundError(ERR_PROJECT_NOT_FOUND)
            require_project_access(resolved, project)

        try:
            available_trigger_sources = build_workflow_reaction_trigger_sources(
                bl,
                project_id=resolved.project_id,
                scope_kind=scope_kind,
            )
            validate_workflow_reaction_rules(
                [
                    row.model_dump() if hasattr(row, "model_dump") else dict(row)
                    for row in command.reactions
                ],
                available_sources=available_trigger_sources,
                scope_kind=scope_kind,
            )
            validate_notification_reaction_actions(
                [row.model_dump() for row in command.reactions],
                extension_for=lambda key: (
                    bl.get_extension(key, account_id=resolved.account_id)
                    if hasattr(bl, "get_extension")
                    else None
                ),
            )
            target_kind, target_scope_id, scoped_rows = bl.replace_scoped_workflow_reactions(
                workflow_id,
                [row.model_dump() for row in command.reactions],
                scope_kind=scope_kind,
                scope_id=scope_id,
                account_id=resolved.account_id,
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("Failed to update workflow reactions") from exc

        return WorkflowReactionsView(
            workflow_id=str(workflow_id or "").strip(),
            scope_kind=target_kind,
            scope_id=target_scope_id,
            project_id=resolved.project_id,
            available_trigger_sources=available_trigger_sources,
            reactions=[WorkflowReactionRuleView.model_validate(row) for row in scoped_rows],
        )


__all__ = [name for name in globals() if not name.startswith("__")]
