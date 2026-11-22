"""Crash-safe connection polls on the canonical PostgreSQL schema.

A poll result is committed against its own connection, by compare-and-set,
under the connection row lock, together with its events and dispatch records.
Exactly one job starts per matching workflow and event, across crashes, replays
and concurrent workers; no new job starts once the trigger is switched off. An
event a tool_provider's background action returns reaches the workflow trigger
with the extension's own ``source_id``.

The job stack below ``enqueue_workflow_start`` is replaced by a real but small
job transaction (a row in ``fixture_poll_jobs``) that runs the
``job_created_hook`` exactly as job creation does; everything above it, and all
locking, is the production code.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.project_connection_ingress import (
    patch_project_connection_ingress,
)
from core.application.management.command_services_common import (
    _process_extension_response_envelope,
)
from core.application.management.command_services_event import EventCommandService
from core.application.workflows import provider_event_delivery as delivery
from core.infrastructure.db.platform_stores import PlatformInboundPollingStore

pytestmark = pytest.mark.integration

EXT = "fixture.items-poll"
EVENT = "fixture.item.created"
MODE = "sync_mode"
SCOPE = "sync_scope"
INTERVAL = "poll_interval_seconds"
WAIT = 15


def _extension(install_id: str) -> dict[str, Any]:
    return {
        "extension_id": EXT,
        "extension_key": EXT,
        "extension_install_id": install_id,
        "compiled_manifest": {"supported_events": [EVENT]},
        "connection_types": [{"connection_type_id": "items_app"}],
        "install_metadata": {
            "runtime_grants": {
                "mutations": {"project_connection_upsert": {"scope": "owning_extension"}}
            }
        },
        "action_definitions": [
            {
                "action_id": "poll_items",
                "handler": {
                    "mode": "extension",
                    "config": {"host_mutations": ["project_connection_upsert"]},
                },
                "contract": {
                    "serialization": {"resource": "item_sync"},
                    "background": {
                        "kind": "connection_poll",
                        "interval_field": INTERVAL,
                        "mode_field": MODE,
                        "transport_policy": "webhook_preferred",
                    },
                },
            }
        ],
    }


@dataclass
class Jobs:
    """``enqueue_task_fn``: one job row and the hook in one transaction."""

    dsn: str
    #: Called inside the job transaction after the hook (raise = crash before commit).
    before_commit: Callable[[dict[str, Any]], None] | None = None
    #: Called after the commit (raise = crash right after the commit).
    after_commit: Callable[[dict[str, Any]], None] | None = None
    created: list[str] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> str:
        hook = kwargs.pop("job_created_hook")
        workflow_id = kwargs["extra_state"]["workflow_id"]
        with psycopg.connect(self.dsn) as conn:
            job_id = conn.execute(
                "INSERT INTO fixture_poll_jobs (task_id, workflow_id) VALUES (%s, %s) "
                "RETURNING id::text",
                (kwargs["task_id"], workflow_id),
            ).fetchone()[0]
            hook(conn, job_id)
            if self.before_commit is not None:
                self.before_commit(kwargs)
        self.created.append(job_id)
        if self.after_commit is not None:
            self.after_commit(kwargs)
        return job_id


@dataclass
class World:
    dsn: str
    project_id: str
    install_id: str
    connections: list[str]
    workflows: list[str]
    triggers: list[str]
    jobs: Jobs

    @property
    def extension(self) -> dict[str, Any]:
        return _extension(self.install_id)

    def sql(self, query: str, params: tuple = ()) -> list[tuple]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return list(cur.fetchall()) if cur.description else []

    def versions(self, connection_id: str) -> tuple[int, int]:
        [(state_version, generation)] = self.sql(
            "SELECT state_version, config_generation FROM project_connections "
            "WHERE connection_id = %s",
            (connection_id,),
        )
        return int(state_version), int(generation)

    def cursor(self, connection_id: str) -> Any:
        [(ingress,)] = self.sql(
            "SELECT ingress_config FROM project_connections WHERE connection_id = %s",
            (connection_id,),
        )
        return (ingress.get("item_sync") or {}).get("cursor")

    def poll_context(self, connection_id: str) -> dict[str, Any]:
        """What the worker reads before it calls the extension."""
        state_version, generation = self.versions(connection_id)
        return {
            "connection_id": connection_id,
            "state_version": state_version,
            "config_generation": generation,
            "mode_field": MODE,
            "mode_default": "off",
        }

    def events(self) -> list[str]:
        return [
            row[0]
            for row in self.sql(
                "SELECT source_id FROM provider_event_inbox WHERE project_id = %s "
                "ORDER BY source_id",
                (self.project_id,),
            )
        ]

    def dispatches(self) -> dict[str, tuple[str, str]]:
        return {
            row[0]: (row[1], row[2])
            for row in self.sql(
                "SELECT workflow_id, status, reason FROM provider_event_dispatches "
                "WHERE project_id = %s",
                (self.project_id,),
            )
        }

    def job_count(self, workflow_id: str | None = None) -> int:
        workflows = [workflow_id] if workflow_id else self.workflows
        [(count,)] = self.sql(
            "SELECT count(*) FROM fixture_poll_jobs WHERE workflow_id = ANY(%s)", (workflows,)
        )
        return int(count)

    def deliveries(self) -> list[tuple[str, str]]:
        return [
            (row[0], str(row[1]))
            for row in self.sql(
                "SELECT status, job_id FROM provider_webhook_deliveries WHERE provider_key = %s "
                "AND delivery_key LIKE %s",
                (EXT, f"{self.project_id}|%"),
            )
        ]

    def commit_poll(
        self, context: dict[str, Any], *, cursor: Any, messages: list[str]
    ) -> dict[str, Any]:
        """The worker's result handling for one ``poll_items`` answer."""
        connection_id = context["connection_id"]
        invocation = {
            "status": "succeeded",
            "operation_type": "action",
            "operation_id": "poll_items",
            "response": {
                "ok": True,
                "result": {"processed_updates": len(messages)},
                "events": [
                    {
                        "event_type": EVENT,
                        "summary": f"Item {message}",
                        "source_id": f"fixture:{connection_id}:{message}",
                        "payload": {"connection_id": connection_id, "message_id": message},
                    }
                    for message in messages
                ],
                "host_mutations": [
                    {
                        "kind": "project_connection_upsert",
                        "payload": {
                            "connection_id": connection_id,
                            "ingress_config": {MODE: "polling", "item_sync": {"cursor": cursor}},
                            "expected_state_version": context["state_version"],
                            "expected_config_generation": context["config_generation"],
                        },
                    }
                ],
            },
        }
        with PlatformInboundPollingStore.dedicated(self.dsn) as store:
            service = EventCommandService(
                get_logger=lambda: store,
                resolve_domain_event_by_ref=lambda *_a, **_k: None,
                to_json_obj=lambda value: value if isinstance(value, dict) else {},
                serialize_domain_event_datetime=lambda value: str(value or ""),
                enqueue_task=self.jobs,
            )
            _process_extension_response_envelope(
                invocation,
                self.extension,
                EXT,
                scope=None,
                audit={
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "project_id": self.project_id,
                    "extension_install_id": self.install_id,
                    "connection_id": connection_id,
                },
                bl=store,
                get_logger=lambda: store,
                event_command_service_factory=lambda: service,
                poll_context=context,
            )
        return invocation

    def patch_ingress(self, connection_id: str, **values: Any) -> None:
        """The trigger settings save (``project_connection_ingress_patch``)."""
        with PlatformInboundPollingStore.dedicated(self.dsn) as store:
            patch_project_connection_ingress(
                store,
                project_id=self.project_id,
                extension_id=EXT,
                config={"fields": [MODE, SCOPE, INTERVAL]},
                input_payload=values,
                extension_install_id=self.install_id,
                account_id=DEFAULT_ACCOUNT_ID,
                connection_id=connection_id,
                generation_exempt_fields={INTERVAL},
            )

    def sweep(self) -> list[dict[str, Any]]:
        with PlatformInboundPollingStore.dedicated(self.dsn) as store:
            return delivery.sweep_pending_provider_event_dispatches(
                store, enqueue_task_fn=self.jobs, min_age_seconds=0
            )


