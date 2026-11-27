"""Every run names who or what started it (``meta.submitted_by``).

The approval journal copies the job's ``submitted_by`` into ``started_by``; a run
started by the scheduler, a webhook, a provider event or a child invocation must
name its source, and a run started through the API names the person (also through
a personal token, the token's owner) or the service account of the authenticated
scope, never a name the caller sent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import core.infrastructure.runtime.control.scans_scheduled_jobs as scheduled_mod
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.jobs import task_enqueue_runtime, task_scheduling
from core.application.management.command_models import ProjectTaskEnqueueInput
from core.application.management.command_services import (
    JobCommandService,
    ProjectCommandService,
)
from core.application.management.scope import ScopeContext
from core.application.workflows.workflow_start_runtime import (
    automated_run_started_by,
    run_started_by,
    stamp_run_started_by,
)
from core.infrastructure.runtime.control.config import ControlConfig

PROJECT_ID = "3"


def _scope(**fields: Any) -> ScopeContext:
    return ScopeContext(**{"account_id": DEFAULT_ACCOUNT_ID, **fields})


# --- who: the authenticated scope -----------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"actor_id": "alice", "principal_type": "operator", "auth_source": "local"},
            {"id": "alice", "kind": "person"},
        ),
        # A personal token acts as its owner: the scope carries the owner's username.
        (
            {"actor_id": "alice", "principal_type": "admin", "auth_source": "personal_token"},
            {"id": "alice", "kind": "person"},
        ),
        (
            {"actor_id": "billing-sync", "principal_type": "service_account"},
            {"id": "billing-sync", "kind": "service_account"},
        ),
        ({"actor_id": "system", "principal_type": "system_actor"}, None),
        ({"actor_id": "", "principal_type": "operator"}, None),
        ({"actor_id": "worker"}, None),
    ],
)
def test_run_started_by_names_only_an_authenticated_actor(fields, expected) -> None:
    assert run_started_by(_scope(**fields)) == expected
    assert run_started_by(None) is None


# --- what: the source of an automated start --------------------------------------------


@pytest.mark.parametrize(
    ("meta", "task_source", "extra_state", "expected"),
    [
        (
            {"scheduled_job_id": "s-1", "run_label": "Nightly import"},
            "schedule",
            None,
            {"id": "schedule:s-1", "kind": "schedule", "display_name": "Nightly import"},
        ),
        (
            {"entry_key": "orders-secret-key", "entrypoint_id": "ep-1", "run_label": "Shop orders"},
            "webhook",
            None,
            {"id": "webhook:ep-1", "kind": "webhook", "display_name": "Shop orders"},
        ),
        (
            {"entry_key": "orders"},
            "webhook",
            None,
            {
                "id": "webhook:key-1c168adb00d2",
                "kind": "webhook",
                "display_name": "webhook:key-1c168adb00d2",
            },
        ),
        (
            {"source_system": "acme-shop"},
            "provider",
            {"provider_key": "acme-shop"},
            {"id": "provider:acme-shop", "kind": "provider", "display_name": "acme-shop"},
        ),
        (
            {"provider_key": "acme-tracker"},
            "provider",
            None,
            {"id": "provider:acme-tracker", "kind": "provider", "display_name": "acme-tracker"},
        ),
        (
            {"invoke_parent_workflow_id": "parent-flow"},
            "invoke_workflow",
            None,
            {
                "id": "workflow:parent-flow",
                "kind": "workflow",
                "display_name": "workflow:parent-flow",
            },
        ),
        (
            {},
            "command_dispatch",
            None,
            {
                "id": "automation:command_dispatch",
                "kind": "automation",
                "display_name": "automation:command_dispatch",
            },
        ),
    ],
)
def test_an_automated_start_is_named_by_its_source(meta, task_source, extra_state, expected):
    assert (
        automated_run_started_by(meta, task_source=task_source, extra_state=extra_state) == expected
    )


def test_stamping_keeps_a_named_starter_and_fills_an_empty_one() -> None:
    person = {"id": "alice", "kind": "person"}

    assert stamp_run_started_by({"submitted_by": person}, task_source="schedule")[
        "submitted_by"
    ] == (person)
    assert stamp_run_started_by(
        {"submitted_by": {"id": " "}, "scheduled_job_id": "s-2"}, task_source="schedule"
    )["submitted_by"]["id"] == ("schedule:s-2")
    assert stamp_run_started_by(None, task_source="")["submitted_by"]["id"] == ("automation:system")


# --- every job creation stamps it -------------------------------------------------------


def test_enqueue_job_names_the_source_of_a_run_without_an_actor(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        task_enqueue_runtime,
        "_enqueue_job_unadmitted",
        lambda **kwargs: captured.update(kwargs) or "job-1",
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_run_title._running_registry_workflow",
        lambda _workflow_id, _account_id: None,
    )

    task_enqueue_runtime.enqueue_job(
        task_id="webhook:orders:r-1",
        task_summary="Webhook entrypoint orders",
        task_source="webhook",
        project_id="project-1",
        project_name="Demo",
        project_config={},
        extra_state={"workflow_id": "plain", "entry_key": "orders"},
        meta={"entry_key": "orders", "entrypoint_id": "ep-1", "run_label": "Shop orders"},
    )

    assert captured["meta"]["submitted_by"] == {
        "id": "webhook:ep-1",
        "kind": "webhook",
        "display_name": "Shop orders",
    }


@pytest.mark.parametrize("label", ["", "Shop orders"])
def test_a_webhook_starter_never_carries_the_entry_key(label: str) -> None:
    entry_key = "whk_9f3c1d7e2b5a4f60"
    for meta, state in (
        ({"entry_key": entry_key, "run_label": label}, None),
        ({"run_label": label}, {"entry_key": entry_key}),
        ({"entry_key": entry_key, "entrypoint_id": "ep-7", "run_label": label}, None),
    ):
        starter = automated_run_started_by(meta, task_source="webhook", extra_state=state)
        assert starter["kind"] == "webhook"
        assert entry_key not in starter["id"]
        assert entry_key not in starter["display_name"]


def test_a_webhook_launch_records_its_entrypoint_id_for_the_starter() -> None:
    from core.application.workflows.workflow_entrypoint_launch import (
        WorkflowEntrypointLaunchService,
    )

    captured: dict[str, Any] = {}
    WorkflowEntrypointLaunchService(
        enqueue_workflow_start=lambda **kwargs: captured.update(kwargs) or "job-9",
        now_iso=lambda: "2026-11-14T10:00:00Z",
    ).launch(
        entry_key="whk_9f3c1d7e2b5a4f60",
        task_id="webhook:x:r-1",
        project_id="project-1",
        run_config={},
        workflow_id="scrape",
        request_id="r-1",
        idempotency_key="",
        workflow_inputs={},
        workflow_interface={},
        files=[],
        request_context={},
        idempotency_identities=[],
        primary_idempotency_identity={},
        entry_label="Shop orders",
        entrypoint_id="ep-7",
    )

    meta = captured["job_meta"]
    assert meta["entrypoint_id"] == "ep-7"
    starter = automated_run_started_by(
        meta, task_source="webhook", extra_state=captured["extra_state"]
    )
    assert starter == {"id": "webhook:ep-7", "kind": "webhook", "display_name": "Shop orders"}


def test_a_webhook_request_hands_its_entrypoint_id_to_the_launch() -> None:
    from core.entrypoints.api import workflow_entrypoints_routes_execute

    source = Path(workflow_entrypoints_routes_execute.__file__).read_text(encoding="utf-8")
    assert 'entrypoint_id=str(preparation.entrypoint.get("entrypoint_id") or "").strip()' in source


def test_enqueue_job_keeps_the_actor_the_caller_named(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        task_enqueue_runtime,
        "_enqueue_job_unadmitted",
        lambda **kwargs: captured.update(kwargs) or "job-2",
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_run_title._running_registry_workflow",
        lambda _workflow_id, _account_id: None,
    )

    task_enqueue_runtime.enqueue_job(
        task_id="t-2",
        task_summary="Run",
        task_source="manual",
        project_id="project-1",
        project_name="Demo",
        project_config={},
        extra_state={"workflow_id": "plain"},
        meta={"submitted_by": {"id": "alice", "kind": "person"}},
    )

    assert captured["meta"]["submitted_by"] == {"id": "alice", "kind": "person"}


def _cfg() -> ControlConfig:
    return ControlConfig(
        watchdog_enabled=True,
        watchdog_interval=30.0,
        watchdog_auto_apply=True,
        watchdog_stuck_seconds=120,
        watchdog_orphan_grace_seconds=120,
        watchdog_pickup_grace_seconds=45,
        watchdog_max_journal_entries=200,
        scheduled_jobs_enabled=True,
        scheduled_jobs_interval=30.0,
        scheduled_jobs_batch_limit=20,
        dashboard_rollup_enabled=True,
        dashboard_rollup_interval=300.0,
        dashboard_rollup_backfill_days=180,
        dashboard_rollup_lookback_days=3,
        security_posture_enabled=True,
        security_posture_interval=600.0,
        workflow_knowledge_sync_enabled=True,
        workflow_knowledge_sync_interval=120.0,
        workflow_knowledge_sync_changelog_limit=200,
    )


def _run_schedule(**row: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    now = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
    scheduled_mod._run_scheduled_job_once(
        SimpleNamespace(get_project_run_config=lambda _project_id: {"project_name": "Demo"}),
        {
            "id": "schedule-1",
            "project_id": "project-1",
            "account_id": "account-1",
            "workflow_id": "scrape",
            "name": "Nightly import",
            "inputs": {},
            "cron_expr": "* * * * *",
            "next_run_at": now,
            "admission_allowed": True,
            "admission_workflow_revision": 1,
            "current_workflow_revision": 1,
            **row,
        },
        _cfg(),
        now_utc=now,
        _record_scheduled_job_outcome_fn=lambda *_args, **_kwargs: None,
        _enqueue_scheduled_job_fn=lambda _bl, **kwargs: (
            captured.update(kwargs) or {"status": "enqueued", "job_id": "job-1"}
        ),
        _materialize_scheduled_job_launch_fn=lambda _workflow_id, inputs: (dict(inputs), []),
    )
    return captured["meta"]


def test_a_due_scheduled_run_names_its_schedule() -> None:
    assert _run_schedule()["submitted_by"] == {
        "id": "schedule:schedule-1",
        "kind": "schedule",
        "display_name": "Nightly import",
    }


def test_a_scheduled_run_started_now_names_who_asked() -> None:
    meta = _run_schedule(run_requested_by={"id": "alice", "kind": "person"})

    assert meta["submitted_by"] == {"id": "alice", "kind": "person"}


def test_the_scheduled_enqueue_keeps_the_schedule_starter(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        task_scheduling,
        "_enqueue_scheduled_job_unadmitted",
        lambda **kwargs: captured.update(kwargs) or {"status": "enqueued"},
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_run_title._running_registry_workflow",
        lambda _workflow_id, _account_id: None,
    )

    task_scheduling.enqueue_scheduled_job(
        task_source="schedule",
        workflow_id="scrape",
        account_id="account-1",
        extra_state={"workflow_id": "scrape"},
        meta={"scheduled_job_id": "s-1"},
    )

    assert captured["meta"]["submitted_by"]["id"] == "schedule:s-1"


# --- the API: the authenticated scope, never the caller's metadata ----------------------


@pytest.fixture
def task_service(monkeypatch):
    queued: list[dict[str, Any]] = []
    store = MagicMock()
    store.get_project.return_value = {
        "account_id": DEFAULT_ACCOUNT_ID,
        "id": PROJECT_ID,
        "name": "stock",
    }
    store.get_project_run_config.return_value = {
        "project_name": "stock",
        "project_workflows": [{"workflow_id": "stock-import", "enabled": True}],
    }
    workflow = {"settings": {"callable": {"enabled": False}}}
    registry = SimpleNamespace(db_store=SimpleNamespace(get_workflow=lambda _id: workflow))
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance", lambda: registry
    )

    def enqueue_task(**kwargs):
        queued.append(kwargs)
        return 91

    service = ProjectCommandService(
        get_logger=lambda: store,
        resolve_workflow_selection=lambda requested, project_workflows=None: (
            requested,
            "explicit_request",
        ),
        validate_explicit_workflow_override=lambda **kwargs: None,
        enqueue_task=enqueue_task,
    )
    return service, queued


def _api_task(**job_meta: Any) -> ProjectTaskEnqueueInput:
    return ProjectTaskEnqueueInput(
        project_id=PROJECT_ID,
        task_id="command-1",
        task_summary="Command workflow run",
        workflow_id="stock-import",
        task_source="command_dispatch",
        entry_path="manual_api",
        job_meta=job_meta,
    )


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"actor_id": "alice", "principal_type": "admin", "auth_source": "personal_token"},
            {"id": "alice", "kind": "person"},
        ),
        (
            {"actor_id": "billing-sync", "principal_type": "service_account"},
            {"id": "billing-sync", "kind": "service_account"},
        ),
    ],
)
def test_an_api_run_names_the_authenticated_caller_not_the_sent_name(
    task_service, fields, expected
) -> None:
    service, queued = task_service

    service.enqueue_manual_task(
        _api_task(submitted_by={"id": "someone-else", "kind": "person"}),
        trusted_internal=True,
        scope=_scope(**fields),
    )

    assert queued[0]["meta"]["submitted_by"] == expected


def test_an_internal_api_caller_never_keeps_a_sent_name(task_service) -> None:
    service, queued = task_service

    service.enqueue_manual_task(
        _api_task(submitted_by={"id": "someone-else", "kind": "person"}),
        trusted_internal=True,
        scope=_scope(actor_id="system", principal_type="system_actor"),
    )

    # Named by its source when the job is created (enqueue_job).
    assert "submitted_by" not in (queued[0]["meta"] or {})


# --- a restart is started by whoever restarted it ----------------------------------------


def _restart(job_meta: dict[str, Any], scope: ScopeContext | None) -> dict[str, Any]:
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": 15,
        "task_id": "task-15",
        "task_summary": "Import",
        "task_source": "schedule",
        "project_id": "3",
        "account_id": DEFAULT_ACCOUNT_ID,
        "meta": {"replay_snapshot": {"artifact_id": "artifact-a"}, **job_meta},
    }
    bl.get_project.return_value = {"id": 3, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
    enqueue_task = MagicMock(return_value=88)
    service = JobCommandService(
        get_logger=lambda: bl,
        build_rerun_workflow_selection=MagicMock(
            return_value=({"workflow_id": "wf-1"}, {"rerun_from_job_id": "15"})
        ),
        load_replay_snapshot=MagicMock(return_value={"workflow_id": "wf-1", "test_mode": False}),
        enqueue_task=enqueue_task,
        get_project_run_config=MagicMock(return_value={"project_name": "alpha"}),
    )
    service.restart_job(15, scope=scope)
    return enqueue_task.call_args.kwargs["meta"]


def test_a_restart_names_the_person_who_restarted() -> None:
    meta = _restart(
        {"submitted_by": {"id": "schedule:s-1", "kind": "schedule"}},
        _scope(actor_id="alice", principal_type="admin"),
    )

    assert meta["submitted_by"] == {"id": "alice", "kind": "person"}


def test_an_internal_restart_keeps_the_original_starter() -> None:
    original = {"id": "schedule:s-1", "kind": "schedule", "display_name": "Nightly"}

    assert _restart({"submitted_by": original}, None)["submitted_by"] == original
