from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.application.management.command_models import ProjectCreateInput
from core.application.management.command_service_runtime import (
    attachment_policy_from_config,
    build_job_command_service_factory,
    build_project_command_service_factory,
    create_approval_command_service,
    create_event_command_service,
    create_extension_binding_service,
    create_external_agent_command_service,
    create_job_command_service,
    create_job_command_service_from_runtime,
    create_model_provider_command_service,
    create_project_command_service,
    create_project_command_service_from_runtime,
    create_tool_execution_service,
    create_trash_lifecycle_service,
)
from core.application.management.command_services import (
    ApprovalCommandService,
    EventCommandService,
    ExtensionBindingService,
    ExternalAgentCommandService,
    JobCommandService,
    ModelProviderCommandService,
    ProjectCommandService,
    ToolExecutionService,
    TrashLifecycleService,
)


def test_attachment_policy_from_config_normalizes_sets() -> None:
    policy = attachment_policy_from_config(
        {
            "attachment_max_size_bytes": 1024,
            "attachment_max_files": 3,
            "allowed_attachment_mimes": {"text/plain", "image/png"},
            "allowed_attachment_extensions": {".txt", ".png"},
        }
    )

    assert policy["attachment_max_size_bytes"] == 1024
    assert policy["attachment_max_files"] == 3
    assert policy["allowed_attachment_mimes"] == {"text/plain", "image/png"}
    assert policy["allowed_attachment_extensions"] == {".txt", ".png"}


def test_create_project_command_service_reads_env_backed_attachment_policy() -> None:
    service = create_project_command_service(
        config={
            "attachment_max_size_bytes": "2048",
            "attachment_max_files": "4",
            "allowed_attachment_mimes": {"TEXT/PLAIN", "image/png"},
            "allowed_attachment_extensions": {".TXT", ".png"},
        },
        get_logger=lambda: None,
        resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
        validate_explicit_workflow_override=lambda *args, **kwargs: None,
        sanitize_external_input=lambda value: value,
        enqueue_task=lambda **kwargs: 1,
        normalize_cron_expr=lambda value: value,
        cron_next_run_at=lambda expr, start_at=None: start_at,
        form_flag=bool,
        run_scheduled_job_once=lambda **kwargs: {},
        parse_agent_model_overrides=lambda form_data: {},
        build_project_policy_from_form=lambda form_data: {},
        list_workflow_choices=lambda **kwargs: [],
        write_project_profile_to_kb=lambda profile: None,
        to_json_obj=lambda value: value,
        parse_test_mode_fn=lambda value, default: str(value or "").strip().lower() == "smoke",
    )

    assert isinstance(service, ProjectCommandService)
    assert service._attachment_max_size_bytes == 2048
    assert service._attachment_max_files == 4
    assert service._allowed_attachment_mimes == {"text/plain", "image/png"}
    assert service._allowed_attachment_extensions == {".txt", ".png"}
    assert service._parse_test_mode("smoke", False) is True


def test_create_project_command_service_from_runtime_uses_grouped_helpers() -> None:
    workflow_helpers = SimpleNamespace(
        agent_model_override_targets=lambda default_model: ["writer"],
        normalize_cron_expr=lambda value: value,
        cron_next_run_at=lambda expr, start_at=None: start_at,
        form_flag=bool,
        parse_agent_model_overrides=lambda form_data, agent_names: {
            "agent_names": list(agent_names)
        },
        build_project_policy_from_form=lambda form_data: {},
    )

    service = create_project_command_service_from_runtime(
        config={
            "attachment_max_size_bytes": "2048",
            "attachment_max_files": "4",
            "allowed_attachment_mimes": {"TEXT/PLAIN", "image/png"},
            "allowed_attachment_extensions": {".TXT", ".png"},
        },
        get_logger=lambda: None,
        resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
        validate_explicit_workflow_override=lambda **kwargs: None,
        sanitize_external_input=lambda value: value,
        enqueue_task=lambda **kwargs: 1,
        run_scheduled_job_once=lambda **kwargs: {},
        list_workflow_choices=lambda **kwargs: [],
        write_project_profile_to_kb=lambda profile: None,
        to_json_obj=lambda value: value,
        workflow_helpers_module=workflow_helpers,
    )

    assert isinstance(service, ProjectCommandService)
    assert service._attachment_max_size_bytes == 2048
    assert service._attachment_max_files == 4
    assert service._parse_agent_model_overrides(
        {},
    ) == {"agent_names": ["writer"]}