@pytest.fixture
def make_world(disposable_db, monkeypatch):
    from core.application.registry import workflow_contract_service, workflow_secret_defaults
    from core.application.workflows import workflow_entrypoint_repository
    from core.infrastructure.db import db_pool

    monkeypatch.setattr(db_pool, "_runtime_env", {"FS_DB_POOL_ENABLED": "0"})
    monkeypatch.setattr(
        workflow_entrypoint_repository,
        "resolve_project_run_config_for_workflow_project",
        lambda _bl, _workflow_id, project_id: (project_id, {"project_name": "Poll fixture"}),
    )

    class _Interface:
        def validate_inputs_for_workflow_id(self, _workflow_id, inputs, *, channel=""):
            return SimpleNamespace(ok=True, errors=[], inputs=inputs, interface={})

    monkeypatch.setattr(workflow_contract_service, "WorkflowInterfaceService", _Interface)
    monkeypatch.setattr(
        workflow_secret_defaults, "workflow_secret_launch_snapshot", lambda *_a, **_k: []
    )
    delivery.FAULT_POINTS.clear()

    def build(*, connections: int = 1, workflows: int = 1) -> World:
        project_id = str(uuid4())
        install_id = str(uuid4())
        connection_ids = [f"conn_{uuid4().hex[:12]}" for _ in range(connections)]
        workflow_ids = [f"poll-wf-{uuid4().hex[:10]}" for _ in range(workflows)]
        trigger_ids: list[str] = []
        with psycopg.connect(disposable_db.dsn, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fixture_poll_jobs (id uuid DEFAULT "
                "gen_random_uuid() PRIMARY KEY, task_id text NOT NULL, workflow_id text NOT NULL)"
            )
            conn.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_id, f"Poll fixture {project_id}", DEFAULT_ACCOUNT_ID),
            )
            # One install per extension and account; every world shares it and
            # keeps to its own project.
            conn.execute(
                "INSERT INTO extension_installs (extension_id, install_id, account_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (EXT, install_id, DEFAULT_ACCOUNT_ID),
            )
            [(install_id,)] = conn.execute(
                "SELECT install_id FROM extension_installs WHERE extension_id = %s "
                "AND account_id = %s",
                (EXT, DEFAULT_ACCOUNT_ID),
            ).fetchall()
            for connection_id in connection_ids:
                conn.execute(
                    "INSERT INTO project_connections (connection_id, project_id, extension_id, "
                    "account_id, extension_install_id, connection_kind, connection_type, "
                    "display_name, ingress_config) VALUES (%s, %s, %s, %s, %s, 'extension', "
                    "'items_app', %s, %s::jsonb)",
                    (
                        connection_id,
                        project_id,
                        EXT,
                        DEFAULT_ACCOUNT_ID,
                        install_id,
                        connection_id,
                        json.dumps({MODE: "polling", SCOPE: "a"}),
                    ),
                )
            for workflow_id in workflow_ids:
                conn.execute(
                    "INSERT INTO registry_workflows (workflow_key, display_name, account_id, "
                    "enabled) VALUES (%s, 'Poll fixture', %s, true)",
                    (workflow_id, DEFAULT_ACCOUNT_ID),
                )
                conn.execute(
                    "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
                    (project_id, workflow_id),
                )
                [(trigger_id,)] = conn.execute(
                    "INSERT INTO workflow_provider_triggers (workflow_id, project_id, "
                    "provider_key, trigger_type_id, display_name, event_name, "
                    "trigger_config_jsonb, account_id) VALUES (%s, %s, %s, 'message_received', "
                    "'New item', %s, %s::jsonb, %s) RETURNING trigger_id",
                    (
                        workflow_id,
                        project_id,
                        EXT,
                        EVENT,
                        json.dumps({"connection_id": connection_ids[0]}),
                        DEFAULT_ACCOUNT_ID,
                    ),
                ).fetchall()
                trigger_ids.append(trigger_id)
        return World(
            dsn=disposable_db.dsn,
            project_id=project_id,
            install_id=install_id,
            connections=connection_ids,
            workflows=workflow_ids,
            triggers=trigger_ids,
            jobs=Jobs(disposable_db.dsn),
        )

    yield build
    delivery.FAULT_POINTS.clear()


