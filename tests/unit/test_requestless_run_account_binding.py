"""Paths that start or resume runs without a request bind the run's own account.

ADR 0020, C5: a registry read that names no account and finds none bound is
refused. A public webhook, a worker poll or a sweep has no principal, so each binds
the account its own row names (the entrypoint, the project, the job, the event)
before the admission and the start read the registry. The provider-event dispatch
and the provider trigger webhook have their own tests in
test_provider_event_trigger_dispatch.py and test_api_webhooks.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from core.application.management.command_models import (
    ProviderWebhookIngestInput,
    ReactionEventIngestInput,
)
from core.application.management.command_services import EventCommandService
from core.application.registry.workflow_account import bound_workflow_account
from core.application.workflows.reaction_runtime import list_account_workflow_reactions
from core.application.workflows.workflow_entrypoint_models import (
    WorkflowEntrypointPreparation,
)
from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes


def test_public_workflow_webhook_runs_in_the_entrypoints_account() -> None:
    controller = object.__new__(execute_routes._WorkflowEntrypointHttpController)
    controller._entry_key = "hook-1"
    controller._service = SimpleNamespace(
        prepare=lambda _key: WorkflowEntrypointPreparation(
            entrypoint={"entrypoint_id": "ep-1", "account_id": "acct-hook"},
            project_id="proj-1",
            workflow_id="wf-1",
        )
    )
    bound: list[str | None] = []

    async def _execute_prepared(preparation):
        bound.append(bound_workflow_account())
        return "response"

    controller._execute_prepared = _execute_prepared

    assert asyncio.run(controller.execute()) == "response"
    assert bound == ["acct-hook"]


def _event_service(bl: Any, job_store: Any = None) -> EventCommandService:
    return EventCommandService(
        get_logger=lambda: bl,
        get_job_store=(lambda: job_store) if job_store is not None else None,
        resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
        to_json_obj=lambda value: value,
        serialize_domain_event_datetime=lambda value: value,
        workflow_exists=lambda workflow_id: True,
    )


def test_provider_task_webhook_runs_in_the_projects_account() -> None:
    bl = MagicMock()
    bl.get_project.return_value = {"id": "proj-1", "account_id": "acct-project"}
    service = _event_service(bl)
    bound: list[str | None] = []
    service._ingest_provider_webhook = lambda command, scope=None: bound.append(
        bound_workflow_account()
    )

    service.ingest_provider_webhook(
        ProviderWebhookIngestInput(provider_key="sample_tasks", project_id="proj-1")
    )

    assert bound == ["acct-project"]


def test_reaction_event_runs_in_the_jobs_account_and_reads_the_job_once() -> None:
    job_store = MagicMock(spec=["get_job"])
    job_store.get_job.return_value = {"id": "55", "project_id": "p", "account_id": "acct-job"}
    service = _event_service(MagicMock(), job_store)
    seen: list[tuple[str | None, Any]] = []

    def _inner(command, *, scope=None, expected_project_id=None, loaded_job=None):
        seen.append((bound_workflow_account(), loaded_job))

    service._ingest_reaction_event = _inner

    service.ingest_reaction_event(
        ReactionEventIngestInput(event_type="external.comment.received", job_id="55")
    )

    assert seen == [("acct-job", job_store.get_job.return_value)]
    job_store.get_job.assert_called_once_with("55")


def test_reaction_rules_are_read_in_the_events_account() -> None:
    bl = MagicMock()
    bl.list_workflow_reactions.return_value = [{"reaction_key": "r"}]

    assert list_account_workflow_reactions(bl, "wf-1", "acct-event") == [{"reaction_key": "r"}]
    bl.list_workflow_reactions.assert_called_once_with(
        "wf-1", enabled_only=True, account_id="acct-event"
    )
