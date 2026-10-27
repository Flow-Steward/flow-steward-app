"""Workflow trigger retirement on the disposable canonical PostgreSQL database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.application.workflows.workflow_entrypoint_repository import load_workflow_entrypoint
from core.application.workflows.workflow_provider_trigger_repository import (
    list_enabled_provider_triggers_for_provider,
)
from core.infrastructure.db.platform_stores import (
    PlatformControlScheduleStore,
    PlatformProjectStore,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def trigger_owner(disposable_db, monkeypatch):
    from core.infrastructure.db import db_pool

    monkeypatch.setattr(db_pool, "_runtime_env", {"FS_DB_POOL_ENABLED": "0"})
    workflow = f"trigger-retirement-{uuid4().hex}"
    project = str(uuid4())
    key = uuid4().hex
    schedule = str(uuid4())
    provider = f"fixture-{uuid4().hex}"
    due = datetime.now(UTC) - timedelta(minutes=2)
    with psycopg.connect(disposable_db.dsn) as conn:
        conn.execute(
            "INSERT INTO projects(id, name, account_id) VALUES (%s, 'Retirement fixture', %s)",
            (project, DEFAULT_ACCOUNT_ID),
        )
        conn.execute(
            "INSERT INTO registry_workflows(workflow_key, display_name, account_id, enabled) VALUES (%s, 'Retirement fixture', %s, true)",
            (workflow, DEFAULT_ACCOUNT_ID),
        )
        conn.execute(
            "INSERT INTO project_workflows(project_id, workflow_id) VALUES (%s, %s)",
            (project, workflow),
        )
        conn.execute(
            "INSERT INTO workflow_entrypoints(workflow_id, project_id, entry_key, secret_ref) VALUES (%s,%s,%s,%s)",
            (workflow, project, key, key),
        )
        conn.execute(
            "INSERT INTO project_secrets(project_id, secret_key, secret_value_ciphertext) VALUES (%s,%s,'synthetic-ciphertext')",
            (project, key),
        )
        conn.execute(
            "INSERT INTO workflow_provider_triggers(workflow_id, project_id, provider_key, trigger_type_id) VALUES (%s,%s,%s,'event')",
            (workflow, project, provider),
        )
        conn.execute(
            "INSERT INTO scheduled_jobs(id, project_id, account_id, workflow_id, cron_expr, next_run_at) VALUES (%s,%s,%s,%s,'* * * * *',%s)",
            (schedule, project, DEFAULT_ACCOUNT_ID, workflow, due),
        )
    store = PlatformProjectStore(disposable_db.dsn)
    registry = RegistryDBStore(disposable_db.dsn)
    schedules = PlatformControlScheduleStore(disposable_db.dsn)
    monkeypatch.setattr(registry, "_trigger_public_contract_projection_sync", lambda: None)
    service = TrashLifecycleService(
        get_logger=lambda: store,
        get_audit_store=lambda: SimpleNamespace(log_action=lambda **_: None),
    )
    monkeypatch.setattr(service, "_reload_workflow_registry", lambda: None)
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID, actor_id="retirement-test", principal_type="admin"
    )
    fixture = SimpleNamespace(
        dsn=disposable_db.dsn,
        workflow=workflow,
        project=project,
        key=key,
        schedule=schedule,
        provider=provider,
        due=due,
        store=store,
        registry=registry,
        schedules=schedules,
        service=service,
        scope=scope,
    )
    try:
        yield fixture
    finally:
        for owner in (store, registry, schedules):
            connection = getattr(owner, "_direct_conn", None) or getattr(owner, "_conn", None)
            if connection is not None:
                connection.close()
        with psycopg.connect(disposable_db.dsn) as conn:
            conn.execute(
                "DELETE FROM registry_changelog WHERE entity_type='workflow' AND entity_id=%s",
                (workflow,),
            )
            conn.execute("DELETE FROM jobs WHERE project_id=%s", (project,))
            conn.execute("DELETE FROM projects WHERE id=%s", (project,))
            conn.execute("DELETE FROM registry_workflows WHERE workflow_id=%s", (workflow,))


def _delete(owner, path):
    if path == "registry":
        owner.registry.delete_workflow(owner.workflow)
    else:
        owner.service.trash("registry_workflow", owner.workflow, scope=owner.scope)


@pytest.mark.parametrize("path", ["registry", "trash"])
def test_delete_retires_all_owned_triggers(trigger_owner, path):
    o = trigger_owner
    _delete(o, path)
    with psycopg.connect(o.dsn) as conn:
        assert conn.execute(
            "SELECT enabled FROM workflow_entrypoints WHERE entry_key=%s", (o.key,)
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT enabled FROM workflow_provider_triggers WHERE workflow_id=%s", (o.workflow,)
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT enabled,status FROM scheduled_jobs WHERE id=%s", (o.schedule,)
        ).fetchone() == (False, "paused")
        assert conn.execute(
            "SELECT active FROM project_secrets WHERE project_id=%s AND secret_key=%s",
            (o.project, o.key),
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT enabled,deleted_at IS NOT NULL,revision FROM registry_workflows WHERE workflow_id=%s",
            (o.workflow,),
        ).fetchone() == (True, True, 2)


def test_deleted_owner_entrypoint_lookup_hides_key(trigger_owner):
    o = trigger_owner
    assert load_workflow_entrypoint(o.store, o.key)["enabled"] is True
    _delete(o, "registry")
    assert load_workflow_entrypoint(o.store, o.key) == {}


def test_deleted_owner_provider_candidate_is_excluded(trigger_owner):
    o = trigger_owner
    assert len(list_enabled_provider_triggers_for_provider(o.store, provider_key=o.provider)) == 1
    _delete(o, "registry")
    assert list_enabled_provider_triggers_for_provider(o.store, provider_key=o.provider) == []


@pytest.mark.parametrize("path", ["registry", "trash"])
def test_deleted_owner_due_and_stale_claim_do_not_advance(trigger_owner, path):
    o = trigger_owner
    assert any(str(row["id"]) == o.schedule for row in o.schedules.list_due_scheduled_jobs())
    with psycopg.connect(o.dsn) as conn:
        conn.execute(
            "INSERT INTO scheduled_job_runs(scheduled_job_id,project_id,workflow_id,status,scheduled_for,detail_jsonb) VALUES (%s,%s,%s,'skipped',%s,jsonb_build_object('marker','retained'))",
            (o.schedule, o.project, o.workflow, o.due),
        )
        history = conn.execute(
            "SELECT to_jsonb(r) FROM scheduled_job_runs r WHERE scheduled_job_id=%s", (o.schedule,)
        ).fetchall()
    _delete(o, path)
    assert not any(str(row["id"]) == o.schedule for row in o.schedules.list_due_scheduled_jobs())
    assert (
        o.schedules.claim_due_scheduled_job(
            project_id=o.project,
            scheduled_job_id=o.schedule,
            expected_next_run_at=o.due,
            next_run_at=o.due + timedelta(minutes=5),
        )
        is None
    )
    with psycopg.connect(o.dsn) as conn:
        assert (
            conn.execute(
                "SELECT to_jsonb(r) FROM scheduled_job_runs r WHERE scheduled_job_id=%s",
                (o.schedule,),
            ).fetchall()
            == history
        )
        assert conn.execute(
            "SELECT next_run_at FROM scheduled_jobs WHERE id=%s", (o.schedule,)
        ).fetchone() == (o.due,)


def _state(owner):
    with psycopg.connect(owner.dsn) as conn:
        return {
            table: conn.execute(
                f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {table} t WHERE {column}=%s",
                (identity,),
            ).fetchone()[0]
            for table, column, identity in [
                ("registry_workflows", "workflow_id", owner.workflow),
                ("workflow_entrypoints", "workflow_id", owner.workflow),
                ("workflow_provider_triggers", "workflow_id", owner.workflow),
                ("scheduled_jobs", "workflow_id", owner.workflow),
                ("project_secrets", "project_id", owner.project),
                ("registry_changelog", "entity_id", owner.workflow),
            ]
        }


@pytest.mark.parametrize("path", ["registry", "trash"])
@pytest.mark.parametrize(
    "stage",
    [
        "UPDATE project_secrets",
        "UPDATE registry_workflows",
        "UPDATE workflow_entrypoints",
        "UPDATE workflow_provider_triggers",
        "UPDATE scheduled_jobs",
        "INSERT INTO registry_changelog",
    ],
)
def test_every_retirement_stage_rolls_back_whole_owner(trigger_owner, monkeypatch, path, stage):
    from core.application.workflows import workflow_trigger_lifecycle as lifecycle

    o = trigger_owner
    before = _state(o)
    actual = lifecycle.retire_workflow

    class FailAfter:
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, query, params=None):
            self.cursor.execute(query, params)
            if stage in " ".join(str(query).split()):
                raise RuntimeError("injected lifecycle stage failure")

    monkeypatch.setattr(
        lifecycle, "retire_workflow", lambda cursor, **kwargs: actual(FailAfter(cursor), **kwargs)
    )
    with pytest.raises(RuntimeError, match="injected lifecycle stage failure"):
        _delete(o, path)
    assert _state(o) == before


@pytest.mark.parametrize("path", ["registry", "trash"])
@pytest.mark.parametrize("was_enabled", [False, True])
@pytest.mark.parametrize("restore_path", ["direct", "plan"])
def test_restore_preserves_workflow_enabled_without_resurrecting_triggers(
    trigger_owner, path, was_enabled, restore_path
):
    o = trigger_owner
    with psycopg.connect(o.dsn) as conn:
        conn.execute(
            "UPDATE registry_workflows SET enabled=%s WHERE workflow_id=%s",
            (was_enabled, o.workflow),
        )
    _delete(o, path)
    if restore_path == "direct":
        o.service.restore("registry_workflow", o.workflow, scope=o.scope)
    else:
        o.service.execute_plan(
            "registry_workflow", o.workflow, action_type="restore", scope=o.scope
        )
    with psycopg.connect(o.dsn) as conn:
        assert conn.execute(
            "SELECT enabled,deleted_at FROM registry_workflows WHERE workflow_id=%s", (o.workflow,)
        ).fetchone() == (was_enabled, None)
        assert conn.execute(
            "SELECT enabled FROM workflow_entrypoints WHERE entry_key=%s", (o.key,)
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT enabled,status FROM scheduled_jobs WHERE id=%s", (o.schedule,)
        ).fetchone() == (False, "paused")
        assert conn.execute(
            "SELECT active FROM project_secrets WHERE project_id=%s AND secret_key=%s",
            (o.project, o.key),
        ).fetchone() == (False,)


@pytest.mark.parametrize(
    "consumer",
    [
        "entrypoint",
        "callback_metadata_old",
        "unknown_metadata",
        "malformed_auth_ref",
        "callback_ref",
        "http_definition",
        "malformed_http",
    ],
)
def test_shared_and_foreign_current_authority_is_retained(trigger_owner, consumer):
    o = trigger_owner
    other = f"other-{uuid4().hex}"
    with psycopg.connect(o.dsn) as conn:
        conn.execute(
            "INSERT INTO registry_workflows(workflow_key,display_name,account_id) VALUES (%s,'Other',%s)",
            (other, DEFAULT_ACCOUNT_ID),
        )
        if consumer == "entrypoint":
            conn.execute(
                "INSERT INTO project_workflows(project_id,workflow_id) VALUES (%s,%s)",
                (o.project, other),
            )
            conn.execute(
                "INSERT INTO workflow_entrypoints(workflow_id,project_id,entry_key,secret_ref,enabled) VALUES (%s,%s,%s,%s,false)",
                (other, o.project, uuid4().hex, o.key),
            )
        elif consumer == "callback_metadata_old":
            conn.execute(
                'INSERT INTO project_secrets(project_id,secret_key,active,metadata_jsonb) VALUES (%s,%s,false,\'{"source":"extension_callback_registration"}\')',
                (o.project, o.key),
            )
        elif consumer == "malformed_auth_ref":
            conn.execute(
                "UPDATE workflow_entrypoints SET auth_config_jsonb=jsonb_build_object('secret_ref','[]'::jsonb) WHERE entry_key=%s",
                (o.key,),
            )
        elif consumer == "callback_ref":
            from psycopg.types.json import Jsonb

            conn.execute(
                "INSERT INTO extension_callback_registrations(extension_id,callback_id,callback_token_hash,project_id,auth_config_jsonb) VALUES ('fixture','callback',%s,%s,%s)",
                (uuid4().hex, o.project, Jsonb({"capability_token_ref": o.key})),
            )
        elif consumer == "unknown_metadata":
            conn.execute(
                "UPDATE project_secrets SET metadata_jsonb='[]'::jsonb WHERE project_id=%s",
                (o.project,),
            )
        else:
            from psycopg.types.json import Jsonb

            config = (
                {"http_request": {"auth_mode": "bearer", "auth_ref": "secret:" + o.key}}
                if consumer == "http_definition"
                else {"http_request": []}
            )
            conn.execute(
                "INSERT INTO registry_workflow_steps(owner_workflow_id,step_id,step_kind,override) VALUES (%s,'request','http_request',%s)",
                (other, Jsonb(config)),
            )
    try:
        _delete(o, "registry")
        with psycopg.connect(o.dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM project_secrets WHERE project_id=%s AND secret_key=%s AND active",
                (o.project, o.key),
            ).fetchone() == (1,)
        assert load_workflow_entrypoint(o.store, o.key) == {}
    finally:
        with psycopg.connect(o.dsn) as conn:
            conn.execute("DELETE FROM registry_workflows WHERE workflow_id=%s", (other,))


def test_all_retained_versions_nowait_conflict_unwinds_prior_write(trigger_owner):
    from core.application.workflows.project_credential_versions import (
        WorkflowCredentialVersionConflict,
        guard_project_credential_versions,
    )

    o = trigger_owner
    inactive = str(uuid4())
    with psycopg.connect(o.dsn) as conn:
        conn.execute(
            "INSERT INTO project_secrets(id,project_id,secret_key,active) VALUES (%s,%s,%s,false)",
            (inactive, o.project, o.key),
        )
    with psycopg.connect(o.dsn) as holder:
        holder.execute("SELECT id FROM project_secrets WHERE id=%s FOR UPDATE", (inactive,))
        with (
            psycopg.connect(o.dsn) as contender,
            pytest.raises(WorkflowCredentialVersionConflict),
            contender.transaction(),
        ):
            contender.execute(
                "UPDATE registry_workflows SET display_name='must roll back' WHERE workflow_id=%s",
                (o.workflow,),
            )
            with contender.cursor() as cursor:
                guard_project_credential_versions(cursor, project_id=o.project, secret_key=o.key)
        with psycopg.connect(o.dsn) as observer:
            assert observer.execute(
                "SELECT display_name FROM registry_workflows WHERE workflow_id=%s", (o.workflow,)
            ).fetchone() == ("Retirement fixture",)
    _delete(o, "registry")
    with psycopg.connect(o.dsn) as observer:
        assert observer.execute(
            "SELECT count(*) FROM project_secrets WHERE project_id=%s AND active", (o.project,)
        ).fetchone() == (0,)


def test_trashed_exact_webhook_cleanup_keeps_other_rows(trigger_owner):
    from core.application.workflows.workflow_webhook_creation_service import (
        create_workflow_webhook_mutation_service,
    )

    o = trigger_owner
    _delete(o, "registry")
    assert create_workflow_webhook_mutation_service().delete(bl=o.store, entry_key=o.key) is True
    with psycopg.connect(o.dsn) as observer:
        assert observer.execute(
            "SELECT count(*) FROM workflow_entrypoints WHERE entry_key=%s", (o.key,)
        ).fetchone() == (0,)
        assert observer.execute(
            "SELECT count(*) FROM registry_workflows WHERE workflow_id=%s", (o.workflow,)
        ).fetchone() == (1,)
        assert observer.execute(
            "SELECT count(*) FROM workflow_provider_triggers WHERE workflow_id=%s", (o.workflow,)
        ).fetchone() == (1,)


def _create_job(owner, connection):
    from core.application.jobs.job_submission_repository import JobSubmissionRepository

    return JobSubmissionRepository(owner.store, connection).create(
        encrypted_envelope="synthetic-envelope",
        outbox_status="pending",
        project_id=owner.project,
        task_id=uuid4().hex,
        task_summary="retirement race",
        task_source="step_test_run",
        selected_workflow_key=owner.workflow,
        status="pending",
    )


def _wait_for_lock(dsn, pid):
    import time

    deadline = time.monotonic() + 10
    with psycopg.connect(dsn, autocommit=True) as observer:
        while time.monotonic() < deadline:
            row = observer.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,)
            ).fetchone()
            if row and row[0] == "Lock":
                return
            time.sleep(0.01)
    raise AssertionError("Contender never reached the actual PostgreSQL lock wait")


def _counts(owner):
    with psycopg.connect(owner.dsn) as conn:
        return tuple(
            conn.execute(query, (owner.project,)).fetchone()[0]
            for query in [
                "SELECT count(*) FROM jobs WHERE project_id=%s",
                "SELECT count(*) FROM job_dispatch_outbox WHERE job_id IN (SELECT id FROM jobs WHERE project_id=%s)",
                "SELECT count(*) FROM workflow_definition_snapshots WHERE id IN (SELECT workflow_definition_snapshot_id FROM jobs WHERE project_id=%s)",
            ]
        )


@pytest.mark.parametrize("path", ["registry", "trash"])
def test_admission_winner_blocks_delete_after_row_lock_is_released(trigger_owner, path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from core.application.management.errors import ServiceConflictError
    from core.infrastructure.db import psycopg_runtime

    o = trigger_owner
    started = threading.Event()
    pid = []
    writer = psycopg_runtime.connect(o.dsn)
    writer.autocommit = False
    try:
        job_id = _create_job(o, writer)

        def delete():
            store = o.registry if path == "registry" else o.store
            with store.conn.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pid.append(cursor.fetchone()[0])
            started.set()
            _delete(o, path)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(delete)
            try:
                assert started.wait(10)
                _wait_for_lock(o.dsn, pid[0])
                writer.commit()
                with pytest.raises(ServiceConflictError) as rejected:
                    future.result(timeout=15)
                assert rejected.value.payload["code"] == "workflow_has_nonterminal_jobs"
            finally:
                writer.rollback()
        assert _counts(o) == (1, 1, 1)
        with psycopg.connect(o.dsn) as conn:
            conn.execute("UPDATE jobs SET status='completed' WHERE id=%s", (job_id,))
        _delete(o, path)
        assert _counts(o) == (1, 1, 1)
    finally:
        writer.close()


@pytest.mark.parametrize("path", ["registry", "trash"])
def test_deletion_winner_refuses_waiting_pin_and_rolls_back_job_outbox(
    trigger_owner, monkeypatch, path
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionPinError,
    )
    from core.application.workflows import workflow_trigger_lifecycle as lifecycle
    from core.infrastructure.db import psycopg_runtime

    o = trigger_owner
    locked = threading.Event()
    release = threading.Event()
    pin_started = threading.Event()
    pid = []
    retire = lifecycle.retire_entrypoint_credentials

    def wait_after_workflow_lock(cursor, entrypoints, **kwargs):
        locked.set()
        assert release.wait(15)
        retire(cursor, entrypoints, **kwargs)

    monkeypatch.setattr(lifecycle, "retire_entrypoint_credentials", wait_after_workflow_lock)

    def admit():
        connection = psycopg_runtime.connect(o.dsn)
        connection.autocommit = False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pid.append(cursor.fetchone()[0])
            pin_started.set()
            try:
                result = _create_job(o, connection)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        deletion = executor.submit(_delete, o, path)
        try:
            assert locked.wait(10)
            admission = executor.submit(admit)
            assert pin_started.wait(10)
            _wait_for_lock(o.dsn, pid[0])
            release.set()
            deletion.result(timeout=15)
            with pytest.raises(WorkflowDefinitionPinError):
                admission.result(timeout=15)
        finally:
            release.set()
    assert _counts(o) == (0, 0, 0)
    with psycopg.connect(o.dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM workflow_definition_snapshots WHERE workflow_id=%s", (o.workflow,)
        ).fetchone() == (0,)


def test_late_pin_after_deletion_already_refuses_all_durable_artifacts(trigger_owner):
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionPinError,
    )
    from core.infrastructure.db import psycopg_runtime

    o = trigger_owner
    _delete(o, "registry")
    connection = psycopg_runtime.connect(o.dsn)
    connection.autocommit = False
    try:
        with pytest.raises(WorkflowDefinitionPinError):
            _create_job(o, connection)
        connection.rollback()
    finally:
        connection.close()
    assert _counts(o) == (0, 0, 0)


def test_retirement_inventory_protects_committed_pin_and_all_versions(trigger_owner):
    from core.infrastructure.db import psycopg_runtime

    o = trigger_owner
    other = f"pin-consumer-{uuid4().hex}"
    from psycopg.types.json import Jsonb

    with psycopg.connect(o.dsn) as conn:
        conn.execute(
            "INSERT INTO registry_workflows(workflow_key,display_name,account_id) VALUES (%s,'Pinned consumer',%s)",
            (other, DEFAULT_ACCOUNT_ID),
        )
        conn.execute(
            "INSERT INTO registry_workflow_steps(owner_workflow_id,step_id,step_kind,override) VALUES (%s,'request','http_request',%s)",
            (other, Jsonb({"http_request": {"auth_mode": "bearer", "auth_ref": o.key}})),
        )
    connection = psycopg_runtime.connect(o.dsn)
    connection.autocommit = False
    try:
        from core.application.jobs.job_submission_repository import JobSubmissionRepository

        JobSubmissionRepository(o.store, connection).create(
            encrypted_envelope="fixture",
            outbox_status="pending",
            project_id=o.project,
            task_id=uuid4().hex,
            task_source="step_test_run",
            selected_workflow_key=other,
            status="pending",
        )
        connection.commit()
        with psycopg.connect(o.dsn) as conn:
            conn.execute("DELETE FROM registry_workflow_steps WHERE owner_workflow_id=%s", (other,))
        _delete(o, "registry")
        with psycopg.connect(o.dsn) as conn:
            assert conn.execute(
                "SELECT active FROM project_secrets WHERE project_id=%s AND secret_key=%s",
                (o.project, o.key),
            ).fetchone() == (True,)
    finally:
        connection.close()
        with psycopg.connect(o.dsn) as conn:
            conn.execute("DELETE FROM jobs WHERE project_id=%s", (o.project,))
            conn.execute("DELETE FROM registry_workflows WHERE workflow_id=%s", (other,))


@pytest.mark.parametrize("path", ["registry", "trash"])
def test_public_deleted_key_is_unknown_before_auth_body_replay_or_enqueue(
    trigger_owner, monkeypatch, path
):
    from psycopg.types.json import Jsonb

    from tests.unit.test_workflow_entrypoints_route import (
        _build_entrypoint_client,
        _workflow_entrypoint_test_surface,
    )

    o = trigger_owner
    with psycopg.connect(o.dsn) as conn:
        conn.execute(
            "UPDATE workflow_entrypoints SET auth_mode='bearer_token',auth_config_jsonb=%s WHERE entry_key=%s",
            (Jsonb({"secret_ref": o.key}), o.key),
        )
    from core.infrastructure.db.db_pool import transaction_connection

    def enqueue(**_kwargs):
        with transaction_connection(o.store.conn) as connection:
            return _create_job(o, connection)

    with transaction_connection(o.store.conn):
        assert o.store.save_project_secret(
            project_id=o.project, secret_key=o.key, secret_value="token-123"
        )
    client, state = _build_entrypoint_client(monkeypatch, enqueue_impl=enqueue)
    surface = _workflow_entrypoint_test_surface()
    authenticate = surface._validate_workflow_entrypoint_auth

    def authenticate_from_storage(**kwargs):
        return authenticate(**{**kwargs, "bl": o.store})

    monkeypatch.setattr(surface, "_validate_workflow_entrypoint_auth", authenticate_from_storage)
    monkeypatch.setattr(
        surface,
        "_load_workflow_entrypoint",
        lambda _bl, key: load_workflow_entrypoint(o.store, key),
    )
    response = client.post(
        "/webhook/workflows/" + o.key,
        json={"title": "witness"},
        headers={"Authorization": "Bearer token-123"},
    )
    assert response.status_code == 202, response.text
    assert state["enqueued"] == 1
    assert _counts(o) == (1, 1, 1)
    with psycopg.connect(o.dsn) as conn:
        conn.execute("UPDATE jobs SET status='completed' WHERE project_id=%s", (o.project,))
        conn.execute("UPDATE workflow_entrypoints SET enabled=false WHERE entry_key=%s", (o.key,))
    assert client.post("/webhook/workflows/" + o.key, json={}).status_code == 409
    _delete(o, path)
    crossed = []

    def forbidden(*args, **kwargs):
        crossed.append("post-prepare")
        raise AssertionError("Deleted key crossed preparation boundary")

    for name in (
        "_validate_workflow_entrypoint_auth",
        "_stage_request_body_with_cap",
        "_read_body_with_cap",
        "_stage_multipart_files",
        "_claim_idempotency_reservation",
    ):
        monkeypatch.setattr(surface, name, forbidden)
    for key, token in (
        (o.key, ""),
        (o.key, "wrong"),
        (o.key, "token-123"),
        ("unknown-" + o.key, "token-123"),
    ):
        response = client.post(
            "/webhook/workflows/" + key,
            content=b"{invalid body",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        assert response.status_code == 404
        assert response.json()["error"] == "Workflow entrypoint not found"
    assert crossed == []
    assert state["enqueued"] == 1
    assert _counts(o) == (1, 1, 1)


@pytest.mark.parametrize(
    "mode",
    ["bearer_token", "shared_secret_header", "basic_auth", "hmac_sha256"],
)
def test_retirement_uses_persisted_refs_across_projects_and_auth_modes(trigger_owner, mode):
    from psycopg.types.json import Jsonb

    o = trigger_owner
    second = str(uuid4())
    old_ref = "persisted-" + uuid4().hex
    sentinel = o.key + "-similar"
    with psycopg.connect(o.dsn) as conn:
        conn.execute(
            "INSERT INTO projects(id,name,account_id) VALUES (%s,'Second trigger project',%s)",
            (second, DEFAULT_ACCOUNT_ID),
        )
        conn.execute(
            "INSERT INTO project_workflows(project_id,workflow_id) VALUES (%s,%s)",
            (second, o.workflow),
        )
        conn.execute(
            "INSERT INTO project_secrets(project_id,secret_key,secret_value_ciphertext) VALUES (%s,%s,'second'),(%s,%s,'sentinel')",
            (second, old_ref, o.project, sentinel),
        )
        conn.execute(
            "INSERT INTO project_secrets(project_id,secret_key,secret_value_ciphertext,active) VALUES (%s,%s,'old',false)",
            (second, old_ref),
        )
        auth = {"password_secret_ref" if mode == "basic_auth" else "secret_ref": old_ref}
        conn.execute(
            "INSERT INTO workflow_entrypoints(workflow_id,project_id,entry_key,auth_mode,auth_config_jsonb,secret_ref) VALUES (%s,%s,%s,%s,%s,%s)",
            (o.workflow, second, "regenerated-" + uuid4().hex, mode, Jsonb(auth), old_ref),
        )
        conn.execute(
            "INSERT INTO secret_refs(account_id,ref_key,encrypted_value) VALUES (%s,%s,'default-sentinel')",
            (DEFAULT_ACCOUNT_ID, o.key),
        )
    try:
        _delete(o, "registry")
        with psycopg.connect(o.dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM project_secrets WHERE project_id=%s AND secret_key=%s AND active",
                (second, old_ref),
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT active FROM project_secrets WHERE project_id=%s AND secret_key=%s",
                (o.project, sentinel),
            ).fetchone() == (True,)
            assert conn.execute(
                "SELECT encrypted_value FROM secret_refs WHERE account_id=%s AND ref_key=%s",
                (DEFAULT_ACCOUNT_ID, o.key),
            ).fetchone() == ("default-sentinel",)
    finally:
        with psycopg.connect(o.dsn) as conn:
            conn.execute("DELETE FROM projects WHERE id=%s", (second,))
            conn.execute(
                "DELETE FROM secret_refs WHERE account_id=%s AND ref_key=%s",
                (DEFAULT_ACCOUNT_ID, o.key),
            )


@pytest.mark.parametrize("path", ["registry", "trash"])
def test_delete_history_captures_editor_commit_before_workflow_lock(
    trigger_owner, monkeypatch, path
):
    from core.application.workflows import workflow_trigger_lifecycle as lifecycle

    owner = trigger_owner
    old = owner.registry.get_workflow(owner.workflow)
    assert old["revision"] == 1 and old["steps"] == []
    original_lock = lifecycle.lock_workflow_owner
    ordering = []

    def editor_then_lock(cursor, workflow_id, **kwargs):
        # The delete owner has already completed any pre-lock lookup. The editor
        # commits before the actual W acquisition, with an explicit reached-lock seam.
        with psycopg.connect(owner.dsn) as editor:
            editor.execute(
                "UPDATE registry_workflows SET revision=7,display_name='Committed editor definition' WHERE workflow_id=%s",
                (owner.workflow,),
            )
            editor.execute(
                "INSERT INTO registry_workflow_steps(owner_workflow_id,step_id,step_kind,display_name) VALUES (%s,'edited','http_request','Committed step')",
                (owner.workflow,),
            )
        ordering.append("editor committed")
        result = original_lock(cursor, workflow_id, **kwargs)
        ordering.append("delete locked current W")
        assert result["revision"] == 7
        return result

    monkeypatch.setattr(lifecycle, "lock_workflow_owner", editor_then_lock)
    _delete(owner, path)
    with psycopg.connect(owner.dsn) as connection:
        snapshot = connection.execute(
            "SELECT snapshot FROM registry_changelog WHERE entity_id=%s AND action=%s ORDER BY id DESC LIMIT 1",
            (owner.workflow, "delete" if path == "registry" else "trash"),
        ).fetchone()[0]
        assert snapshot.get("revision") == 7
        assert snapshot["display_name"] == "Committed editor definition"
        assert snapshot["steps"][0]["step_id"] == "edited"
        assert snapshot["steps"][0]["display_name"] == "Committed step"
        assert connection.execute(
            "SELECT revision,deleted_at IS NOT NULL FROM registry_workflows WHERE workflow_id=%s",
            (owner.workflow,),
        ).fetchone() == (8, True)
    assert ordering == ["editor committed", "delete locked current W"]


@pytest.mark.parametrize("path", ["registry", "trash"])
@pytest.mark.parametrize("admission", ["stale", "matching"])
def test_claimed_schedule_resumed_after_retirement_keeps_paused_slot(
    trigger_owner, path, admission
):
    from core.infrastructure.runtime.control.scans_scheduled_jobs import _run_scheduled_job_once

    owner = trigger_owner
    with psycopg.connect(owner.dsn) as connection:
        connection.execute(
            "UPDATE scheduled_jobs SET admission_allowed=true,admission_workflow_revision=1,admission_reason='allowed' WHERE id=%s",
            (owner.schedule,),
        )
    claimed = owner.schedules.claim_due_scheduled_job(
        project_id=owner.project,
        scheduled_job_id=owner.schedule,
        expected_next_run_at=owner.due,
        next_run_at=owner.due + timedelta(minutes=5),
    )
    assert isinstance(claimed, dict) and claimed["enabled"] is True
    assert claimed["admission_workflow_revision"] == claimed["current_workflow_revision"] == 1
    if admission == "stale":
        claimed["admission_workflow_revision"] = 0
    _delete(owner, path)
    with psycopg.connect(owner.dsn) as connection:
        retired_slot = connection.execute(
            "SELECT next_run_at FROM scheduled_jobs WHERE id=%s", (owner.schedule,)
        ).fetchone()[0]
    attempted = []

    def enqueue(_bl, **_kwargs):
        from core.infrastructure.db.db_pool import transaction_connection

        attempted.append("canonical pin")
        with transaction_connection(owner.store.conn) as physical:
            return {"job_id": _create_job(owner, physical)}

    result = _run_scheduled_job_once(
        owner.schedules,
        claimed,
        SimpleNamespace(),
        advance_schedule=False,
        scheduled_for=owner.due,
        _get_project_run_config_fn=lambda *_: {
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_name": "Fixture",
        },
        _materialize_scheduled_job_launch_fn=lambda *_: ({}, []),
        _enqueue_scheduled_job_fn=enqueue,
    )
    assert result["status"] in {"failed", "skipped"}
    assert attempted == (["canonical pin"] if admission == "matching" else [])
    with psycopg.connect(owner.dsn) as connection:
        assert connection.execute(
            "SELECT enabled,status,next_run_at FROM scheduled_jobs WHERE id=%s", (owner.schedule,)
        ).fetchone() == (False, "paused", retired_slot)
        history = connection.execute(
            "SELECT status FROM scheduled_job_runs WHERE scheduled_job_id=%s", (owner.schedule,)
        ).fetchall()
        assert history == [(result["status"],)]
    assert _counts(owner) == (0, 0, 0)


def test_concurrent_registry_delete_loser_preserves_actual_admin_404(trigger_owner, monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request

    from core.application.workflows import workflow_trigger_lifecycle as lifecycle
    from core.entrypoints.admin import workflows_routes_write as routes

    owner = trigger_owner
    winner = RegistryDBStore(owner.dsn)
    monkeypatch.setattr(winner, "_trigger_public_contract_projection_sync", lambda: None)
    original_lock = lifecycle.lock_workflow_owner
    ordering = []
    reloads = []
    initial = owner.registry.get_workflow(owner.workflow)
    monkeypatch.setattr(routes, "_get_db", lambda: owner.registry)
    monkeypatch.setattr(routes, "_resolve_workflow_scope_target", lambda *_: (initial, owner.scope))
    monkeypatch.setattr(routes, "_assert_workflow_mutation_allowed", lambda *_: None)
    monkeypatch.setattr(
        routes,
        "_get_workflow_portability_service",
        lambda *_: SimpleNamespace(assert_delete_allowed=lambda *_: None),
    )
    monkeypatch.setattr(
        routes,
        "_get_registry",
        lambda: SimpleNamespace(reload_all=lambda: reloads.append("route reload")),
    )
    monkeypatch.setattr(
        owner.registry,
        "_trigger_public_contract_projection_sync",
        lambda: reloads.append("loser projection"),
    )

    def winner_then_lock(cursor, workflow_id, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(lifecycle, "lock_workflow_owner", original_lock)
            winner.delete_workflow(owner.workflow)
        ordering.append("winner committed")
        return original_lock(cursor, workflow_id, **kwargs)

    monkeypatch.setattr(lifecycle, "lock_workflow_owner", winner_then_lock)
    try:
        with pytest.raises(HTTPException) as exc:
            routes.delete_workflow(
                owner.workflow,
                Request(
                    {
                        "type": "http",
                        "method": "DELETE",
                        "path": "/api/workflows/" + owner.workflow,
                        "headers": [],
                    }
                ),
            )
        assert exc.value.status_code == 404
        assert reloads == [] and ordering == ["winner committed"]
        with psycopg.connect(owner.dsn) as connection:
            assert connection.execute(
                "SELECT revision FROM registry_workflows WHERE workflow_id=%s", (owner.workflow,)
            ).fetchone() == (2,)
            assert connection.execute(
                "SELECT count(*) FROM registry_changelog WHERE entity_id=%s AND action='delete'",
                (owner.workflow,),
            ).fetchone() == (1,)
        # The adapter has already unwound: its physical transaction is unbound.
        assert owner.registry._get_tx_conn() is None
    finally:
        winner._conn.close()


@pytest.mark.parametrize("path", ["registry", "trash"])
def test_zero_row_tombstone_rolls_back_actual_owner(trigger_owner, path):
    from psycopg import sql

    from core.application.management.errors import ServiceNotFoundError

    owner = trigger_owner
    trigger = "suppress_tombstone_" + uuid4().hex
    with psycopg.connect(owner.dsn) as connection:
        connection.execute(
            sql.SQL(
                "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NULL; END;'"
            ).format(sql.Identifier(trigger))
        )
        connection.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE UPDATE ON registry_workflows FOR EACH ROW WHEN (OLD.workflow_id={} AND NEW.deleted_at IS NOT NULL) EXECUTE FUNCTION {}()"
            ).format(sql.Identifier(trigger), sql.Literal(owner.workflow), sql.Identifier(trigger))
        )
    before = _state(owner)
    try:
        with pytest.raises((KeyError, ServiceNotFoundError)):
            _delete(owner, path)
        assert _state(owner) == before
    finally:
        with psycopg.connect(owner.dsn) as connection:
            connection.execute(
                sql.SQL("DROP TRIGGER {} ON registry_workflows").format(sql.Identifier(trigger))
            )
            connection.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(trigger)))