def _pause_at(name: str) -> tuple[threading.Event, threading.Event]:
    """Install a fault point that parks its thread until released (once)."""
    reached = threading.Event()
    release = threading.Event()

    def _pause() -> None:
        delivery.FAULT_POINTS.pop(name, None)
        reached.set()
        assert release.wait(WAIT), "fault point was never released"

    delivery.FAULT_POINTS[name] = _pause
    return reached, release


def _thread(target: Callable[[], Any]) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            target()
        except BaseException as exc:  # surfaced by the test
            errors.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, errors


# --------------------------------------------------------------------------- state


def test_two_connections_of_one_install_keep_independent_cursors(make_world):
    world = make_world(connections=2)
    first, second = world.connections
    for connection_id, cursor in (
        (first, "a1"),
        (second, "b1"),
        (second, "b2"),
        (first, "a2"),
        (first, "a3"),
        (second, "b3"),
    ):
        outcome = world.commit_poll(world.poll_context(connection_id), cursor=cursor, messages=[])
        assert outcome["connection_poll"]["verdict"] == "accept"
    assert world.cursor(first) == "a3"
    assert world.cursor(second) == "b3"
    assert world.versions(first)[0] == 3
    assert world.versions(second)[0] == 3


def test_two_workers_polling_one_connection_one_state_write_wins(make_world):
    world = make_world()
    [connection_id] = world.connections
    both = world.poll_context(connection_id)
    winner = world.commit_poll(both, cursor="new", messages=["m1"])
    stale = world.commit_poll(dict(both), cursor="old", messages=["m1", "m0"])
    assert winner["connection_poll"] == {"verdict": "accept", "reason": "", "state_saved": True}
    assert stale["connection_poll"]["verdict"] == "stale_state"
    assert stale["connection_poll"]["state_saved"] is False
    # The cursor never goes backwards; the stale worker's events are kept, once.
    assert world.cursor(connection_id) == "new"
    assert world.versions(connection_id)[0] == 1
    assert world.events() == sorted([f"fixture:{connection_id}:m0", f"fixture:{connection_id}:m1"])
    assert world.job_count() == 2