def test_production_project_runtime_creates_blank_route_without_catalog_lookup() -> None:
    from core.application.workflows import serve_workflow_helpers

    created: dict = {}

    class _WorkspaceCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=()):
            return None

        def fetchone(self):
            return (
                "ws_default",
                "00000000-0000-0000-0000-000000000002",
                None,
                "default",
                "Default",
                "active",
                None,
            )

    class _WorkspaceConnection:
        def cursor(self):
            return _WorkspaceCursor()

    class _Logger:
        conn = _WorkspaceConnection()

        def add_project(self, **payload):
            created.update(payload)
            return "project-1"

    service = create_project_command_service_from_runtime(
        config={},
        get_logger=_Logger,
        resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
        validate_explicit_workflow_override=lambda **kwargs: None,
        sanitize_external_input=lambda value: value,
        enqueue_task=lambda **kwargs: 1,
        run_scheduled_job_once=lambda **kwargs: {},
        list_workflow_choices=lambda **kwargs: [],
        write_project_profile_to_kb=lambda profile: None,
        to_json_obj=lambda value: value,
        workflow_helpers_module=serve_workflow_helpers,
    )

    result = service.create_project(
        ProjectCreateInput(
            name="Blank route project",
            workspace_id="ws_default",
            form_data={"name": "Blank route project", "workspace_id": "ws_default"},
        )
    )

    assert result.project_id == "project-1"
    assert created["agents_config"] == {}


def test_build_project_command_service_factory_returns_zero_arg_factory() -> None:
    factory = build_project_command_service_factory(
        config={},
        get_logger=lambda: None,
        resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
        validate_explicit_workflow_override=lambda **kwargs: None,
        sanitize_external_input=lambda value: value,
        enqueue_task=lambda **kwargs: 1,
        run_scheduled_job_once=lambda **kwargs: {},
        list_workflow_choices=lambda **kwargs: [],
        write_project_profile_to_kb=lambda profile: None,
        to_json_obj=lambda value: value,
        workflow_helpers_module=SimpleNamespace(
            agent_model_override_targets=lambda default_model: [],
            normalize_cron_expr=lambda value: value,
            cron_next_run_at=lambda expr, start_at=None: start_at,
            form_flag=bool,
            parse_agent_model_overrides=lambda form_data, agent_names: {},
            build_project_policy_from_form=lambda form_data: {},
        ),
    )

    assert isinstance(factory(), ProjectCommandService)


def test_create_job_command_service_wires_runtime_dependencies() -> None:
    service = create_job_command_service(
        get_logger=lambda: None,
        build_rerun_workflow_selection=lambda **kwargs: {},
        enqueue_task=lambda **kwargs: 1,
        get_thread_state_values=lambda thread_id: {},
        runtime_step_scope_id=lambda runtime_state: "",
        latest_runtime_message_id=lambda runtime_state, phase_id="": None,
        override_workflow_id_from_value=lambda value: "",
        is_empty_original_value=lambda value: True,
        derive_override_original_value=lambda **kwargs: {},
        validate_explicit_workflow_override=lambda **kwargs: None,
        to_json_obj=lambda value: value,
        serialize_decision_override_row=lambda row: row,
        serialize_job_step_replay_row=lambda row: row,
        sanitize_retryable_interrupted_step_snapshot=lambda value: value,
        resolve_job_project_name=lambda bl, job: "",
        append_runtime_journal=lambda *args, **kwargs: {},
        enqueue_task_in_queue=lambda **kwargs: None,
        resolve_replay_message_row=lambda **kwargs: (None, None, 200),
        collect_replay_step_trace=lambda *args, **kwargs: {},
        extract_replay_prompt_capture=lambda payload: {},
        derive_original_replay_outcome=lambda payload: {},
        invoke_step_reexecute_runtime=lambda **kwargs: {},
        invoke_step_replay_llm=lambda **kwargs: {},
        build_replay_diff_excerpt=lambda original, replayed: "",
        retryable_interrupted_step_status="retryable_interrupted_step",
    )

    assert isinstance(service, JobCommandService)
    assert service._retryable_interrupted_step_status == "retryable_interrupted_step"


