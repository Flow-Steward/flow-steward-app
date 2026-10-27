"""Extension connector dispatch."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from core.application.extensions.operation_support import extension_operation_contract
from core.application.extensions.secret_reflection import (
    collect_secret_values,
    secret_exposure_invocation,
    value_exposes_secret,
)
from core.application.orchestration.connector_connection import (
    _build_runtime_connection_target,
    _connection_id_from_binding,
    _is_validation_only_test_mode,
    _load_extension_project_state_for_target,
    _operation_declares_connection_ref,
    _persist_connection_session_update,
    _strip_connection_session_updates,
    _test_mode_connection_failure,
)
from core.application.orchestration.connector_effects import (
    _claim_external_effect_for_action,
    _derive_extension_action,
    _external_effect_runtime_identity_error,
    _resolved_install_id,
)
from core.application.orchestration.connector_events import (
    _emit_connector_tool_event,
    _job_event_logger,
    _runtime_ids_from_state,
    _shared_logger,
    _step_update_sink,
)
from core.application.orchestration.connector_grants import (
    _artifact_policy_for_operation,
    _cleanup_prepared_output_grants,
    _grant_requests_from_policy,
    _prepare_grants_for_extension_action,
)
from core.application.orchestration.connector_mapping import (
    _HOST_TEST_MODE_SUPPRESSION_FLAG,
    _TOKEN_EXTENSION,
    _project_extension_output_aliases,
    _to_object,
)
from core.application.orchestration.step_activity import step_progress_reporter
from core.application.orchestration.step_outputs import early_output_handler
from core.infrastructure.extension_runtime.progress_channel import extension_progress_sink

logger = logging.getLogger(__name__)


def _dispatch_extension_runtime(
    *,
    connector_id: str,
    extension_id: str,
    connector_install_id: str,
    action_id: str,
    operation_id: str,
    input_payload: dict[str, Any],
    event_input_payload: dict[str, Any],
    protected_input_secrets: frozenset[str],
    protected_input_secret_budget_exceeded: bool,
    connection_binding: dict[str, Any],
    state: dict[str, Any],
    suppression_probe_only: bool = False,
    artifact_policy_operation_id: str | None = None,
) -> dict[str, Any] | None:
    ext_id, op_id = _derive_extension_action(
        connector_id=connector_id,
        extension_id=extension_id,
        action_id=action_id,
        operation_id=operation_id,
    )
    if not op_id:
        return None
    policy_operation_id = (
        op_id
        if artifact_policy_operation_id is None
        else str(artifact_policy_operation_id or "").strip()
    )
    from core.infrastructure.extension_runtime.extension_invocation_audit import (
        log_host_extension_invocation,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        resolve_extension_runtime_record,
    )
    from core.infrastructure.extension_runtime.extension_runtime_invocation import (
        extension_runtime_availability_failure,
    )

    install_id = str(connector_install_id or "").strip()
    if str(connector_id or "").strip().startswith(_TOKEN_EXTENSION) and not install_id:
        return {
            "handled": True,
            "ok": False,
            "runtime": "extension_action",
            "extension_id": ext_id,
            "action_id": op_id,
            "invocation": {},
            "detail": "extension_install_missing",
            "errors": [
                {
                    "code": "extension_install_missing",
                    "message": (
                        "Select the installed extension action again before running this step."
                    ),
                }
            ],
        }
    runtime_context = _runtime_ids_from_state(state)
    from core.application.orchestration.dataset_cursor_runtime import (
        dataset_page_context_from_state,
    )

    dataset_page_context = dataset_page_context_from_state(state)
    if suppression_probe_only and runtime_context.get("test_mode") is not True:
        return None
    runtime_account_id = str(runtime_context.get("account_id") or "").strip()
    if not runtime_account_id:
        return {
            "handled": True,
            "ok": False,
            "runtime": "extension_action",
            "extension_id": ext_id or install_id,
            "action_id": op_id,
            "invocation": {},
            "detail": "Connector extension action requires account-scoped runtime context",
        }
    from core.application.management.scope import normalize_scope

    runtime_scope = normalize_scope(
        account_id=runtime_account_id,
        tenant_id=runtime_context.get("tenant_id"),
        workspace_id=runtime_context.get("workspace_id"),
        project_id=runtime_context.get("project_id"),
        workflow_id=runtime_context.get("workflow_id"),
        actor_id=state.get("actor_id"),
        principal_type=state.get("principal_type"),
        allowed_project_ids=frozenset(
            {str(runtime_context.get("project_id") or "").strip()}
            if str(runtime_context.get("project_id") or "").strip()
            else frozenset()
        ),
    )
    resolution_candidates: list[str] = [install_id] if install_id else []
    if not install_id and ext_id:
        resolution_candidates.append(ext_id)
    if not resolution_candidates:
        return None

    extension = None
    bl = None
    resolved_extension_id = ""
    for candidate in resolution_candidates:
        extension, bl = resolve_extension_runtime_record(
            candidate,
            get_logger_only=_shared_logger,
            account_id=runtime_account_id,
        )
        if extension:
            resolved_extension_id = (
                str(
                    extension.get("extension_key") or extension.get("extension_id") or candidate
                ).strip()
                or candidate
            )
            break

    if not extension:
        return {
            "handled": True,
            "ok": False,
            "runtime": "extension_action",
            "extension_id": ext_id or install_id,
            "action_id": op_id,
            "invocation": {},
            "detail": "extension_unavailable",
            "errors": [
                {
                    "code": "extension_unavailable",
                    "message": "This extension is unavailable—reinstall it or replace this step.",
                }
            ],
        }

    install_id = install_id or _resolved_install_id(extension)
    availability_failure = extension_runtime_availability_failure(extension)
    if availability_failure is not None:
        return {
            "handled": True,
            "ok": False,
            "runtime": "extension_action",
            "extension_id": resolved_extension_id or ext_id or install_id,
            "action_id": op_id,
            "invocation": availability_failure,
            "detail": availability_failure["detail"],
            "errors": availability_failure["errors"],
        }

    identity_error = _external_effect_runtime_identity_error(
        extension=extension,
        operation_id=op_id,
        runtime_context=runtime_context,
    )
    if identity_error is not None:
        return {
            "handled": True,
            "ok": False,
            "runtime": "extension_action",
            "extension_id": resolved_extension_id or ext_id or install_id,
            "action_id": op_id,
            "detail": identity_error["code"],
            "errors": [identity_error],
        }

    if bl is not None:
        runtime_context["logger"] = bl
    if install_id:
        runtime_context["connector_install_id"] = install_id
    connection_id = _connection_id_from_binding(dict(connection_binding or {}))
    operation_contract = extension_operation_contract(extension, operation_id=op_id)
    from core.application.orchestration.retry_policy import (
        policy_from_state,
        request_semantics_for_operation_kind,
    )

    try:
        operation_deadline_epoch_seconds = float(
            state.get("_operation_deadline_epoch_seconds") or 0.0
        )
    except (TypeError, ValueError):
        operation_deadline_epoch_seconds = 0.0
    if operation_deadline_epoch_seconds <= 0:
        operation_deadline_epoch_seconds = (
            time.time() + policy_from_state(state).operation_deadline_seconds
        )

    def _operation_deadline_failure(
        external_effect: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        semantics = request_semantics_for_operation_kind(operation_contract.operation_kind)
        retry_facts = {
            "operation_kind": operation_contract.operation_kind,
            "request_semantics": semantics,
            "failure_class": "transient",
            "retryable": False,
            "definitely_no_external_effect": True,
            "external_effect_status": "failed",
            "operation_deadline_epoch_seconds": operation_deadline_epoch_seconds,
        }
        return {
            "handled": True,
            "ok": False,
            "runtime": "extension_action",
            "extension_id": resolved_extension_id or ext_id or install_id,
            "action_id": op_id,
            "invocation": {
                "status": "failed",
                "detail": "operation_deadline_exhausted",
                "response": dict(retry_facts),
            },
            "external_effect": dict(external_effect or {}),
            "detail": "operation_deadline_exhausted",
            **retry_facts,
        }

    if time.time() >= operation_deadline_epoch_seconds:
        return _operation_deadline_failure()
    required_connection_type_ids = operation_contract.connection_type_ids
    required_oauth_scopes = operation_contract.required_oauth_scopes
    if required_connection_type_ids:
        if not connection_id:
            return {
                "handled": True,
                "ok": False,
                "runtime": "extension_action",
                "extension_id": resolved_extension_id or ext_id or install_id,
                "action_id": op_id,
                "invocation": {
                    "status": "failed",
                    "response": {"definitely_no_external_effect": True},
                },
                "detail": "connection_resolution_failed:explicit connection binding is required",
            }
        input_payload = {**dict(input_payload or {}), "connection_ref": connection_id}
    elif connection_id and _operation_declares_connection_ref(extension, op_id):
        input_payload = {**dict(input_payload or {}), "connection_ref": connection_id}
    suppressed_test_mode = _is_validation_only_test_mode(
        extension=extension,
        action_id=op_id,
        runtime_context=runtime_context,
    )
    if suppressed_test_mode:
        connection_failure = _test_mode_connection_failure(
            bl,
            connection_id=connection_id,
            runtime_context=runtime_context,
            extension_id=resolved_extension_id or ext_id,
            connector_install_id=install_id,
            required_connection_type_ids=required_connection_type_ids,
            required_oauth_scopes=required_oauth_scopes,
        )
        if connection_failure is not None:
            return {
                "handled": True,
                "ok": False,
                "runtime": "extension_action",
                "extension_id": resolved_extension_id or ext_id or install_id,
                "action_id": op_id,
                "invocation": {
                    "status": "failed",
                    "response": {"definitely_no_external_effect": True},
                },
                "detail": connection_failure["message"],
                "errors": [connection_failure],
            }
        runtime_context[_HOST_TEST_MODE_SUPPRESSION_FLAG] = True
        suppressed_result = {
            "test_mode_status": "suppressed",
            "external_effect_status": "suppressed",
            "definitely_no_external_effect": True,
        }
        invocation = {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": dict(suppressed_result),
                "external_effect_status": "suppressed",
                "definitely_no_external_effect": True,
            },
        }
        log_host_extension_invocation(
            bl=bl,
            extension=extension,
            extension_id=resolved_extension_id or ext_id or install_id,
            invocation_type=f"action:{op_id}",
            request={
                "action_id": op_id,
                "input_payload": dict(event_input_payload or {}),
                "target": (
                    {"connection_binding": dict(connection_binding or {})}
                    if connection_binding
                    else {}
                ),
                "connector_install_id": install_id or None,
                "connection_binding": dict(connection_binding or {}),
            },
            invocation=invocation,
            runtime_context=runtime_context,
            request_context={
                "source": "connector_step_runtime",
                "connector_install_id": install_id or None,
                "external_effect_id": None,
                **(
                    {"dataset_page": dataset_page_context}
                    if dataset_page_context is not None
                    else {}
                ),
            },
            action_id=op_id,
            external_effect_id=None,
        )
        return {
            "handled": True,
            "ok": True,
            "runtime": "extension_action",
            "extension_id": resolved_extension_id or ext_id or install_id,
            "action_id": op_id,
            "operation_kind": operation_contract.operation_kind,
            "invocation": invocation,
            "external_effect": {},
            "produced_artifacts": [],
            "produced_datasets": [],
            "grant_outputs": {},
            "detail": "test_mode_suppressed",
        }
    if suppression_probe_only:
        return None
    try:
        runtime_connection, logged_connection = _build_runtime_connection_target(
            bl,
            connection_binding=dict(connection_binding or {}),
            runtime_context=runtime_context,
            extension_id=resolved_extension_id or ext_id,
            connector_install_id=install_id,
            required_connection_type_ids=required_connection_type_ids,
            required_oauth_scopes=required_oauth_scopes,
        )
    except Exception as exc:
        return {
            "handled": True,
            "ok": False,
            "runtime": "extension_action",
            "extension_id": resolved_extension_id or ext_id or install_id,
            "action_id": op_id,
            "invocation": {},
            "detail": f"connection_resolution_failed:{exc}",
        }
    target = {"connection_binding": dict(connection_binding or {})} if connection_binding else {}
    logged_target = (
        {"connection_binding": dict(connection_binding or {})} if connection_binding else {}
    )
    if runtime_connection:
        target["connection"] = runtime_connection
        logged_target["connection"] = logged_connection
    hydrated_secrets, secret_budget_exceeded = collect_secret_values(
        [
            runtime_connection.get("secrets") if runtime_connection else None,
            list(protected_input_secrets),
        ]
    )
    if secret_budget_exceeded or protected_input_secret_budget_exceeded:
        invocation = secret_exposure_invocation()
        return {
            "handled": True,
            "ok": False,
            "runtime": "extension_action",
            "extension_id": resolved_extension_id or ext_id or install_id,
            "action_id": op_id,
            "invocation": invocation,
            "external_effect": {},
            "produced_artifacts": [],
            "produced_datasets": [],
            "grant_outputs": {},
            "detail": invocation["detail"],
        }
    project_state = (
        {}
        if suppressed_test_mode
        else _load_extension_project_state_for_target(
            bl,
            account_id=str(runtime_context.get("account_id") or "").strip(),
            project_id=str(runtime_context.get("project_id") or "").strip(),
            extension_id=resolved_extension_id or ext_id or install_id,
            extension_install_id=install_id,
            connection_id=str((runtime_connection or {}).get("connection_id") or "").strip(),
            extension=extension,
            input_payload=dict(input_payload or {}),
        )
    )
    if project_state:
        target["project_state"] = project_state
        logged_target["project_state"] = {
            "item_count": len(project_state.get("items") or []),
        }
    grant_plane = None
    prepared_grants = None
    early_finalized_outputs: dict[str, dict[str, Any]] = {}
    try:
        external_effect_claim = (
            {}
            if suppressed_test_mode
            else _claim_external_effect_for_action(
                bl,
                extension=extension,
                extension_id=resolved_extension_id or ext_id or install_id,
                extension_install_id=install_id,
                connection_id=str((runtime_connection or {}).get("connection_id") or "").strip(),
                action_id=op_id,
                input_payload=dict(input_payload or {}),
                runtime_context=runtime_context,
            )
        )
    except Exception:
        if prepared_grants is not None:
            _cleanup_prepared_output_grants(grant_plane, prepared_grants)
        raise
    runtime_context.pop("external_effect_resolution_ref", None)
    external_effect = _to_object(external_effect_claim.get("effect"))
    external_effect_id = str(external_effect.get("effect_id") or "").strip()
    external_effect_claim_token = str(external_effect.get("claim_token") or "").strip()
    if external_effect_claim and not external_effect_claim.get("should_spawn"):
        _cleanup_prepared_output_grants(grant_plane, prepared_grants)
        from core.application.extensions.external_effect_replay import (
            hydrate_external_effect_claim,
        )

        external_effect_claim = hydrate_external_effect_claim(bl, external_effect_claim)
        if external_effect_claim.get("error"):
            if external_effect_claim.get("rate_limited"):
                response_summary = _to_object(external_effect.get("response_summary_jsonb"))
                try:
                    retry_after_seconds = max(
                        1.0,
                        float(
                            external_effect_claim.get("retry_after_seconds")
                            or response_summary.get("retry_after_seconds")
                            or 1
                        ),
                    )
                except (TypeError, ValueError):
                    retry_after_seconds = 1.0
                retry_facts = {
                    "operation_kind": operation_contract.operation_kind,
                    "request_semantics": request_semantics_for_operation_kind(
                        operation_contract.operation_kind
                    ),
                    "failure_class": "transient",
                    "retryable": True,
                    "retry_after_seconds": retry_after_seconds,
                    "definitely_no_external_effect": True,
                    "external_effect_status": "failed",
                    "provider_error_code": "rate_limited",
                    "operation_deadline_epoch_seconds": operation_deadline_epoch_seconds,
                }
                return {
                    "handled": True,
                    "ok": False,
                    "runtime": "extension_action",
                    "extension_id": resolved_extension_id or ext_id or install_id,
                    "action_id": op_id,
                    "operation_kind": operation_contract.operation_kind,
                    "invocation": {
                        "status": "failed",
                        "detail": "external_effect_rate_limited",
                        "response": dict(retry_facts),
                    },
                    "external_effect": external_effect,
                    "produced_artifacts": [],
                    "produced_datasets": [],
                    "grant_outputs": {},
                    "detail": "external_effect_rate_limited",
                    **retry_facts,
                }
            return {
                "handled": True,
                "ok": False,
                "runtime": "extension_action",
                "extension_id": resolved_extension_id or ext_id or install_id,
                "action_id": op_id,
                "external_effect": external_effect,
                "produced_artifacts": [],
                "produced_datasets": [],
                "grant_outputs": {},
                "detail": str(external_effect_claim.get("error") or ""),
            }
        effect_status = str(external_effect.get("status") or "").strip().lower()
        replay_invocation = _to_object(external_effect_claim.get("replay_invocation"))
        produced_artifacts = list(replay_invocation.get("produced_artifacts") or [])
        produced_datasets = list(replay_invocation.get("produced_datasets") or [])
        grant_outputs = _to_object(replay_invocation.get("grant_outputs"))
        output_requests = _grant_requests_from_policy(
            _artifact_policy_for_operation(extension, policy_operation_id),
            input_payload=dict(input_payload or {}),
            project_state=project_state,
        )
        outputs_expected = bool(
            output_requests.get("artifact_outputs")
            or output_requests.get("artifact_output_specs")
            or output_requests.get("dataset_outputs")
            or output_requests.get("dataset_output_specs")
        )
        outputs_available = bool(produced_artifacts or produced_datasets or grant_outputs)
        if (
            effect_status in {"succeeded", "suppressed"}
            and outputs_expected
            and not outputs_available
        ):
            return {
                "handled": True,
                "ok": False,
                "runtime": "extension_action",
                "extension_id": resolved_extension_id or ext_id or install_id,
                "action_id": op_id,
                "external_effect": external_effect,
                "produced_artifacts": [],
                "produced_datasets": [],
                "grant_outputs": {},
                "detail": "external_effect_deduped_outputs_unavailable",
                "errors": [
                    {
                        "code": "external_effect_deduped_outputs_unavailable",
                        "message": (
                            "External effect replay cannot recover output grant handles "
                            "from the ledger"
                        ),
                    }
                ],
            }
        ok = effect_status in {"succeeded", "suppressed"}
        visible_produced_artifacts = produced_artifacts if ok else []
        visible_produced_datasets = produced_datasets if ok else []
        visible_grant_outputs = grant_outputs if ok else {}
        result = {
            "handled": True,
            "ok": ok,
            "runtime": "extension_action",
            "extension_id": resolved_extension_id or ext_id or install_id,
            "action_id": op_id,
            "invocation": replay_invocation,
            "external_effect": external_effect,
            "produced_artifacts": visible_produced_artifacts,
            "produced_datasets": visible_produced_datasets,
            "grant_outputs": visible_grant_outputs,
            "detail": f"external_effect_{effect_status or 'deduped'}",
        }
        if visible_grant_outputs:
            result.update(visible_grant_outputs)
        if len(visible_produced_artifacts) == 1:
            artifact_id = str(visible_produced_artifacts[0].get("artifact_id") or "").strip()
            result["artifact_handle"] = f"artifact:{artifact_id}" if artifact_id else ""
        if len(visible_produced_datasets) == 1:
            dataset_id = str(visible_produced_datasets[0].get("dataset_id") or "").strip()
            result["dataset_handle"] = f"dataset:{dataset_id}" if dataset_id else ""
        return result
    try:

        def register_marker(grants: Any) -> None:
            if not external_effect_id:
                return
            from core.application.extensions.external_effects import (
                register_external_effect_output_reconciliation,
            )

            register_external_effect_output_reconciliation(
                bl,
                effect_id=external_effect_id,
                account_id=str(runtime_context.get("account_id") or "").strip(),
                claim_token=external_effect_claim_token,
                prepared_grants=grants,
            )

        grant_plane, prepared_grants = _prepare_grants_for_extension_action(
            extension=extension,
            action_id=policy_operation_id,
            input_payload=dict(input_payload or {}),
            project_state=project_state,
            runtime_context=runtime_context,
            runtime_scope=runtime_scope,
            state=state,
            before_materialize_outputs=register_marker,
            include_output_grants=not suppressed_test_mode,
        )
    except Exception as exc:
        _cleanup_prepared_output_grants(grant_plane, prepared_grants)
        from core.application.extensions.external_effects import (
            fail_external_effect_before_spawn,
        )

        with contextlib.suppress(Exception):
            fail_external_effect_before_spawn(
                bl,
                effect_id=external_effect_id,
                claim_token=external_effect_claim_token,
                code="external_effect_pre_spawn_setup_failed",
            )
        return {
            "handled": True,
            "ok": False,
            "runtime": "extension_action",
            "extension_id": resolved_extension_id or ext_id or install_id,
            "action_id": op_id,
            "external_effect": external_effect,
            "detail": (
                "external_effect_pre_spawn_setup_failed"
                if external_effect_id
                else f"grant_preparation_failed:{exc}"
            ),
        }
    if external_effect_id:
        runtime_context["external_effect_id"] = external_effect_id
        try:
            from core.application.extensions.external_effects import mark_external_effect_running

            running_effect = mark_external_effect_running(
                bl,
                effect_id=external_effect_id,
                claim_token=external_effect_claim_token,
            )
            if not running_effect:
                _cleanup_prepared_output_grants(grant_plane, prepared_grants)
                from core.application.extensions.external_effects import (
                    fail_external_effect_before_spawn,
                )

                with contextlib.suppress(Exception):
                    fail_external_effect_before_spawn(
                        bl,
                        effect_id=external_effect_id,
                        claim_token=external_effect_claim_token,
                        code="external_effect_pre_spawn_setup_failed",
                    )
                return {
                    "handled": True,
                    "ok": False,
                    "runtime": "extension_action",
                    "extension_id": resolved_extension_id or ext_id or install_id,
                    "action_id": op_id,
                    "external_effect": external_effect,
                    "detail": "external_effect_lease_reclaimed",
                }
        except Exception:
            _cleanup_prepared_output_grants(grant_plane, prepared_grants)
            from core.application.extensions.external_effects import (
                fail_external_effect_before_spawn,
            )

            with contextlib.suppress(Exception):
                fail_external_effect_before_spawn(
                    bl,
                    effect_id=external_effect_id,
                    claim_token=external_effect_claim_token,
                    code="external_effect_pre_spawn_setup_failed",
                )
            return {
                "handled": True,
                "ok": False,
                "runtime": "extension_action",
                "extension_id": resolved_extension_id or ext_id or install_id,
                "action_id": op_id,
                "external_effect": external_effect,
                "detail": "external_effect_lease_unavailable",
            }
    try:
        remaining_timeout_seconds = operation_deadline_epoch_seconds - time.time()
        if remaining_timeout_seconds <= 0:
            _cleanup_prepared_output_grants(grant_plane, prepared_grants)
            if external_effect_id:
                from core.application.extensions.external_effects import (
                    fail_external_effect_before_spawn,
                )

                with contextlib.suppress(Exception):
                    fail_external_effect_before_spawn(
                        bl,
                        effect_id=external_effect_id,
                        claim_token=external_effect_claim_token,
                        code="operation_deadline_exhausted",
                    )
            return _operation_deadline_failure(external_effect)
        _emit_connector_tool_event(
            bl,
            job_id=runtime_context.get("job_id"),
            group_id=str(runtime_context.get("group_id") or "").strip(),
            step_id=str(runtime_context.get("step_id") or "").strip(),
            tool_id=f"{resolved_extension_id or ext_id or install_id}.{op_id}",
            function_name=op_id,
            event_type="tool_call",
            excerpt="",
            include_excerpt=False,
        )
        progress_logger = _job_event_logger(bl)
        progress_step_id = str(runtime_context.get("step_id") or "").strip()
        with extension_progress_sink(
            _step_update_sink(
                progress=step_progress_reporter(
                    progress_logger,
                    job_id=runtime_context.get("job_id"),
                    step_id=progress_step_id,
                    label=progress_step_id or op_id,
                ),
                outputs=early_output_handler(
                    grant_plane=grant_plane,
                    prepared_grants=prepared_grants,
                    finalized=early_finalized_outputs,
                    event_logger=progress_logger,
                    job_id=runtime_context.get("job_id"),
                    step_id=progress_step_id,
                ),
            )
        ):
            invocation = run_extension_action(
                extension,
                action_id=op_id,
                input_payload=dict(input_payload or {}),
                target=target,
                timeout_seconds=remaining_timeout_seconds,
                runtime_context=runtime_context,
                scope=runtime_scope,
                artifact_inputs=(prepared_grants.artifact_inputs if prepared_grants else None),
                artifact_outputs=(prepared_grants.artifact_outputs if prepared_grants else None),
                dataset_inputs=(prepared_grants.dataset_inputs if prepared_grants else None),
                dataset_outputs=(prepared_grants.dataset_outputs if prepared_grants else None),
            )
    except Exception:
        if not external_effect_id:
            raise
        from core.application.extensions.external_effects import (
            external_effect_spawn_exception_invocation,
        )

        invocation = external_effect_spawn_exception_invocation()
    secret_exposure = value_exposes_secret(invocation, hydrated_secrets)
    if secret_exposure:
        invocation = secret_exposure_invocation()
    _emit_connector_tool_event(
        bl,
        job_id=runtime_context.get("job_id"),
        group_id=str(runtime_context.get("group_id") or "").strip(),
        step_id=str(runtime_context.get("step_id") or "").strip(),
        tool_id=f"{resolved_extension_id or ext_id or install_id}.{op_id}",
        function_name=op_id,
        event_type="tool_result",
        excerpt="",
        is_error=invocation.get("status") != "succeeded",
        include_excerpt=False,
    )
    produced_artifacts: list[dict[str, Any]] = []
    produced_datasets: list[dict[str, Any]] = []
    grant_outputs: dict[str, Any] = {}
    if (
        grant_plane is not None
        and prepared_grants is not None
        and (prepared_grants.artifact_output_ids or prepared_grants.dataset_output_ids)
        and invocation.get("status") == "succeeded"
    ):
        from core.application.extensions import runtime_grants

        try:
            finalized_grants = runtime_grants.finalize_prepared_extension_grants(
                grant_plane,
                prepared_grants,
                already_finalized=early_finalized_outputs,
            )
            if finalized_grants.errors:
                invocation = {
                    **invocation,
                    "status": "failed",
                    "detail": str(finalized_grants.errors[0].get("code") or "grant_upload_failed"),
                    "errors": finalized_grants.errors,
                }
            else:
                produced_artifacts = finalized_grants.produced_artifacts
                produced_datasets = finalized_grants.produced_datasets
                for descriptor in prepared_grants.artifact_outputs:
                    binding_key = str(descriptor.get("binding_key") or "").strip()
                    handle = str(descriptor.get("artifact_handle") or "").strip()
                    if binding_key and handle:
                        grant_outputs[binding_key] = handle
                for descriptor in prepared_grants.dataset_outputs:
                    binding_key = str(descriptor.get("binding_key") or "").strip()
                    handle = str(descriptor.get("dataset_handle") or "").strip()
                    if binding_key and handle:
                        grant_outputs[binding_key] = handle
                invocation["produced_artifacts"] = produced_artifacts
                invocation["produced_datasets"] = produced_datasets
                invocation["grant_outputs"] = grant_outputs
        except Exception:
            from core.application.extensions.external_effects import (
                external_effect_output_finalization_exception_invocation,
            )

            invocation = external_effect_output_finalization_exception_invocation()
    if invocation.get("status") == "succeeded" and not suppressed_test_mode and not secret_exposure:
        try:
            from core.application.extensions.response_envelope import process_project_state_updates

            process_project_state_updates(
                invocation,
                extension=extension,
                extension_id=resolved_extension_id or ext_id or install_id,
                db=bl,
                account_id=str(runtime_context.get("account_id") or "").strip(),
                project_id=str(runtime_context.get("project_id") or "").strip(),
                connection_id=str((runtime_connection or {}).get("connection_id") or "").strip()
                or None,
                actor_id=str(runtime_context.get("actor_id") or "").strip() or None,
            )
        except Exception as exc:
            logger.warning("failed to process extension project-state updates: %s", exc)
            invocation = {
                **invocation,
                "status": "failed",
                "detail": "project_state_update_failed",
                "errors": [
                    *list(invocation.get("errors") or []),
                    {"code": "project_state_update_failed", "message": str(exc)},
                ],
            }
    if not secret_exposure:
        try:
            _persist_connection_session_update(
                bl,
                runtime_connection=runtime_connection,
                connection_binding=dict(connection_binding or {}),
                invocation=invocation,
                fail_closed=bool(external_effect_id),
            )
            _strip_connection_session_updates(invocation)
        except Exception:
            from core.application.extensions.external_effects import (
                external_effect_session_persistence_exception_invocation,
            )

            invocation = external_effect_session_persistence_exception_invocation()
    if str(invocation.get("status") or "").strip().lower() != "succeeded":
        _cleanup_prepared_output_grants(grant_plane, prepared_grants)
        produced_artifacts = []
        produced_datasets = []
        grant_outputs = {}
        invocation.pop("produced_artifacts", None)
        invocation.pop("produced_datasets", None)
        invocation.pop("grant_outputs", None)
    if external_effect_id:
        try:
            from core.application.extensions.external_effects import (
                apply_external_effect_confirmation,
                confirm_external_effect_from_invocation,
                public_confirmed_external_effect,
            )

            confirmed_effect = confirm_external_effect_from_invocation(
                bl,
                effect_id=external_effect_id,
                invocation=invocation,
                claim_token=external_effect_claim_token,
                account_id=str(runtime_context.get("account_id") or "").strip(),
                extension=extension,
                operation_id=op_id,
                protected_values=hydrated_secrets,
                host_outputs={
                    "produced_artifacts": produced_artifacts,
                    "produced_datasets": produced_datasets,
                    "grant_outputs": grant_outputs,
                },
                expected_project_id=str(runtime_context.get("project_id") or "").strip() or None,
                expected_connection_id=str(
                    (runtime_connection or {}).get("connection_id") or ""
                ).strip()
                or None,
            )
            if confirmed_effect:
                invocation = apply_external_effect_confirmation(invocation, confirmed_effect)
                external_effect = public_confirmed_external_effect(confirmed_effect)
                if invocation.get("status") != "succeeded":
                    produced_artifacts = []
                    produced_datasets = []
                    grant_outputs = {}
        except Exception as exc:
            logger.warning("failed to confirm external effect: %s", exc)
            if getattr(exc, "commit_outcome", "unknown") == "rolled_back":
                _cleanup_prepared_output_grants(grant_plane, prepared_grants)
            from core.application.extensions.external_effects import (
                external_effect_confirmation_failure_invocation,
            )

            invocation = external_effect_confirmation_failure_invocation(exc)
            produced_artifacts = []
            produced_datasets = []
            grant_outputs = {}
    log_host_extension_invocation(
        bl=bl,
        extension=extension,
        extension_id=resolved_extension_id or ext_id or install_id,
        invocation_type=f"action:{op_id}",
        request={
            "action_id": op_id,
            "input_payload": dict(event_input_payload or {}),
            "target": logged_target,
            "connector_install_id": install_id or None,
            "connection_binding": dict(connection_binding or {}),
        },
        invocation=invocation,
        runtime_context=runtime_context,
        request_context={
            "source": "connector_step_runtime",
            "connector_install_id": install_id or None,
            "external_effect_id": external_effect_id or None,
            **({"dataset_page": dataset_page_context} if dataset_page_context is not None else {}),
        },
        action_id=op_id,
        external_effect_id=external_effect_id or None,
    )
    ok = str(invocation.get("status") or "").strip().lower() == "succeeded"
    visible_produced_artifacts = produced_artifacts if ok else []
    visible_produced_datasets = produced_datasets if ok else []
    visible_grant_outputs = grant_outputs if ok else {}
    result = {
        "handled": True,
        "ok": ok,
        "runtime": "extension_action",
        "extension_id": resolved_extension_id or ext_id or install_id,
        "action_id": op_id,
        # Operation semantics come from the installed immutable manifest, not
        # from an extension-controlled response envelope.
        "operation_kind": operation_contract.operation_kind,
        "invocation": invocation,
        "external_effect": external_effect,
        "produced_artifacts": visible_produced_artifacts,
        "produced_datasets": visible_produced_datasets,
        "grant_outputs": visible_grant_outputs,
        "detail": ""
        if ok
        else str(invocation.get("detail") or "Connector extension action failed"),
    }
    return _project_extension_output_aliases(
        result,
        visible_grant_outputs=visible_grant_outputs,
        visible_produced_artifacts=visible_produced_artifacts,
        visible_produced_datasets=visible_produced_datasets,
    )