def test_switch_off_while_the_poll_waits_drops_events_jobs_and_state(make_world):
    world = make_world()
    [connection_id] = world.connections
    context = world.poll_context(connection_id)
    world.patch_ingress(connection_id, **{MODE: "off"})
    outcome = world.commit_poll(context, cursor="c1", messages=["m1"])
    assert outcome["connection_poll"]["verdict"] == "stale_configuration"
    assert world.events() == []
    assert world.job_count() == 0
    assert world.cursor(connection_id) is None
    assert world.versions(connection_id) == (0, 2)


def test_scope_change_while_the_poll_waits_rejects_then_new_baseline(make_world):
    world = make_world()
    [connection_id] = world.connections
    old = world.poll_context(connection_id)
    world.patch_ingress(connection_id, **{SCOPE: "b"})
    rejected = world.commit_poll(old, cursor="a-1", messages=["m1"])
    assert rejected["connection_poll"]["reason"] == "config_generation_changed"
    assert world.events() == []
    fresh = world.poll_context(connection_id)
    assert fresh["config_generation"] == old["config_generation"] + 1
    accepted = world.commit_poll(fresh, cursor="b-1", messages=[])
    assert accepted["connection_poll"]["verdict"] == "accept"
    assert world.cursor(connection_id) == "b-1"


def test_interval_change_keeps_the_generation(make_world):
    world = make_world()
    [connection_id] = world.connections
    context = world.poll_context(connection_id)
    world.patch_ingress(connection_id, **{INTERVAL: 120})
    outcome = world.commit_poll(context, cursor="c1", messages=[])
    assert outcome["connection_poll"]["verdict"] == "accept"


def test_disable_and_settings_edit_move_the_generation(make_world):
    world = make_world()
    [connection_id] = world.connections
    world.sql(
        "UPDATE project_connections SET connection_config = connection_config || "
        '\'{"oauth": {"lifecycle_status": "connected"}}\'::jsonb WHERE connection_id = %s',
        (connection_id,),
    )
    assert world.versions(connection_id)[1] == 1  # a token refresh rewrites only oauth
    world.sql(
        "UPDATE project_connections SET connection_config = connection_config || "
        '\'{"scope": "a"}\'::jsonb WHERE connection_id = %s',
        (connection_id,),
    )
    assert world.versions(connection_id)[1] == 2
    world.sql(
        "UPDATE project_connections SET enabled = false WHERE connection_id = %s",
        (connection_id,),
    )
    assert world.versions(connection_id)[1] == 3