def test_create_job_command_service_from_runtime_uses_grouped_modules() -> None:
    snapshot_serializers = SimpleNamespace(
        serialize_decision_override_row=lambda row: row,
        serialize_job_step_replay_row=lambda row: row,
    )
    workflow_override_runtime = SimpleNamespace(
        runtime_step_scope_id=lambda runtime_state: "",
        latest_runtime_message_id=lambda runtime_state, phase_id="": None,
        override_workflow_id_from_value=lambda value: "",
        is_empty_original_value=lambda value: True,
        derive_override_original_value=lambda **kwargs: {},
        validate_explicit_workflow_override=lambda **kwargs: None,
    )
    interrupted_step_runtime = SimpleNamespace(
        sanitize_interrupted_step_snapshot=lambda value: value,
    )
    query_service_runtime = SimpleNamespace(
        resolve_replay_message_row=lambda **kwargs: (None, None, 200),
    )
    step_replay_runtime = SimpleNamespace(
        extract_replay_prompt_capture=lambda payload: {},
        derive_original_replay_outcome=lambda payload: {},
        invoke_step_replay_llm=lambda **kwargs: {},
        build_replay_diff_excerpt=lambda original, replayed: "",
    )

    service = create_job_command_service_from_runtime(
        get_logger=lambda: None,
        build_rerun_workflow_selection=lambda **kwargs: {},
        enqueue_task=lambda **kwargs: 1,
        get_thread_state_values=lambda thread_id: {},
        to_json_obj=lambda value: value,
        resolve_job_project_name=lambda bl, job: "",
        append_runtime_journal=lambda *args, **kwargs: {},
        enqueue_task_in_queue=lambda **kwargs: None,
        collect_replay_step_trace=lambda bl, job_id, row: {},
        invoke_step_reexecute_runtime=lambda **kwargs: {},
        workflow_override_runtime_module=workflow_override_runtime,
        interrupted_step_runtime_module=interrupted_step_runtime,
        query_service_runtime_module=query_service_runtime,
        step_replay_runtime_module=step_replay_runtime,
        snapshot_serializers_module=snapshot_serializers,
        retryable_interrupted_step_status="retryable_interrupted_step",
    )

    assert isinstance(service, JobCommandService)
    assert service._retryable_interrupted_step_status == "retryable_interrupted_step"


def test_build_job_command_service_factory_returns_zero_arg_factory() -> None:
    factory = build_job_command_service_factory(
        get_logger=lambda: None,
        build_rerun_workflow_selection=lambda **kwargs: {},
        enqueue_task=lambda **kwargs: 1,
        get_thread_state_values=lambda thread_id: {},
        to_json_obj=lambda value: value,
        resolve_job_project_name=lambda bl, job: "",
        append_runtime_journal=lambda *args, **kwargs: {},
        enqueue_task_in_queue=lambda **kwargs: None,
        collect_replay_step_trace=lambda bl, job_id, row: {},
        invoke_step_reexecute_runtime=lambda **kwargs: {},
        workflow_override_runtime_module=SimpleNamespace(
            runtime_step_scope_id=lambda runtime_state: "",
            latest_runtime_message_id=lambda runtime_state, phase_id="": None,
            override_workflow_id_from_value=lambda value: "",
            is_empty_original_value=lambda value: True,
            derive_override_original_value=lambda **kwargs: {},
            validate_explicit_workflow_override=lambda **kwargs: None,
        ),
        interrupted_step_runtime_module=SimpleNamespace(
            sanitize_interrupted_step_snapshot=lambda value: value,
        ),
        query_service_runtime_module=SimpleNamespace(
            resolve_replay_message_row=lambda **kwargs: (None, None, 200),
        ),
        step_replay_runtime_module=SimpleNamespace(
            extract_replay_prompt_capture=lambda payload: {},
            derive_original_replay_outcome=lambda payload: {},
            invoke_step_replay_llm=lambda **kwargs: {},
            build_replay_diff_excerpt=lambda original, replayed: "",
        ),
        snapshot_serializers_module=SimpleNamespace(
            serialize_decision_override_row=lambda row: row,
            serialize_job_step_replay_row=lambda row: row,
        ),
        retryable_interrupted_step_status="retryable_interrupted_step",
    )

    assert isinstance(factory(), JobCommandService)


