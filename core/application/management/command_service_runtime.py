"""Reusable factories for transport-independent command service wiring."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.extensions.binding_service import ExtensionBindingService
from core.application.management.command_services import (
    ApprovalCommandService,
    CommandDispatchService,
    EventCommandService,
    ExternalAgentCommandService,
    JobCommandService,
    ModelProviderCommandService,
    ProjectCommandService,
    ToolExecutionService,
    TrashLifecycleService,
)


def create_project_dataset_catalog_store(dsn: str):
    """Create the application-owned dataset catalog used by lifecycle planning."""
    from core.application.registry.db_store import RegistryDBStore

    return RegistryDBStore(dsn)


def attachment_policy_from_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "attachment_max_size_bytes": int(config.get("attachment_max_size_bytes") or 0),
        "attachment_max_files": int(config.get("attachment_max_files") or 0),
        "allowed_attachment_mimes": set(config.get("allowed_attachment_mimes") or set()),
        "allowed_attachment_extensions": set(config.get("allowed_attachment_extensions") or set()),
    }


def public_workflow_policy_from_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "idempotency_result_retention_days": int(
            config.get("idempotency_result_retention_days") or 30
        )
    }


def create_project_command_service(
    *,
    config: dict[str, Any],
    get_logger,
    resolve_workflow_selection,
    validate_explicit_workflow_override,
    sanitize_external_input,
    enqueue_task,
    normalize_cron_expr,
    cron_next_run_at,
    form_flag,
    run_scheduled_job_once,
    parse_agent_model_overrides,
    build_project_policy_from_form,
    list_workflow_choices,
    workflow_availability_assert=None,
    write_project_profile_to_kb,
    to_json_obj,
    artifact_plane=None,
    parse_test_mode_fn=None,
    enqueue_task_in_queue=None,
) -> ProjectCommandService:
    if parse_test_mode_fn is None:
        from core.infrastructure.runtime.test_mode import parse_test_mode as parse_test_mode_fn

    return ProjectCommandService(
        get_logger=get_logger,
        resolve_workflow_selection=resolve_workflow_selection,
        validate_explicit_workflow_override=validate_explicit_workflow_override,
        sanitize_external_input=sanitize_external_input,
        enqueue_task=enqueue_task,
        enqueue_task_in_queue=enqueue_task_in_queue,
        parse_test_mode=parse_test_mode_fn,
        normalize_cron_expr=normalize_cron_expr,
        cron_next_run_at=cron_next_run_at,
        form_flag=form_flag,
        run_scheduled_job_once=run_scheduled_job_once,
        parse_agent_model_overrides=parse_agent_model_overrides,
        build_project_policy_from_form=build_project_policy_from_form,
        list_workflow_choices=list_workflow_choices,
        workflow_availability_assert=workflow_availability_assert,
        write_project_profile_to_kb=write_project_profile_to_kb,
        to_json_obj=to_json_obj,
        artifact_plane=artifact_plane,
        **public_workflow_policy_from_config(config),
        **attachment_policy_from_config(config),
    )


def create_project_command_service_from_runtime(
    *,
    config: dict[str, Any],
    get_logger,
    resolve_workflow_selection,
    validate_explicit_workflow_override,
    sanitize_external_input,
    enqueue_task,
    run_scheduled_job_once,
    list_workflow_choices,
    workflow_availability_assert=None,
    write_project_profile_to_kb,
    to_json_obj,
    workflow_helpers_module,
    artifact_plane=None,
    enqueue_task_in_queue=None,
) -> ProjectCommandService:
    return create_project_command_service(
        config=config,
        get_logger=get_logger,
        resolve_workflow_selection=resolve_workflow_selection,
        validate_explicit_workflow_override=validate_explicit_workflow_override,
        sanitize_external_input=sanitize_external_input,
        enqueue_task=enqueue_task,
        enqueue_task_in_queue=enqueue_task_in_queue,
        normalize_cron_expr=workflow_helpers_module.normalize_cron_expr,
        cron_next_run_at=workflow_helpers_module.cron_next_run_at,
        form_flag=workflow_helpers_module.form_flag,
        run_scheduled_job_once=run_scheduled_job_once,
        parse_agent_model_overrides=lambda form_data: (
            workflow_helpers_module.parse_agent_model_overrides(
                form_data,
                workflow_helpers_module.agent_model_override_targets("gpt-4.1-nano"),
            )
        ),
        build_project_policy_from_form=workflow_helpers_module.build_project_policy_from_form,
        list_workflow_choices=list_workflow_choices,
        workflow_availability_assert=workflow_availability_assert,
        write_project_profile_to_kb=write_project_profile_to_kb,
        to_json_obj=to_json_obj,
        artifact_plane=artifact_plane,
    )


def build_project_command_service_factory(**kwargs) -> Callable[[], ProjectCommandService]:
    def _factory() -> ProjectCommandService:
        return create_project_command_service_from_runtime(**kwargs)

    return _factory


def create_job_command_service(
    *,
    get_logger,
    build_rerun_workflow_selection,
    load_replay_snapshot=None,
    enqueue_task,
    get_thread_state_values,
    runtime_step_scope_id,
    latest_runtime_message_id,
    override_workflow_id_from_value,
    is_empty_original_value,
    derive_override_original_value,
    validate_explicit_workflow_override,
    to_json_obj,
    serialize_decision_override_row,
    serialize_job_step_replay_row,
    sanitize_retryable_interrupted_step_snapshot,
    resolve_job_project_name,
    append_runtime_journal,
    enqueue_task_in_queue,
    resolve_replay_message_row,
    collect_replay_step_trace,
    extract_replay_prompt_capture,
    derive_original_replay_outcome,
    invoke_step_reexecute_runtime,
    invoke_step_replay_llm,
    build_replay_diff_excerpt,
    retryable_interrupted_step_status,
    get_project_run_config=None,
) -> JobCommandService:
    return JobCommandService(
        get_logger=get_logger,
        build_rerun_workflow_selection=build_rerun_workflow_selection,
        load_replay_snapshot=load_replay_snapshot,
        enqueue_task=enqueue_task,
        get_thread_state_values=get_thread_state_values,
        runtime_step_scope_id=runtime_step_scope_id,
        latest_runtime_message_id=latest_runtime_message_id,
        override_workflow_id_from_value=override_workflow_id_from_value,
        is_empty_original_value=is_empty_original_value,
        derive_override_original_value=derive_override_original_value,
        validate_explicit_workflow_override=validate_explicit_workflow_override,
        to_json_obj=to_json_obj,
        serialize_decision_override_row=serialize_decision_override_row,
        serialize_job_step_replay_row=serialize_job_step_replay_row,
        sanitize_retryable_interrupted_step_snapshot=sanitize_retryable_interrupted_step_snapshot,
        resolve_job_project_name=resolve_job_project_name,
        append_runtime_journal=append_runtime_journal,
        enqueue_task_in_queue=enqueue_task_in_queue,
        resolve_replay_message_row=resolve_replay_message_row,
        collect_replay_step_trace=collect_replay_step_trace,
        extract_replay_prompt_capture=extract_replay_prompt_capture,
        derive_original_replay_outcome=derive_original_replay_outcome,
        invoke_step_reexecute_runtime=invoke_step_reexecute_runtime,
        invoke_step_replay_llm=invoke_step_replay_llm,
        build_replay_diff_excerpt=build_replay_diff_excerpt,
        retryable_interrupted_step_status=retryable_interrupted_step_status,
        get_project_run_config=get_project_run_config,
    )


def create_job_command_service_from_runtime(
    *,
    get_logger,
    build_rerun_workflow_selection,
    load_replay_snapshot=None,
    enqueue_task,
    get_thread_state_values,
    to_json_obj,
    resolve_job_project_name,
    append_runtime_journal,
    enqueue_task_in_queue,
    collect_replay_step_trace,
    invoke_step_reexecute_runtime,
    workflow_override_runtime_module,
    interrupted_step_runtime_module,
    query_service_runtime_module,
    step_replay_runtime_module,
    snapshot_serializers_module,
    retryable_interrupted_step_status,
    derive_override_original_value=None,
    validate_explicit_workflow_override=None,
    invoke_step_replay_llm=None,
    get_project_run_config=None,
) -> JobCommandService:
    return create_job_command_service(
        get_logger=get_logger,
        build_rerun_workflow_selection=build_rerun_workflow_selection,
        load_replay_snapshot=load_replay_snapshot,
        enqueue_task=enqueue_task,
        get_thread_state_values=get_thread_state_values,
        runtime_step_scope_id=workflow_override_runtime_module.runtime_step_scope_id,
        latest_runtime_message_id=workflow_override_runtime_module.latest_runtime_message_id,
        override_workflow_id_from_value=workflow_override_runtime_module.override_workflow_id_from_value,
        is_empty_original_value=workflow_override_runtime_module.is_empty_original_value,
        derive_override_original_value=(
            derive_override_original_value
            or workflow_override_runtime_module.derive_override_original_value
        ),
        validate_explicit_workflow_override=(
            validate_explicit_workflow_override
            or workflow_override_runtime_module.validate_explicit_workflow_override
        ),
        to_json_obj=to_json_obj,
        serialize_decision_override_row=snapshot_serializers_module.serialize_decision_override_row,
        serialize_job_step_replay_row=snapshot_serializers_module.serialize_job_step_replay_row,
        sanitize_retryable_interrupted_step_snapshot=interrupted_step_runtime_module.sanitize_interrupted_step_snapshot,
        resolve_job_project_name=resolve_job_project_name,
        append_runtime_journal=append_runtime_journal,
        enqueue_task_in_queue=enqueue_task_in_queue,
        resolve_replay_message_row=query_service_runtime_module.resolve_replay_message_row,
        collect_replay_step_trace=collect_replay_step_trace,
        extract_replay_prompt_capture=step_replay_runtime_module.extract_replay_prompt_capture,
        derive_original_replay_outcome=step_replay_runtime_module.derive_original_replay_outcome,
        invoke_step_reexecute_runtime=invoke_step_reexecute_runtime,
        invoke_step_replay_llm=(
            invoke_step_replay_llm or step_replay_runtime_module.invoke_step_replay_llm
        ),
        build_replay_diff_excerpt=step_replay_runtime_module.build_replay_diff_excerpt,
        retryable_interrupted_step_status=retryable_interrupted_step_status,
        get_project_run_config=get_project_run_config,
    )


def build_job_command_service_factory(**kwargs) -> Callable[[], JobCommandService]:
    def _factory() -> JobCommandService:
        return create_job_command_service_from_runtime(**kwargs)

    return _factory


def create_approval_command_service(
    *,
    resume_shutdown_in_progress,
    get_checkpointer,
    get_logger,
    enqueue_workflow_resume,
    workflow_resume_repository_factory,
    get_thread_state_snapshot=None,
) -> ApprovalCommandService:
    return ApprovalCommandService(
        resume_shutdown_in_progress=resume_shutdown_in_progress,
        get_checkpointer=get_checkpointer,
        get_logger=get_logger,
        enqueue_workflow_resume=enqueue_workflow_resume,
        workflow_resume_repository_factory=workflow_resume_repository_factory,
        get_thread_state_snapshot=get_thread_state_snapshot,
    )


def build_approval_command_service_factory(**kwargs) -> Callable[[], ApprovalCommandService]:
    def _factory() -> ApprovalCommandService:
        return create_approval_command_service(**kwargs)

    return _factory


def create_event_command_service(
    *,
    config: dict[str, Any],
    get_logger,
    get_job_store=None,
    get_project_run_config=None,
    resolve_domain_event_by_ref,
    to_json_obj,
    serialize_domain_event_datetime,
    sanitize_external_input,
    enqueue_task,
    resolve_workflow_selection=None,
    workflow_availability_assert=None,
    workflow_exists,
    evaluate_workflow_reactions,
    reaction_action_result_executed,
    dispatch_project_notification_event,
    fetch_provider_attachments,
    artifact_plane=None,
) -> EventCommandService:
    return EventCommandService(
        get_logger=get_logger,
        get_job_store=get_job_store,
        get_project_run_config=get_project_run_config,
        resolve_domain_event_by_ref=resolve_domain_event_by_ref,
        to_json_obj=to_json_obj,
        serialize_domain_event_datetime=serialize_domain_event_datetime,
        sanitize_external_input=sanitize_external_input,
        enqueue_task=enqueue_task,
        resolve_workflow_selection=resolve_workflow_selection,
        workflow_availability_assert=workflow_availability_assert,
        workflow_exists=workflow_exists,
        evaluate_workflow_reactions=evaluate_workflow_reactions,
        reaction_action_result_executed=reaction_action_result_executed,
        dispatch_project_notification_event=dispatch_project_notification_event,
        fetch_provider_attachments=fetch_provider_attachments,
        artifact_plane=artifact_plane,
        **attachment_policy_from_config(config),
    )


def build_event_command_service_factory(**kwargs) -> Callable[[], EventCommandService]:
    def _factory() -> EventCommandService:
        return create_event_command_service(**kwargs)

    return _factory


def create_external_agent_command_service(*, get_logger) -> ExternalAgentCommandService:
    return ExternalAgentCommandService(get_logger=get_logger)


def build_external_agent_command_service_factory(
    **kwargs,
) -> Callable[[], ExternalAgentCommandService]:
    def _factory() -> ExternalAgentCommandService:
        return create_external_agent_command_service(**kwargs)

    return _factory


def create_extension_binding_service(
    *,
    get_logger,
    resolve_extension_runtime_record,
    extension_identifier,
    extension_operation_handler_mode,
    extension_operation_failure_status,
    get_project_by_id=None,
    event_command_service_factory=None,
    command_dispatch_service_factory=CommandDispatchService,
    get_extension_install_store=None,
    get_extension_runtime_store=None,
    extension_query_service_factory=None,
    get_operation_store=None,
    get_artifact_plane=None,
) -> ExtensionBindingService:
    return ExtensionBindingService(
        get_logger=get_logger,
        get_operation_store=get_operation_store or get_logger,
        get_project_by_id=get_project_by_id,
        resolve_extension_runtime_record=resolve_extension_runtime_record,
        extension_identifier=extension_identifier,
        extension_operation_handler_mode=extension_operation_handler_mode,
        extension_operation_failure_status=extension_operation_failure_status,
        event_command_service_factory=event_command_service_factory,
        command_dispatch_service_factory=command_dispatch_service_factory,
        get_extension_install_store=get_extension_install_store,
        get_extension_runtime_store=get_extension_runtime_store,
        extension_query_service_factory=extension_query_service_factory,
        get_artifact_plane=get_artifact_plane,
    )


def build_extension_binding_service_factory(**kwargs) -> Callable[[], ExtensionBindingService]:
    def _factory() -> ExtensionBindingService:
        return create_extension_binding_service(**kwargs)

    return _factory


def create_tool_execution_service(
    *,
    get_logger,
    artifact_plane=None,
    event_command_service_factory=None,
    get_extension_store=None,
) -> ToolExecutionService:
    return ToolExecutionService(
        get_logger=get_logger,
        artifact_plane=artifact_plane,
        event_command_service_factory=event_command_service_factory,
        get_extension_store=get_extension_store,
    )


def build_tool_execution_service_factory(**kwargs) -> Callable[[], ToolExecutionService]:
    def _factory() -> ToolExecutionService:
        return create_tool_execution_service(**kwargs)

    return _factory


def create_trash_lifecycle_service(
    *,
    get_logger,
    artifact_plane=None,
    get_project_dataset_catalog=None,
    get_audit_store=None,
    delete_project_queues=None,
    begin_project_executor_quiescence=None,
    end_project_executor_quiescence=None,
    workflow_secret_default_service=None,
) -> TrashLifecycleService:
    return TrashLifecycleService(
        get_logger=get_logger,
        artifact_plane=artifact_plane,
        get_project_dataset_catalog=get_project_dataset_catalog,
        get_audit_store=get_audit_store,
        delete_project_queues=delete_project_queues,
        begin_project_executor_quiescence=begin_project_executor_quiescence,
        end_project_executor_quiescence=end_project_executor_quiescence,
        workflow_secret_default_service=workflow_secret_default_service,
    )


def create_model_provider_command_service(*, get_logger) -> ModelProviderCommandService:
    return ModelProviderCommandService(get_logger=get_logger)