def test_race_switch_off_waits_for_the_locked_poll_transaction(make_world):
    world = make_world()
    [connection_id] = world.connections
    context = world.poll_context(connection_id)
    reached, release = _pause_at("poll_after_lock")
    poll, poll_errors = _thread(lambda: world.commit_poll(context, cursor="c1", messages=["m1"]))
    assert reached.wait(WAIT)
    switched = threading.Event()
    switch, switch_errors = _thread(
        lambda: (world.patch_ingress(connection_id, **{MODE: "off"}), switched.set())
    )
    # The switch-off needs the row the poll transaction holds.
    assert not switched.wait(1.0)
    release.set()
    poll.join(WAIT)
    switch.join(WAIT)
    assert not poll_errors and not switch_errors
    # The result committed completely before the switch-off.
    assert world.events() == [f"fixture:{connection_id}:m1"]
    assert world.cursor(connection_id) == "c1"
    [(status, reason)] = [value for _, value in world.dispatches().items()]
    # Its job either committed before the switch-off, or was refused after it.
    if status == "accepted":
        assert world.job_count() == 1
    else:
        assert (status, reason) == ("failed", "config_generation_changed")
        assert world.job_count() == 0


def test_race_poll_after_a_committed_switch_off_is_rejected(make_world):
    world = make_world()
    [connection_id] = world.connections
    context = world.poll_context(connection_id)
    reached, release = _pause_at("poll_after_lock")
    world.patch_ingress(connection_id, **{MODE: "off"})
    poll, errors = _thread(lambda: world.commit_poll(context, cursor="c1", messages=["m1"]))
    assert reached.wait(WAIT)
    release.set()
    poll.join(WAIT)
    assert not errors
    assert world.events() == []
    assert world.job_count() == 0
    assert world.cursor(connection_id) is None


# --------------------------------------------------------------------------- delivery


def test_event_reaches_the_trigger_with_the_extension_source_id(make_world):
    world = make_world()
    [connection_id] = world.connections
    world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["item-1"])
    source_id = f"fixture:{connection_id}:item-1"
    assert world.events() == [source_id]
    [(task_id,)] = world.sql(
        "SELECT task_id FROM fixture_poll_jobs WHERE workflow_id = %s", (world.workflows[0],)
    )
    assert task_id == f"provider-trigger:{EXT}:{source_id}"
    assert [status for status, _ in world.deliveries()] == ["accepted"]


def test_1_crash_inside_the_poll_transaction_rolls_everything_back(make_world):
    world = make_world()
    [connection_id] = world.connections
    context = world.poll_context(connection_id)

    def _crash() -> None:
        raise RuntimeError("worker died inside the poll transaction")

    delivery.FAULT_POINTS["poll_before_commit"] = _crash
    with pytest.raises(RuntimeError, match="worker died"):
        world.commit_poll(context, cursor="c1", messages=["m1"])
    delivery.FAULT_POINTS.clear()
    assert world.events() == []
    assert world.dispatches() == {}
    assert world.versions(connection_id)[0] == 0
    assert world.cursor(connection_id) is None
    # The next poll re-reads the same page.
    world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1"])
    assert world.events() == [f"fixture:{connection_id}:m1"]
    assert world.job_count() == 1


def test_2_crash_inside_the_job_transaction_then_retry(make_world):
    world = make_world()
    [connection_id] = world.connections
    crashes = []

    def _crash(_kwargs: dict[str, Any]) -> None:
        crashes.append(True)
        raise RuntimeError("worker died inside the job transaction")

    world.jobs.before_commit = _crash
    world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1"])
    assert crashes == [True]
    assert world.job_count() == 0
    assert [status for status, _ in world.dispatches().values()] == ["pending"]
    world.jobs.before_commit = None
    world.sweep()
    assert world.job_count() == 1
    assert [status for status, _ in world.dispatches().values()] == ["accepted"]


def test_3_crash_right_after_the_job_commit_sweep_starts_no_second_job(make_world):
    world = make_world()
    [connection_id] = world.connections

    def _crash(_kwargs: dict[str, Any]) -> None:
        raise RuntimeError("worker died right after the job commit")

    world.jobs.after_commit = _crash
    world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1"])
    world.jobs.after_commit = None
    assert world.job_count() == 1
    assert [status for status, _ in world.dispatches().values()] == ["accepted"]
    [(status, job_id)] = world.deliveries()
    assert status == "accepted" and job_id == world.jobs.created[0]
    world.sweep()
    assert world.job_count() == 1


