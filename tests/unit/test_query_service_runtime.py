from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.management.project_profile_kb import write_project_profile_to_kb
from core.application.management.query_service_runtime import (
    build_job_query_service_factory,
    build_project_query_service_factory,
    create_approval_query_service,
    create_event_query_service,
    create_extension_query_service,
    create_job_query_service,
    create_job_query_service_from_runtime,
    create_project_query_service,
    create_project_query_service_from_runtime,
    job_attention_group,
    resolve_replay_message_row,
    serialize_attention_job_row,
    serialize_awaiting_approval_job_row,
)
from core.application.management.query_services import (
    ApprovalQueryService,
    EventQueryService,
    ExtensionQueryService,
    JobQueryService,
    ProjectQueryService,
)


@pytest.mark.parametrize("generated", [True, False])
def test_job_list_summary_provenance_does_not_guess_from_owner_text(generated: bool) -> None:
    from core.application.management.query_models import JobsListView
    from core.application.reports.display_text import build_display_summary

    row = {
        "id": "generated-job" if generated else "owner-job",
        "status": "pending",
        "selected_workflow": "weekly_report",
        "task_summary": "Workflow run: weekly_report",
        "task_source": "schedule",
    }
    if generated:
        row.update(
            task_summary_key="react.jobs.generated_workflow_run_summary",
            task_summary_params={"workflow_id": "weekly_report"},
        )
    serialized = serialize_attention_job_row(
        row,
        truncate_text_fn=lambda value, max_len=0: str(value or "")[: max_len or None],
        build_display_summary_fn=build_display_summary,
    )
    payload = JobsListView.model_validate({"jobs": [serialized]}).dump()["jobs"][0]

    assert payload["task_summary"] == "Workflow run: weekly_report"
    if generated:
        assert payload.get("task_summary_key") == "react.jobs.generated_workflow_run_summary"
        assert payload.get("task_summary_params") == {"workflow_id": "weekly_report"}
    else:
        assert not payload.get("task_summary_key")
        assert not payload.get("task_summary_params")


def test_create_job_query_service_wires_runtime_dependencies() -> None:
    service = create_job_query_service(
        get_logger=lambda: None,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda status, acknowledged_at=None: "running",
        build_runtime_payload_for_api=lambda job, bl: None,
        build_display_summary=lambda value: str(value or ""),
        serialize_log_row=lambda row, job_id: row,
        serialize_event_row=lambda row: row,
        serialize_agent_message_row=lambda row: row,
        serialize_decision_override_row=lambda row: row,
        serialize_job_error_row=lambda row: row,
        serialize_job_step_replay_row=lambda row: row,
        build_job_causal_chain=lambda bl, job_id: {},
        resolve_replay_message_row=lambda **kwargs: (None, None, 200),
        collect_replay_step_trace=lambda bl, job_id, row: {},
        get_thread_state_snapshot=lambda thread_id: None,
        build_runtime_payload=lambda job, bl: {},
        resolve_job_project_name=lambda bl, job: "Demo",
    )

    assert isinstance(service, JobQueryService)
    assert service._resolve_job_project_name(None, {}) == "Demo"


def test_create_project_query_service_wires_runtime_dependencies() -> None:
    service = create_project_query_service(
        get_logger=lambda: None,
        serialize_projects_grid_row=lambda row: row,
        get_queue_depth=lambda project_id: 0,
        get_ready_queue_depth=lambda: 0,
        to_json_obj=lambda value: value,
        project_settings_diff=lambda from_snapshot, to_snapshot: {},
        list_workflow_choices=lambda **kwargs: [],
        sanitize_external_input=lambda value: value,
        resolve_workflow_selection=lambda requested, project_workflows=None: ("", "auto"),
        validate_explicit_workflow_override=lambda **kwargs: None,
        build_task_envelope_for_preview=lambda task_summary: {},
        normalize_orchestrator_state=lambda state: state,
        plan_workflow_preview=lambda envelope, state: [],
    )

    assert isinstance(service, ProjectQueryService)