def test_create_approval_command_service_uses_current_constructor_contract() -> None:
    service = create_approval_command_service(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: None,
        get_logger=lambda: None,
        enqueue_workflow_resume=lambda **kwargs: None,
        workflow_resume_repository_factory=lambda bl: None,
    )

    assert isinstance(service, ApprovalCommandService)


def test_entrypoints_compose_durable_resume_through_infrastructure_wiring() -> None:
    root = Path(__file__).parents[2]
    wiring = (root / "core/infrastructure/runtime/workflow_resume_wiring.py").read_text(
        encoding="utf-8"
    )
    api = (root / "core/entrypoints/api/app_factory.py").read_text(encoding="utf-8")
    cli = (root / "core/entrypoints/cli/approval_command_runtime.py").read_text(encoding="utf-8")

    assert "enqueue_workflow_resume" in wiring
    assert "WorkflowResumeRepositoryAdapter" in wiring
    assert "workflow_resume_command_ports()" in api
    assert "workflow_resume_command_ports()" in cli


def test_create_event_command_service_reads_attachment_policy_from_config() -> None:
    service = create_event_command_service(
        config={
            "attachment_max_size_bytes": "2048",
            "attachment_max_files": "4",
            "allowed_attachment_mimes": {"TEXT/PLAIN", "image/png"},
            "allowed_attachment_extensions": {".TXT", ".png"},
        },
        get_logger=lambda: None,
        resolve_domain_event_by_ref=lambda bl, event_ref, **kwargs: None,
        to_json_obj=lambda value: value,
        serialize_domain_event_datetime=lambda value: None,
        sanitize_external_input=lambda value: value,
        enqueue_task=lambda **kwargs: 1,
        workflow_exists=lambda workflow_id: True,
        evaluate_workflow_reactions=lambda bl, event: [],
        reaction_action_result_executed=lambda result: bool(result),
        dispatch_project_notification_event=lambda *args, **kwargs: [],
        fetch_provider_attachments=lambda *args, **kwargs: [],
    )

    assert isinstance(service, EventCommandService)
    assert service._attachment_max_size_bytes == 2048
    assert service._attachment_max_files == 4
    assert service._allowed_attachment_mimes == {"text/plain", "image/png"}
    assert service._allowed_attachment_extensions == {".txt", ".png"}


def test_misc_service_factories_return_expected_types() -> None:
    external = create_external_agent_command_service(get_logger=lambda: None)
    binding = create_extension_binding_service(
        get_logger=lambda: None,
        resolve_extension_runtime_record=lambda extension_id: ({}, None),
        extension_identifier=lambda extension: "ext",
        extension_operation_handler_mode=lambda extension, operation_type, operation_id: (
            "extension"
        ),
        extension_operation_failure_status=lambda payload: 500,
    )
    tool = create_tool_execution_service(get_logger=lambda: None)
    provider = create_model_provider_command_service(get_logger=lambda: None)
    trash = create_trash_lifecycle_service(get_logger=lambda: None)

    assert isinstance(external, ExternalAgentCommandService)
    assert isinstance(binding, ExtensionBindingService)
    assert isinstance(tool, ToolExecutionService)
    assert isinstance(provider, ModelProviderCommandService)
    assert isinstance(trash, TrashLifecycleService)