def test_4_the_same_delta_page_delivered_twice(make_world):
    world = make_world()
    [connection_id] = world.connections
    world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1", "m2"])
    world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1", "m2"])
    assert len(world.events()) == 2
    assert world.job_count() == 2
    assert world.versions(connection_id)[0] == 2


def test_5_two_workers_polling_the_same_connection_at_once(make_world):
    world = make_world()
    [connection_id] = world.connections
    context = world.poll_context(connection_id)
    barrier = threading.Barrier(2)
    outcomes: list[dict[str, Any]] = []

    def _worker() -> None:
        barrier.wait(WAIT)
        outcomes.append(world.commit_poll(dict(context), cursor="c1", messages=["m1", "m2"]))

    threads = [_thread(_worker) for _ in range(2)]
    for thread, errors in threads:
        thread.join(WAIT)
        assert not errors
    assert sorted(item["connection_poll"]["verdict"] for item in outcomes) == [
        "accept",
        "stale_state",
    ]
    assert world.versions(connection_id)[0] == 1
    assert len(world.events()) == 2
    assert world.job_count() == 2


def test_6_two_workflows_on_one_event_retry_after_a_crash(make_world):
    world = make_world(workflows=2)
    [connection_id] = world.connections
    first_done: list[str] = []

    def _crash_after_first(kwargs: dict[str, Any]) -> None:
        if not first_done:
            first_done.append(kwargs["extra_state"]["workflow_id"])
            raise RuntimeError("worker died after the first workflow's job")

    world.jobs.after_commit = _crash_after_first
    world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1"])
    world.jobs.after_commit = None
    world.sweep()
    world.sweep()
    for workflow_id in world.workflows:
        assert world.job_count(workflow_id) == 1
    assert sorted(status for status, _ in world.dispatches().values()) == [
        "accepted",
        "accepted",
    ]


def test_7_pending_record_after_the_trigger_is_switched_off_fails(make_world):
    world = make_world()
    [connection_id] = world.connections

    def _crash(_kwargs: dict[str, Any]) -> None:
        raise RuntimeError("worker died inside the job transaction")

    world.jobs.before_commit = _crash
    world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1"])
    world.jobs.before_commit = None
    world.sql(
        "UPDATE workflow_provider_triggers SET enabled = false WHERE trigger_id = %s",
        (world.triggers[0],),
    )
    world.sweep()
    assert world.job_count() == 0
    assert list(world.dispatches().values()) == [("failed", "trigger_inactive")]


def test_8_race_switch_off_before_the_job_takes_the_connection_lock(make_world):
    world = make_world()
    [connection_id] = world.connections
    reached, release = _pause_at("job_before_connection_lock")
    poll, errors = _thread(
        lambda: world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1"])
    )
    assert reached.wait(WAIT)
    # The job transaction has not locked the connection yet: the switch-off commits.
    world.patch_ingress(connection_id, **{MODE: "off"})
    release.set()
    poll.join(WAIT)
    assert not errors
    assert world.job_count() == 0
    assert list(world.dispatches().values()) == [("failed", "config_generation_changed")]


def test_8_race_trigger_disabled_before_the_job_checks_it(make_world):
    world = make_world()
    [connection_id] = world.connections
    reached, release = _pause_at("job_before_connection_lock")
    poll, errors = _thread(
        lambda: world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1"])
    )
    assert reached.wait(WAIT)
    world.sql(
        "UPDATE workflow_provider_triggers SET enabled = false WHERE trigger_id = %s",
        (world.triggers[0],),
    )
    release.set()
    poll.join(WAIT)
    assert not errors
    assert world.job_count() == 0
    assert list(world.dispatches().values()) == [("failed", "trigger_inactive")]


def test_8_job_committed_before_the_switch_off_stays_queued(make_world):
    world = make_world()
    [connection_id] = world.connections
    world.commit_poll(world.poll_context(connection_id), cursor="c1", messages=["m1"])
    world.patch_ingress(connection_id, **{MODE: "off"})
    assert world.job_count() == 1
    assert [status for status, _ in world.dispatches().values()] == ["accepted"]
    world.sweep()
    assert world.job_count() == 1