def test_create_job_query_service_from_runtime_uses_grouped_serializers() -> None:
    snapshot_serializers = SimpleNamespace(
        serialize_log_row=lambda row, job_id: row,
        serialize_event_row=lambda row: row,
        serialize_agent_message_row=lambda row: row,
        serialize_decision_override_row=lambda row: row,
        serialize_job_error_row=lambda row: row,
        serialize_job_step_replay_row=lambda row: row,
    )

    service = create_job_query_service_from_runtime(
        get_logger=lambda: None,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda status, acknowledged_at=None: "running",
        build_runtime_payload_for_api=lambda job, bl: None,
        build_display_summary=lambda value: str(value or ""),
        build_job_causal_chain=lambda bl, job_id: {},
        resolve_replay_message_row=lambda **kwargs: (None, None, 200),
        collect_replay_step_trace=lambda bl, job_id, row: {},
        get_thread_state_snapshot=lambda thread_id: None,
        build_runtime_payload=lambda job, bl: {},
        resolve_job_project_name=lambda bl, job: "Demo",
        snapshot_serializers_module=snapshot_serializers,
    )

    assert isinstance(service, JobQueryService)
    assert service._resolve_job_project_name(None, {}) == "Demo"


def test_build_job_query_service_factory_returns_zero_arg_factory() -> None:
    factory = build_job_query_service_factory(
        get_logger=lambda: None,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda status, acknowledged_at=None: "running",
        build_runtime_payload_for_api=lambda job, bl: None,
        build_display_summary=lambda value: str(value or ""),
        build_job_causal_chain=lambda bl, job_id: {},
        resolve_replay_message_row=lambda **kwargs: (None, None, 200),
        collect_replay_step_trace=lambda bl, job_id, row: {},
        get_thread_state_snapshot=lambda thread_id: None,
        build_runtime_payload=lambda job, bl: {},
        resolve_job_project_name=lambda bl, job: "Demo",
        snapshot_serializers_module=SimpleNamespace(
            serialize_log_row=lambda row, job_id: row,
            serialize_event_row=lambda row: row,
            serialize_agent_message_row=lambda row: row,
            serialize_decision_override_row=lambda row: row,
            serialize_job_error_row=lambda row: row,
            serialize_job_step_replay_row=lambda row: row,
        ),
    )

    assert isinstance(factory(), JobQueryService)


def test_create_project_query_service_from_runtime_imports_state_normalizer() -> None:
    service = create_project_query_service_from_runtime(
        get_logger=lambda: None,
        serialize_projects_grid_row=lambda row: row,
        get_queue_depth=lambda project_id: 0,
        get_ready_queue_depth=lambda: 0,
        to_json_obj=lambda value: value,
        project_settings_diff=lambda from_snapshot, to_snapshot: {},
        list_workflow_choices=lambda **kwargs: [],
        sanitize_external_input=lambda value: value,
        resolve_workflow_selection=lambda requested, project_workflows=None: ("", "auto"),
        validate_explicit_workflow_override=lambda **kwargs: None,
        build_task_envelope_for_preview=lambda task_summary: {},
        plan_workflow_preview=lambda envelope, state: [],
    )

    assert isinstance(service, ProjectQueryService)


def test_build_project_query_service_factory_returns_zero_arg_factory() -> None:
    factory = build_project_query_service_factory(
        get_logger=lambda: None,
        serialize_projects_grid_row=lambda row: row,
        get_queue_depth=lambda project_id: 0,
        get_ready_queue_depth=lambda: 0,
        to_json_obj=lambda value: value,
        project_settings_diff=lambda from_snapshot, to_snapshot: {},
        list_workflow_choices=lambda **kwargs: [],
        sanitize_external_input=lambda value: value,
        resolve_workflow_selection=lambda requested, project_workflows=None: ("", "auto"),
        validate_explicit_workflow_override=lambda **kwargs: None,
        build_task_envelope_for_preview=lambda task_summary: {},
        plan_workflow_preview=lambda envelope, state: [],
    )

    assert isinstance(factory(), ProjectQueryService)


