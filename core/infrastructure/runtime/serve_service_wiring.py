"""Grouped wiring helpers for serve transport factories and scheduler bundles."""

from __future__ import annotations

import logging
from typing import Any

from core.application.extensions.artifact_presentation import ArtifactPresentationResolver
from core.application.jobs import scheduled_job_runtime
from core.application.management import (
    command_service_runtime,
    query_service_runtime,
)
from core.infrastructure.runtime.state_reader_runtime import get_committed_checkpoint_values


def build_scheduled_job_bundle(
    *,
    scheduled_jobs_runtime_config_fn,
    control_run_scheduled_job_once,
    control_record_scheduled_job_outcome,
    control_scheduled_jobs_scan,
    scheduled_job_next_run_at_fn,
    as_utc_datetime_fn,
    run_scheduled_job_once_getter,
    get_project_run_config_fn=None,
):
    record_scheduled_job_outcome, run_scheduled_job_once = (
        scheduled_job_runtime.build_transport_helpers(
            scheduled_jobs_config_fn=scheduled_jobs_runtime_config_fn,
            control_run_scheduled_job_once=control_run_scheduled_job_once,
            control_record_scheduled_job_outcome=control_record_scheduled_job_outcome,
            scheduled_job_next_run_at_fn=scheduled_job_next_run_at_fn,
            as_utc_datetime_fn=as_utc_datetime_fn,
            get_project_run_config_fn=get_project_run_config_fn,
        )
    )

    scheduled_jobs_scan = scheduled_job_runtime.build_scheduled_jobs_scan_runner(
        scheduled_jobs_runtime_config_fn=scheduled_jobs_runtime_config_fn,
        control_scheduled_jobs_scan=control_scheduled_jobs_scan,
        as_utc_datetime_fn=as_utc_datetime_fn,
        run_scheduled_job_once_fn=lambda bl, scheduled_job, **kwargs: (
            run_scheduled_job_once_getter()(
                bl,
                scheduled_job,
                **kwargs,
            )
        ),
        scheduled_job_next_run_at_fn=scheduled_job_next_run_at_fn,
        record_scheduled_job_outcome_fn=lambda bl, **kwargs: record_scheduled_job_outcome(
            bl,
            **kwargs,
        ),
    )

    return record_scheduled_job_outcome, run_scheduled_job_once, scheduled_jobs_scan


