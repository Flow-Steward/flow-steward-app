from __future__ import annotations

from unittest.mock import MagicMock

from core.application.workflows.workflow_provider_trigger_service import (
    WorkflowProviderTriggerCreateCommand,
    WorkflowProviderTriggerService,
)


def test_provider_trigger_create_validation_and_write_live_in_application_service(
    monkeypatch,
) -> None:
    from contextlib import nullcontext

    monkeypatch.setattr(
        "core.application.workflows.workflow_provider_trigger_service.workflow_trigger_transaction",
        lambda *_args, **_kwargs: nullcontext(),
    )
    create = MagicMock(return_value={"trigger_id": "trigger-1"})
    bl = MagicMock()
    bl.get_project.return_value = {"id": "project-1", "account_id": "account-1"}
    service = WorkflowProviderTriggerService(
        find_definition=lambda _bl, **_kwargs: {
            "provider_key": "sample_tasks",
            "trigger_type_id": "task.created",
            "display_name": "Task created",
        },
        validate_config=lambda _definition, _config, *, enabled: (
            "ready",
            [],
            "task.created",
        ),
        create_trigger=create,
        update_trigger=MagicMock(),
        delete_trigger=MagicMock(),
        get_trigger=MagicMock(),
        resolve_project_binding=lambda _bl, **_kwargs: ("project-1", {}),
        load_workflow=lambda _workflow_id: {"account_id": "account-1"},
        is_scope_conflict=lambda _exc: False,
    )

    result, definition = service.create(
        bl=bl,
        command=WorkflowProviderTriggerCreateCommand(
            workflow_id="workflow-1",
            trigger_id="trigger-1",
            project_id="project-1",
            provider_key="sample_tasks",
            trigger_type_id="task.created",
            display_name="",
            enabled=True,
            trigger_config={},
        ),
        authorize_workflow=lambda workflow_id: None,
        authorize_project=lambda project_id: None,
    )

    assert result["trigger_id"] == "trigger-1"
    assert definition["provider_key"] == "sample_tasks"
    assert create.call_args.kwargs["readiness_status"] == "ready"
    assert create.call_args.kwargs["event_name"] == "task.created"