def test_create_query_factories_return_expected_service_types() -> None:
    approval = create_approval_query_service(
        get_logger=lambda: None,
        get_thread_state_snapshot=lambda thread_id: None,
        approval_fallback_payload=lambda job: {},
        to_json_obj=lambda value: value,
    )
    event = create_event_query_service(
        get_logger=lambda: None,
        event_project_name=lambda bl, project_id, cache: "",
        resolve_domain_event_by_ref=lambda bl, event_ref, **kwargs: None,
        serialize_domain_event_summary=lambda row, project_name="": {},
        serialize_domain_event_datetime=lambda value: None,
        to_json_obj=lambda value: value,
        workflow_exists=lambda workflow_id: True,
    )
    extension = create_extension_query_service(
        get_logger=lambda: None,
        resolve_extension_runtime_record=lambda extension_id, *, account_id=None: ({}, None),
        build_extension_compiled_manifest=lambda extension: {},
        extension_validation_summary=lambda extension: {},
    )

    assert isinstance(approval, ApprovalQueryService)
    assert isinstance(event, EventQueryService)
    assert isinstance(extension, ExtensionQueryService)


def test_job_attention_group_maps_statuses_into_ui_buckets() -> None:
    assert (
        job_attention_group(
            "failed",
            None,
            retryable_interrupted_step_status="retryable_step",
        )
        == "needs_attention"
    )
    assert (
        job_attention_group(
            "retryable_step",
            None,
            retryable_interrupted_step_status="retryable_step",
        )
        == "blocked"
    )
    assert (
        job_attention_group(
            "completed",
            None,
            retryable_interrupted_step_status="retryable_step",
        )
        == "done"
    )


def test_attention_row_serializers_format_dates_and_display_summary() -> None:
    now = datetime.now(UTC)
    attention_row = serialize_attention_job_row(
        {
            "id": 12,
            "project_id": 7,
            "project_name": "Demo",
            "task_id": "task-1",
            "task_summary": "Trim noisy job payload",
            "status": "failed",
            "selected_workflow": "inventory_import",
            "selected_workflow_display_name": "Inventory Import",
            "queued_at": now,
            "started_at": now,
            "finished_at": None,
            "heartbeat_at": None,
            "acknowledged_at": None,
        },
        truncate_text_fn=lambda value, max_len=0: str(value or "")[: max_len or None],
        build_display_summary_fn=lambda value: f"Summary: {value}",
    )
    awaiting_row = serialize_awaiting_approval_job_row(
        {
            "id": 44,
            "project_id": 7,
            "project_name": "Demo",
            "task_summary": "Review deployment plan",
            "selected_workflow": "wf.review",
            "thread_id": "thread-44",
            "status": "awaiting_approval",
            "started_at": now,
        },
        truncate_text_fn=lambda value, max_len=0: str(value or "")[: max_len or None],
        build_display_summary_fn=lambda value: f"Summary: {value}",
    )

    assert attention_row["display_summary"] == "Summary: Trim noisy job payload"
    assert attention_row["status"] == "failed"
    assert attention_row["status_label"] == "Failed"
    assert attention_row["attention_group"] == "needs_attention"
    assert attention_row["selected_workflow"] == "inventory_import"
    assert attention_row["selected_workflow_display_name"] == "Inventory Import"
    assert attention_row["queued_at"] == now.isoformat()
    assert awaiting_row["task_summary"] == "Summary: Review deployment plan"
    assert awaiting_row["selected_workflow"] == "wf.review"
    assert awaiting_row["thread_id"] == "thread-44"
    assert awaiting_row["started_at"] == now.isoformat()
    assert awaiting_row["status"] == "awaiting_approval"
    assert awaiting_row["status_label"] == "Waiting for approval"