def build_service_factories(
    *,
    config: dict[str, Any],
    artifact_plane=None,
    get_logger_only,
    get_job_store=None,
    get_project_store=None,
    get_event_store=None,
    get_trace_store=None,
    serialize_attention_job_row,
    serialize_awaiting_approval_job_row,
    job_attention_group,
    build_runtime_payload_for_api,
    build_display_summary,
    build_job_causal_chain,
    resolve_replay_message_row,
    collect_replay_step_trace,
    get_thread_state_snapshot,
    build_runtime_payload,
    resolve_job_project_name,
    snapshot_serializers_module,
    serialize_projects_grid_row,
    get_queue_depth,
    get_ready_queue_depth,
    to_json_obj,
    project_settings_diff,
    list_workflow_choices,
    sanitize_external_input,
    resolve_workflow_selection,
    validate_explicit_workflow_override,
    derive_override_original_value,
    build_task_envelope_for_preview,
    plan_workflow_preview,
    build_rerun_workflow_selection,
    load_replay_snapshot,
    enqueue_task,
    get_thread_state_values,
    append_runtime_journal,
    enqueue_task_in_queue,
    invoke_step_reexecute_runtime,
    invoke_step_replay_llm,
    workflow_override_runtime_module,
    interrupted_step_runtime_module,
    query_service_runtime_module,
    step_replay_runtime_module,
    retryable_interrupted_step_status: str,
    run_scheduled_job_once,
    write_project_profile_to_kb,
    workflow_helpers_module,
    workflow_availability_assert=None,
):
    job_store = get_job_store or get_logger_only
    project_store = get_project_store or get_logger_only
    job_query_service = query_service_runtime.build_job_query_service_factory(
        get_logger=lambda: job_store(),
        get_event_store=get_event_store,
        get_trace_store=get_trace_store,
        serialize_attention_job_row=serialize_attention_job_row,
        serialize_awaiting_approval_job_row=serialize_awaiting_approval_job_row,
        job_attention_group=job_attention_group,
        build_runtime_payload_for_api=build_runtime_payload_for_api,
        build_display_summary=build_display_summary,
        build_job_causal_chain=build_job_causal_chain,
        resolve_replay_message_row=resolve_replay_message_row,
        collect_replay_step_trace=collect_replay_step_trace,
        get_thread_state_snapshot=get_thread_state_snapshot,
        get_committed_checkpoint_values=lambda thread_id: get_committed_checkpoint_values(
            thread_id, config=config, logger=logging.getLogger(__name__)
        ),
        build_runtime_payload=build_runtime_payload,
        resolve_job_project_name=resolve_job_project_name,
        resolve_artifact=ArtifactPresentationResolver(artifact_plane)
        if artifact_plane is not None
        else None,
        get_project_by_id=lambda project_id: project_store().get_project(project_id),
        snapshot_serializers_module=snapshot_serializers_module,
    )

    project_query_service = query_service_runtime.build_project_query_service_factory(
        get_logger=lambda: project_store(),
        get_job_store=lambda: job_store(),
        serialize_projects_grid_row=serialize_projects_grid_row,
        get_queue_depth=get_queue_depth,
        get_ready_queue_depth=get_ready_queue_depth,
        to_json_obj=to_json_obj,
        project_settings_diff=project_settings_diff,
        list_workflow_choices=list_workflow_choices,
        sanitize_external_input=sanitize_external_input,
        resolve_workflow_selection=resolve_workflow_selection,
        validate_explicit_workflow_override=validate_explicit_workflow_override,
        build_task_envelope_for_preview=build_task_envelope_for_preview,
        plan_workflow_preview=plan_workflow_preview,
    )

    job_command_service = command_service_runtime.build_job_command_service_factory(
        get_logger=lambda: job_store(),
        get_project_run_config=lambda project_id: project_store().get_project_run_config(
            project_id
        ),
        build_rerun_workflow_selection=build_rerun_workflow_selection,
        load_replay_snapshot=load_replay_snapshot,
        enqueue_task=enqueue_task,
        get_thread_state_values=get_thread_state_values,
        to_json_obj=to_json_obj,
        resolve_job_project_name=resolve_job_project_name,
        append_runtime_journal=append_runtime_journal,
        enqueue_task_in_queue=enqueue_task_in_queue,
        collect_replay_step_trace=collect_replay_step_trace,
        invoke_step_reexecute_runtime=invoke_step_reexecute_runtime,
        workflow_override_runtime_module=workflow_override_runtime_module,
        interrupted_step_runtime_module=interrupted_step_runtime_module,
        query_service_runtime_module=query_service_runtime_module,
        step_replay_runtime_module=step_replay_runtime_module,
        snapshot_serializers_module=snapshot_serializers_module,
        retryable_interrupted_step_status=retryable_interrupted_step_status,
        derive_override_original_value=derive_override_original_value,
        validate_explicit_workflow_override=validate_explicit_workflow_override,
        invoke_step_replay_llm=invoke_step_replay_llm,
    )

    project_command_service = command_service_runtime.build_project_command_service_factory(
        config=config,
        get_logger=lambda: project_store(),
        resolve_workflow_selection=resolve_workflow_selection,
        validate_explicit_workflow_override=validate_explicit_workflow_override,
        sanitize_external_input=sanitize_external_input,
        enqueue_task=enqueue_task,
        enqueue_task_in_queue=enqueue_task_in_queue,
        run_scheduled_job_once=run_scheduled_job_once,
        list_workflow_choices=list_workflow_choices,
        workflow_availability_assert=workflow_availability_assert,
        write_project_profile_to_kb=write_project_profile_to_kb,
        to_json_obj=to_json_obj,
        workflow_helpers_module=workflow_helpers_module,
        artifact_plane=artifact_plane,
    )

    return job_query_service, project_query_service, job_command_service, project_command_service
