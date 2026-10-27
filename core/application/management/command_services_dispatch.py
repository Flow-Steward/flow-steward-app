# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


class CommandDispatchService:
    """Converges extensions, reactions, MCP, and API onto a single command execution path."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_registry_manager: Callable[[], Any] | None = None,
        workflow_availability_assert: Callable[..., Any] | None = None,
        event_command_service_factory: Callable[[], Any] | None = None,
        project_command_service_factory: Callable[[], Any] | None = None,
        job_command_service_factory: Callable[[], Any] | None = None,
        approval_command_service_factory: Callable[[], Any] | None = None,
        get_extension_store: Callable[[], Any] | None = None,
    ):
        self._get_logger = get_logger
        self._get_registry_manager = get_registry_manager
        self._workflow_availability_assert = workflow_availability_assert
        self._event_command_service_factory = event_command_service_factory
        self._project_command_service_factory = project_command_service_factory
        self._job_command_service_factory = job_command_service_factory
        self._approval_command_service_factory = approval_command_service_factory
        self._get_extension_store = get_extension_store

    @staticmethod
    def _audit_ports(bl: Any) -> tuple[Callable[..., Any], Callable[..., Any]]:
        # A bare MagicMock manufactures arbitrary attributes. Durable audit
        # ports must be declared by the concrete store, including inherited ports.
        if not callable(getattr(type(bl), "log_command_execution", None)) or not callable(
            getattr(type(bl), "update_command_execution", None)
        ):
            raise ServiceUnavailableError("Command audit persistence port is unavailable")
        canonical_accepted = bl.log_command_execution
        canonical_final = bl.update_command_execution
        if not callable(canonical_accepted) or not callable(canonical_final):
            raise ServiceUnavailableError("Command audit persistence port is unavailable")

        def accepted(**fields: Any) -> Any:
            # Transport correlation fields are not command audit table columns.
            fields.pop("source", None)
            fields.pop("correlation_id", None)
            return canonical_accepted(**fields)

        return accepted, canonical_final

    @staticmethod
    def _command_port(
        factory: Callable[[], Any] | None,
        method_name: str,
        unavailable_message: str,
    ) -> Callable[..., Any]:
        if factory is None:
            raise ServiceUnavailableError(unavailable_message)
        port = getattr(factory(), method_name, None)
        if not callable(port):
            raise ServiceUnavailableError(unavailable_message)
        return port

    @staticmethod
    def _finalize_audit(
        final: Callable[..., Any],
        *,
        command_id: str,
        status: str,
        result: dict[str, Any],
        error_text: str | None,
    ) -> None:
        persisted = final(
            command_id=command_id,
            status=status,
            result=result,
            error_text=error_text,
        )
        if persisted is None or persisted is False:
            raise ServiceUnavailableError("Command audit finalization did not persist")

    @staticmethod
    def _transition_job(bl: Any, job_id: str, status: str) -> None:
        update = getattr(bl, "update_job", None)
        if not callable(update):
            raise ServiceUnavailableError("Durable job transition port is unavailable")
        update(job_id, status=status)
        current = bl.get_job(job_id)
        if not isinstance(current, dict) or str(current.get("status") or "") != status:
            raise ServiceUnavailableError("Durable job transition could not be verified")

    def _assert_can_start_workflow(
        self,
        *,
        workflow_id: str,
        account_id: str,
        project_id: Any = None,
    ) -> None:
        normalized_workflow_id = str(workflow_id or "").strip()
        if not normalized_workflow_id:
            return
        if self._workflow_availability_assert is None:
            return
        self._workflow_availability_assert(
            workflow_id=normalized_workflow_id,
            account_id=str(account_id or "").strip() or "acc_default",
            project_id=str(project_id) if project_id is not None else None,
        )

    def _assert_job_scope(self, bl: Any, job_id: str, scope: ScopeContext) -> dict[str, Any]:
        get_job = getattr(bl, "get_job", None)
        job = get_job(job_id) if callable(get_job) else None
        if not isinstance(job, dict):
            raise ServiceNotFoundError(f"Job '{job_id}' not found")
        require_account_access(scope, job)
        if scope.allowed_project_ids:
            project_id = str(job.get("project_id") or "").strip()
            if project_id and project_id not in scope.allowed_project_ids:
                raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
        return job

    def _assert_project_scope(
        self, bl: Any, project_id: Any, scope: ScopeContext
    ) -> dict[str, Any]:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise ServiceValidationError("target.project_id is required")
        get_project = getattr(bl, "get_project", None)
        project = get_project(normalized_project_id) if callable(get_project) else None
        if not isinstance(project, dict):
            raise ServiceNotFoundError(f"Project '{normalized_project_id}' not found")
        require_project_access(scope, project)
        return project

    def _assert_extension_scope(self, extension: dict[str, Any], scope: ScopeContext) -> None:
        require_account_access(scope, extension)

    def dispatch(
        self,
        inp: CommandDispatchInput,
        *,
        scope: ScopeContext | None = None,
    ) -> CommandDispatchResultView:
        from .command_models import CommandDispatchInput, CommandDispatchResultView

        inp = CommandDispatchInput.model_validate(inp)
        scope = _service_scope(scope)
        bl = self._get_logger()

        from core.infrastructure.db.extension_response import KNOWN_COMMAND_CATALOG

        command_type = str(inp.command_type or "").strip()
        if not command_type:
            raise ServiceValidationError("command_type is required")
        if command_type not in KNOWN_COMMAND_CATALOG:
            raise ServiceValidationError(f"Unknown command_type '{command_type}'")

        command_id = f"cmd_{uuid.uuid4().hex[:16]}"
        audit_fields = _scope_audit_fields(scope)

        accepted_audit, final_audit = self._audit_ports(bl)
        persisted_audit = accepted_audit(
            command_id=command_id,
            command_type=command_type,
            account_id=audit_fields.get("account_id", ""),
            actor_type=audit_fields.get("actor_type", "system"),
            actor_id=audit_fields.get("actor_id", ""),
            target=inp.target,
            payload=inp.payload,
            status="accepted",
            source=inp.source,
            correlation_id=inp.correlation_id,
        )
        if persisted_audit is None or persisted_audit is False:
            raise ServiceUnavailableError("Command audit acceptance did not persist")

        result_data: dict[str, Any] = {}
        status = "executed"
        errors: list[dict[str, Any]] = []

        try:
            result_data = self._route_command(
                command_type,
                inp.target,
                inp.payload,
                scope=scope,
                bl=bl,
                command_id=command_id,
                source=inp.source,
                correlation_id=inp.correlation_id,
            )
        except ServiceError as exc:
            self._finalize_audit(
                final_audit,
                command_id=command_id,
                status="failed",
                result={},
                error_text=exc.message,
            )
            raise
        except Exception as exc:
            logger.warning(
                "command dispatch failed: type=%s error=%s", command_type, exc, exc_info=True
            )
            self._finalize_audit(
                final_audit,
                command_id=command_id,
                status="failed",
                result={},
                error_text=str(exc),
            )
            raise ServiceError("Command execution failed", status_code=500) from exc

        self._finalize_audit(
            final_audit,
            command_id=command_id,
            status=status,
            result=result_data,
            error_text=None,
        )

        return CommandDispatchResultView(
            command_id=command_id,
            command_type=command_type,
            status=status,
            result=result_data,
            errors=errors,
        )

    def dispatch_batch(
        self,
        inp: CommandBatchInput,
        *,
        scope: ScopeContext | None = None,
    ) -> CommandBatchResultView:
        from .command_models import CommandBatchInput, CommandBatchResultView

        inp = CommandBatchInput.model_validate(inp)
        results = []
        for cmd_input in inp.commands:
            try:
                result = self.dispatch(cmd_input, scope=scope)
            except ServiceError as exc:
                from .command_models import CommandDispatchResultView

                result = CommandDispatchResultView(
                    command_type=cmd_input.command_type,
                    status="failed",
                    errors=[{"code": "service_error", "message": exc.message}],
                )
            results.append(result)
        return CommandBatchResultView(results=results)

    def _route_command(
        self,
        command_type: str,
        target: dict[str, Any],
        payload: dict[str, Any],
        *,
        scope: ScopeContext | None,
        bl: Any,
        command_id: str | None = None,
        source: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Route a command to the appropriate existing service method."""
        if (
            command_type in {"invoke_extension_action", "update_external_status"}
            and self._get_extension_store is not None
        ):
            bl = self._get_extension_store()

        # Job lifecycle controls are operator-level authority, not ordinary project
        # access: account/project object scope alone (``_assert_job_scope``) proves
        # the caller can *see* the job, not that they may pause/resume/cancel it.
        if command_type in {"pause_job", "resume_job", "cancel_job"}:
            _require_role(scope, "operator")

        if command_type == "pause_job":
            job_id = _required_entity_id(target.get("job_id"), TARGET_JOB_ID_VALUE)
            self._assert_job_scope(bl, job_id, scope)
            self._transition_job(bl, job_id, "paused")
            return {"job_id": job_id, "action": "paused"}

        if command_type == "resume_job":
            job_id = _required_entity_id(target.get("job_id"), TARGET_JOB_ID_VALUE)
            self._assert_job_scope(bl, job_id, scope)
            self._transition_job(bl, job_id, "in_progress")
            return {"job_id": job_id, "action": "resumed"}

        if command_type == "cancel_job":
            job_id = _required_entity_id(target.get("job_id"), TARGET_JOB_ID_VALUE)
            self._assert_job_scope(bl, job_id, scope)
            cancel_job = self._command_port(
                self._job_command_service_factory,
                "cancel_job",
                "Job cancellation port is unavailable",
            )
            cancel_job(job_id, scope=scope)
            return {"job_id": job_id, "action": "cancelled"}

        if command_type == "start_workflow":
            from core.application.management.guards import require_workflow_access

            workflow_id = str(target.get("workflow_id") or "").strip()
            project_id = target.get("project_id")
            if not workflow_id:
                raise ServiceValidationError("target.workflow_id is required for start_workflow")
            require_workflow_access(scope, workflow_id)
            normalized_project_id = str(project_id or "").strip() or None
            if normalized_project_id:
                self._assert_project_scope(bl, normalized_project_id, scope)
            self._assert_can_start_workflow(
                workflow_id=workflow_id,
                account_id=str(
                    (target or {}).get("account_id")
                    or (payload or {}).get("account_id")
                    or getattr(scope, "account_id", "")
                    or "acc_default"
                ).strip(),
                project_id=normalized_project_id,
            )
            enqueue_manual_task = self._command_port(
                self._project_command_service_factory,
                "enqueue_manual_task",
                "Workflow start port is unavailable",
            )
            from .command_models import ProjectTaskEnqueueInput

            task = ProjectTaskEnqueueInput(
                project_id=project_id,
                task_id=str(correlation_id or f"command-{uuid.uuid4().hex}"),
                task_summary=str(payload.get("task_summary") or "Command workflow run"),
                workflow_id=workflow_id,
                task_source="command_dispatch",
                entry_path="manual_api",
                inputs=dict(payload.get("inputs") or {}),
                extra_state=dict(payload.get("extra_state") or {}),
                job_meta=dict(payload.get("job_meta") or {}),
            )
            queued = enqueue_manual_task(
                task,
                allow_implicit_project=not bool(normalized_project_id),
                trusted_internal=True,
                scope=scope,
            )
            job_id = str(getattr(queued, "job_id", "") or "").strip()
            if not job_id:
                raise ServiceUnavailableError("Workflow start did not create a durable job")
            return {"job_id": job_id, "action": "started"}

        if command_type == "invoke_extension_action":
            target = dict(target or {})
            business_target = {
                key: value
                for key, value in target.items()
                if key
                not in {
                    "extension_id",
                    "action_id",
                    "project_id",
                    "connection_id",
                    "connection",
                    "runtime_context",
                    "test_mode",
                }
            }
            payload = dict(payload or {})
            ext_id = str(target.get("extension_id") or "").strip()
            action_id = str(target.get("action_id") or "").strip()
            if not ext_id or not action_id:
                raise ServiceValidationError(
                    "target.extension_id and target.action_id are required for invoke_extension_action"
                )
            # Use extension runtime directly — ExtensionBindingService requires
            # dependency callables that are only available in serve.py context.
            from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

            extension = None
            if bl and hasattr(bl, "get_extension"):
                extension = bl.get_extension(ext_id)
            if not extension:
                raise ServiceNotFoundError(f"Extension '{ext_id}' not found")
            self._assert_extension_scope(extension, scope)
            if extension.get("enabled") is False:
                raise ServiceConflictError(ERR_EXTENSION_DISABLED)

            timeout = int(payload.get("timeout_seconds") or 30)
            provider_payload = {
                key: value for key, value in payload.items() if key != "timeout_seconds"
            }
            audit = _scope_audit_fields(scope)
            runtime_ctx: dict[str, Any] = {**audit}
            if bl is not None:
                runtime_ctx["logger"] = bl
            external_effect: dict[str, Any] = {}
            external_effect_id = ""
            external_effect_claim_token = ""
            from core.application.extensions import external_effects

            external_effect_declaration = (
                external_effects.external_effect_declaration_for_operation(
                    extension,
                    action_id,
                )
            )
            if external_effect_declaration:
                from core.application.extensions.operation_targets import (
                    _direct_action_connection_target,
                    _direct_action_project_id,
                    _target_connection_id_for_external_effect,
                )

                project_id = _direct_action_project_id(
                    scope=scope,
                    context=payload,
                    target=target,
                )
                if not project_id:
                    raise ServiceValidationError(
                        "target.connection_id for an external-effect action requires project context"
                    )
                self._assert_project_scope(bl, project_id, scope)
                connection_id = _target_connection_id_for_external_effect(target)
                if not connection_id:
                    raise ServiceValidationError(
                        "target.connection_id is required for external-effect actions"
                    )
                runtime_connection = _direct_action_connection_target(
                    bl,
                    connection_id=connection_id,
                    account_id=str(audit.get("account_id") or DEFAULT_ACCOUNT_ID).strip()
                    or DEFAULT_ACCOUNT_ID,
                    project_id=project_id,
                    extension_id=ext_id,
                )
                target = {
                    **target,
                    "project_id": project_id,
                    "connection_id": runtime_connection["connection_id"],
                    "connection": runtime_connection,
                }
                audit = {
                    **audit,
                    "project_id": project_id,
                    "connection_id": runtime_connection["connection_id"],
                }
                runtime_ctx.update(
                    {
                        "project_id": project_id,
                        "connection_id": runtime_connection["connection_id"],
                    }
                )

            command_intent_id = str(correlation_id or "").strip()
            if external_effect_declaration and not command_intent_id:
                raise ServiceValidationError(
                    "A durable correlation_id is required for external-effect commands"
                )
            command_intent_id = command_intent_id or str(command_id or "").strip()
            command_intent_digest = external_effects.external_effect_identity_hmac(
                purpose="command-intent",
                value={"command_intent_id": command_intent_id},
            )
            effect_claim = external_effects.claim_external_effect_for_action(
                bl,
                extension=extension,
                account_id=str(audit.get("account_id") or "").strip(),
                project_id=str(audit.get("project_id") or target.get("project_id") or "").strip()
                or None,
                extension_id=ext_id,
                extension_install_id=str(
                    extension.get("extension_install_id") or extension.get("install_id") or ""
                ).strip()
                or None,
                connection_id=str(
                    target.get("connection_id")
                    or _as_dict(target.get("connection")).get("connection_id")
                    or ""
                ).strip()
                or None,
                operation_id=action_id,
                source_type="command_dispatch",
                source_ref={
                    "command_type": command_type,
                    "correlation_id": correlation_id,
                    "command_intent_id": command_intent_id,
                    "source": source,
                },
                request_payload={
                    "input": provider_payload,
                    "target": business_target,
                },
                idempotency_key_override=command_intent_digest,
                test_mode=bool(payload.get("test_mode") or target.get("test_mode")),
            )
            external_effect = _as_dict(effect_claim.get("effect"))
            external_effect_id = str(external_effect.get("effect_id") or "").strip()
            external_effect_claim_token = str(external_effect.get("claim_token") or "").strip()
            if effect_claim.get("declared") and not effect_claim.get("should_spawn"):
                from core.application.extensions.external_effect_replay import (
                    hydrate_external_effect_claim,
                )

                effect_claim = hydrate_external_effect_claim(bl, effect_claim)
                if effect_claim.get("error"):
                    raise ServiceError(
                        str(effect_claim.get("error") or "external_effect_claim_failed"),
                        status_code=502,
                    )
                replay_invocation = _as_dict(effect_claim.get("replay_invocation"))
                if replay_invocation:
                    return {
                        "extension_id": ext_id,
                        "action_id": action_id,
                        "action": "executed",
                        "status": replay_invocation.get("status"),
                        "detail": replay_invocation.get("detail"),
                        "errors": list(replay_invocation.get("errors") or []),
                        "invocation": replay_invocation,
                        "external_effect": external_effect,
                        "project_state_updates": None,
                    }
                return {
                    "extension_id": ext_id,
                    "action_id": action_id,
                    "action": "deduped",
                    "status": external_effect.get("status"),
                    "external_effect": external_effect,
                }
            from core.infrastructure.extension_runtime.extension_runtime_helpers import (
                extension_operation_handler_mode,
            )

            if (
                extension_operation_handler_mode(
                    extension, operation_type="action", operation_id=action_id
                )
                == "platform"
            ):
                from core.application.extensions.platform_connection_operations import (
                    install_platform_connection_operation,
                )

                try:
                    install_platform_connection_operation(runtime_ctx)
                except Exception as exc:
                    if external_effect_id:
                        external_effects.fail_external_effect_before_spawn(
                            bl,
                            effect_id=external_effect_id,
                            claim_token=external_effect_claim_token,
                            code="external_effect_pre_spawn_setup_failed",
                        )
                    raise ServiceError(
                        "Extension action failed before provider invocation",
                        status_code=503,
                    ) from exc
            if external_effect_id:
                runtime_ctx["external_effect_id"] = external_effect_id
                try:
                    running_effect = external_effects.mark_external_effect_running(
                        bl,
                        effect_id=external_effect_id,
                        claim_token=external_effect_claim_token,
                    )
                    if not running_effect:
                        with contextlib.suppress(Exception):
                            external_effects.fail_external_effect_before_spawn(
                                bl,
                                effect_id=external_effect_id,
                                claim_token=external_effect_claim_token,
                                code="external_effect_pre_spawn_setup_failed",
                            )
                        raise ServiceError(
                            "External effect lease was reclaimed before invocation started",
                            status_code=409,
                        )
                except ServiceError:
                    raise
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        external_effects.fail_external_effect_before_spawn(
                            bl,
                            effect_id=external_effect_id,
                            claim_token=external_effect_claim_token,
                            code="external_effect_pre_spawn_setup_failed",
                        )
                    raise ServiceError(
                        "External effect lease could not be confirmed before invocation",
                        status_code=503,
                    ) from exc
            try:
                invocation = run_extension_action(
                    extension,
                    action_id=action_id,
                    target=target,
                    input_payload=provider_payload,
                    timeout_seconds=timeout,
                    runtime_context=runtime_ctx,
                    scope=scope,
                )
            except Exception:
                if not external_effect_id:
                    raise
                invocation = external_effects.external_effect_spawn_exception_invocation()
            if invocation.get("status") == "succeeded":
                try:
                    # Process envelope (commands/events from extension response)
                    _process_extension_response_envelope(
                        invocation,
                        extension,
                        ext_id,
                        scope=scope,
                        audit=audit,
                        bl=bl,
                        get_logger=self._get_logger,
                        event_command_service_factory=self._event_command_service_factory,
                    )
                except Exception as exc:
                    invocation = {
                        **invocation,
                        "status": "failed",
                        "detail": "response_envelope_processing_failed",
                        "errors": [
                            *list(invocation.get("errors") or []),
                            {"code": "response_envelope_processing_failed", "message": str(exc)},
                        ],
                    }
            if external_effect_id:
                try:
                    from core.application.extensions.secret_reflection import (
                        collect_secret_values,
                    )

                    protected_values, protected_budget_exceeded = collect_secret_values(
                        _as_dict(target.get("connection")).get("secrets")
                    )
                    if protected_budget_exceeded:
                        raise RuntimeError("external_effect_replay_secret_budget_exceeded")
                    confirmed_effect = external_effects.confirm_external_effect_from_invocation(
                        bl,
                        effect_id=external_effect_id,
                        invocation=invocation,
                        claim_token=external_effect_claim_token,
                        account_id=str(audit.get("account_id") or "").strip(),
                        extension=extension,
                        operation_id=action_id,
                        protected_values=protected_values,
                        expected_project_id=str(audit.get("project_id") or "").strip() or None,
                        expected_connection_id=str(
                            target.get("connection_id")
                            or _as_dict(target.get("connection")).get("connection_id")
                            or ""
                        ).strip()
                        or None,
                    )
                    if confirmed_effect:
                        invocation = external_effects.apply_external_effect_confirmation(
                            invocation,
                            confirmed_effect,
                        )
                        external_effect = external_effects.public_confirmed_external_effect(
                            confirmed_effect
                        )
                except Exception as exc:
                    logger.warning("failed to confirm external effect: %s", exc)
                    invocation = external_effects.external_effect_confirmation_failure_invocation(
                        exc
                    )
            if invocation.get("status") != "succeeded":
                raise ServiceError(
                    str(invocation.get("detail") or "Extension action failed"),
                    status_code=502,
                    payload={
                        "extension_id": ext_id,
                        "action_id": action_id,
                        "action": invocation,
                        "errors": list(invocation.get("errors") or []),
                        "external_effect": external_effect,
                    },
                )

            return {
                "extension_id": ext_id,
                "action_id": action_id,
                "action": "executed",
                "status": invocation.get("status"),
                "detail": invocation.get("detail"),
                "errors": list(invocation.get("errors") or []),
                "invocation": invocation,
                "external_effect": external_effect,
                "project_state_updates": invocation.get("project_state_updates"),
            }

        if command_type == "retry_step":
            job_id = _required_entity_id(target.get("job_id"), TARGET_JOB_ID_VALUE)
            self._assert_job_scope(bl, job_id, scope)
            step_id = str(target.get("step_id") or "").strip()
            retry_interrupted_step = self._command_port(
                self._job_command_service_factory,
                "retry_interrupted_step",
                "Step retry port is unavailable",
            )
            from .command_models import JobRetryInterruptedStepInput

            retried = retry_interrupted_step(
                job_id,
                JobRetryInterruptedStepInput(requested_by=scope.actor_name),
                scope=scope,
            )
            return {
                "job_id": job_id,
                "step_id": step_id,
                "new_job_id": str(getattr(retried, "new_job_id", "") or ""),
                "action": "retried",
            }

        if command_type == "request_approval":
            job_id = _required_entity_id(target.get("job_id"), TARGET_JOB_ID_VALUE)
            self._assert_job_scope(bl, job_id, scope)
            self._transition_job(bl, job_id, "awaiting_approval")
            return {"job_id": job_id, "action": "approval_requested"}

        if command_type == "resolve_approval":
            job_id = _required_entity_id(target.get("job_id"), TARGET_JOB_ID_VALUE)
            self._assert_job_scope(bl, job_id, scope)
            approved = bool(payload.get("approved", False))
            resume_task = self._command_port(
                self._approval_command_service_factory,
                "resume_task",
                "Approval resolution port is unavailable",
            )
            from .command_models import ApprovalDecisionInput

            approval_id = str(target.get("approval_id") or payload.get("approval_id") or "")
            if not approval_id:
                raise ServiceValidationError("target.approval_id is required")
            job = self._assert_job_scope(bl, job_id, scope)
            thread_id = str(job.get("thread_id") or "").strip()
            if not thread_id:
                raise ServiceConflictError("Job has no approval thread")
            resolved = resume_task(
                thread_id=thread_id,
                scope=scope,
                decision=ApprovalDecisionInput(
                    approved=approved,
                    action=payload.get("action"),
                    comment=str(payload.get("comment") or ""),
                    override_content=str(payload.get("override_content") or ""),
                    approval_id=approval_id,
                ),
                idempotency_key=str(correlation_id or ""),
            )
            return {
                "job_id": job_id,
                "approval_id": approval_id,
                "approved": approved,
                "status": str(getattr(resolved, "status", "") or "in_progress"),
                "action": "approval_resolved",
            }

        if command_type == "enqueue_followup_job":
            from core.application.management.guards import require_workflow_access

            workflow_id = str(target.get("workflow_id") or "").strip()
            require_workflow_access(scope, workflow_id)
            parent_job_id = target.get("parent_job_id")
            if parent_job_id:
                self._assert_job_scope(bl, str(parent_job_id), scope)
            project_id = target.get("project_id")
            if project_id:
                self._assert_project_scope(bl, project_id, scope)
            enqueue_manual_task = self._command_port(
                self._project_command_service_factory,
                "enqueue_manual_task",
                "Follow-up enqueue port is unavailable",
            )
            from .command_models import ProjectTaskEnqueueInput

            queued = enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id=project_id,
                    task_id=str(correlation_id or f"followup-{uuid.uuid4().hex}"),
                    task_summary=str(
                        payload.get("task_summary") or f"Followup from job {parent_job_id}"
                    ),
                    workflow_id=workflow_id,
                    task_source="command_dispatch",
                    entry_path="manual_api",
                    inputs=dict(payload.get("inputs") or {}),
                    extra_state=dict(payload.get("extra_state") or {}),
                    job_meta={"parent_job_id": parent_job_id} if parent_job_id else {},
                ),
                allow_implicit_project=not bool(project_id),
                trusted_internal=True,
                scope=scope,
            )
            job_id = str(getattr(queued, "job_id", "") or "").strip()
            if not job_id:
                raise ServiceUnavailableError("Follow-up enqueue did not create a durable job")
            return {"action": "enqueued", "job": {"id": job_id}}

        if command_type == "update_external_status":
            ext_id = str(target.get("extension_id") or target.get("provider") or "").strip()
            external_id = str(target.get("external_id") or target.get("task_id") or "").strip()
            new_status = str(payload.get("status") or payload.get("new_status") or "").strip()

            if not ext_id:
                raise ServiceValidationError(
                    "target.extension_id or target.provider is required for update_external_status"
                )
            if not external_id or not new_status:
                raise ServiceValidationError(
                    "target.external_id and payload.status are required for update_external_status"
                )
            if not bl or not hasattr(bl, "get_extension"):
                raise ServiceUnavailableError("Extension discovery is not available")

            extension = bl.get_extension(ext_id)
            if not extension:
                raise ServiceNotFoundError(f"Extension '{ext_id}' not found")
            self._assert_extension_scope(extension, scope)
            if not bool(extension.get("enabled")):
                raise ServiceConflictError(ERR_EXTENSION_DISABLED)

            # Route status sync through the registered extension action only.
            # Use caller-specified action_id or fall back to the standard
            # "sync_completion_status" action which shipped extensions implement.
            status_action_id = str(
                target.get("action_id") or payload.get("action_id") or "sync_completion_status"
            ).strip()
            from core.infrastructure.db.extension_response import normalize_extension_response
            from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

            ext_audit = _scope_audit_fields(scope)
            extension_target = dict(target or {})
            extension_target.setdefault("external_id", external_id)
            extension_target.setdefault("task_id", external_id)
            extension_input = {
                **dict(payload or {}),
                "external_id": external_id,
                "task_id": external_id,
                "source_id": external_id,
                "status": new_status,
                "new_status": new_status,
                "final_status": new_status,
            }
            status_runtime_context = {**ext_audit, "logger": bl}
            from core.application.extensions.platform_connection_operations import (
                install_platform_connection_operation,
            )
            from core.infrastructure.extension_runtime.extension_runtime_helpers import (
                extension_operation_handler_mode,
            )

            if (
                extension_operation_handler_mode(
                    extension,
                    operation_type="action",
                    operation_id=status_action_id,
                )
                == "platform"
            ):
                install_platform_connection_operation(status_runtime_context)
            result = run_extension_action(
                extension,
                action_id=status_action_id,
                target=extension_target,
                input_payload=extension_input,
                timeout_seconds=int(payload.get("timeout_seconds") or 15),
                runtime_context=status_runtime_context,
                scope=scope,
            )
            raw_response = result.get("response")
            raw_response_dict = raw_response if isinstance(raw_response, dict) else {}
            envelope = normalize_extension_response(
                raw_response if isinstance(raw_response, dict) else {}
            )
            result["_envelope"] = envelope.model_dump(mode="json")
            logged_result = dict(result)
            if result.get("status") == "succeeded" and not envelope.ok:
                error_entry = envelope.errors[0] if envelope.errors else {}
                logged_result["status"] = "failed"
                logged_result["detail"] = str(
                    result.get("detail")
                    or error_entry.get("message")
                    or raw_response_dict.get("error")
                    or f"Extension {status_action_id} action failed"
                ).strip()
            log_host_extension_invocation(
                bl=bl,
                extension=extension,
                extension_id=ext_id,
                invocation_type=f"action:{status_action_id}",
                request={
                    "external_id": external_id,
                    "new_status": new_status,
                    "extension_target": extension_target,
                    "extension_input": extension_input,
                },
                invocation=logged_result,
                runtime_context={**ext_audit, "logger": bl},
                request_context={
                    "command_type": "update_external_status",
                    "source": "command_dispatch.update_external_status",
                },
                action_id=status_action_id,
                command_id="update_external_status",
            )
            if result.get("status") != "succeeded" or not envelope.ok:
                error_entry = envelope.errors[0] if envelope.errors else {}
                raise ServiceError(
                    str(
                        result.get("detail")
                        or error_entry.get("message")
                        or raw_response_dict.get("error")
                        or f"Extension {status_action_id} action failed"
                    ),
                    status_code=502,
                    payload={
                        "extension_id": ext_id,
                        "action_id": status_action_id,
                        "external_id": external_id,
                        "new_status": new_status,
                        "action": result,
                        "response_envelope": envelope.model_dump(mode="json"),
                    },
                )
            return {
                "action": "status_updated",
                "extension_id": ext_id,
                "external_id": external_id,
                "new_status": new_status,
                "result": envelope.result or raw_response_dict,
            }

        raise ServiceValidationError(f"Unhandled command_type '{command_type}'")


__all__ = [name for name in globals() if not name.startswith("__")]