def test_resolve_replay_message_row_supports_message_id_and_correlation_lookup() -> None:
    logger = MagicMock()
    logger.get_agent_message_by_id.return_value = {"id": 5, "job_id": 9}

    row, error, status_code = resolve_replay_message_row(logger, job_id=9, message_id=5)

    assert row == {"id": 5, "job_id": 9}
    assert error is None
    assert status_code == 200

    logger.get_agent_messages.return_value = [
        {"id": 1, "job_id": 9, "correlation_id": "first"},
        {"id": 2, "job_id": 9, "correlation_id": "match-me"},
    ]
    row, error, status_code = resolve_replay_message_row(
        logger,
        job_id=9,
        correlation_id="match-me",
    )

    assert row == {"id": 2, "job_id": 9, "correlation_id": "match-me"}
    assert error is None
    assert status_code == 200


def test_write_project_profile_to_kb_forwards_rules_and_data_connections() -> None:
    calls: list[dict] = []

    write_project_profile_to_kb(
        {
            "name": "Demo",
            "project_rules": "keep it small",
            "assigned_data_connections": [
                {"connection_id": "warehouse-primary", "display_name": "Warehouse Primary"}
            ],
        },
        logger=MagicMock(),
        write_project_profile=lambda **kwargs: calls.append(dict(kwargs)),
    )

    assert calls[0]["project_name"] == "Demo"
    assert calls[0]["project_rules"] == "keep it small"
    assert calls[0]["assigned_data_connections"][0]["connection_id"] == "warehouse-primary"
    assert calls[0]["project_id"] is None


def test_history_list_projection_preserves_exact_source_and_zero_persisted_duration():
    from core.application.management.query_models import JobsListView

    row = {
        "id": "run",
        "project_id": "project",
        "selected_workflow": "workflow",
        "status": "completed",
        "task_source": "workflow_test_run",
        "duration_ms": 0,
        "started_at": datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
        "meta": {"inputs": {"secret": "HISTORY_SECRET_SENTINEL"}},
    }
    projected = serialize_attention_job_row(
        row,
        truncate_text_fn=lambda value, max_len=0: str(value or "")[: max_len or None],
        build_display_summary_fn=lambda value: str(value or ""),
    )
    payload = JobsListView.model_validate({"jobs": [projected]}).dump()["jobs"][0]
    assert payload["job_id"] == "run"
    assert payload["project_id"] == "project"
    assert payload["selected_workflow"] == "workflow"
    assert payload["task_source"] == "workflow_test_run"
    assert payload["duration_ms"] == 0
    assert payload["started_at"] == "2026-10-18T12:00:00Z"
    assert "meta" not in payload
    assert "HISTORY_SECRET_SENTINEL" not in str(payload)


def test_attention_row_labels_a_hook_run_as_the_hook_not_its_storage_workflow() -> None:
    def _serialize(row: dict) -> dict:
        return serialize_attention_job_row(
            {"id": 12, "project_id": 7, "status": "completed", "task_summary": "x", **row},
            truncate_text_fn=lambda value, max_len=0: str(value or ""),
            build_display_summary_fn=lambda value: str(value or ""),
        )

    hook_run = _serialize(
        {
            "selected_workflow": "hook_storage_wf_1",
            "selected_workflow_display_name": "Before finish",
            "hook_storage": True,
            "hook_author_workflow_display_name": "Order fulfilment",
        }
    )
    ordinary = _serialize(
        {
            "selected_workflow": "inventory_import",
            "selected_workflow_display_name": "Inventory Import",
            "hook_storage": False,
            "hook_author_workflow_display_name": None,
        }
    )
    hook_without_parent = _serialize(
        {
            "selected_workflow": "hook_storage_wf_2",
            "selected_workflow_display_name": "Before finish",
            "hook_storage": True,
        }
    )

    assert hook_run["selected_workflow_display_name"] == "Before finish · Order fulfilment"
    assert ordinary["selected_workflow_display_name"] == "Inventory Import"
    assert hook_without_parent["selected_workflow_display_name"] == "Before finish"
