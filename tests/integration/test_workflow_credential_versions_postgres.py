"""Established credential version writes and retirement on canonical PostgreSQL."""

from __future__ import annotations

from functools import partial

import psycopg
import pytest

from core.infrastructure.db.db_pool import transaction_connection
from tests.integration import test_workflow_trigger_lifecycle_postgres as _trigger_lifecycle
from tests.integration.test_workflow_trigger_lifecycle_postgres import (
    _create_job,
    _delete,
    _state,
)

# Shared disposable fixture; bound by assignment so test parameters can use its name.
trigger_owner = _trigger_lifecycle.trigger_owner

pytestmark = pytest.mark.integration


def test_actual_project_secret_versions_preserve_values_metadata_and_inactive_state(trigger_owner):
    owner = trigger_owner
    for index, active in enumerate((True, True, False)):
        with transaction_connection(owner.store.conn):
            assert owner.store.save_project_secret(
                project_id=owner.project,
                secret_key=owner.key,
                secret_value=f"local-value-{index}",
                active=active,
                metadata={"fixture_version": index},
            )
        assert owner.store.get_project_secret(project_id=owner.project, secret_key=owner.key) == (
            f"local-value-{index}" if active else ""
        )
    with psycopg.connect(owner.dsn) as conn:
        rows = conn.execute(
            "SELECT active,key_version,metadata_jsonb,secret_value_ciphertext FROM project_secrets WHERE project_id=%s AND metadata_jsonb <> '{}'::jsonb ORDER BY created_at,id",
            (owner.project,),
        ).fetchall()
        assert len(rows) == 3
        assert all(
            not row[0] and row[1] == "aesgcm-v1" and row[3] != f"local-value-{index}"
            for index, row in enumerate(rows)
        )
        assert [row[2] for row in rows] == [{"fixture_version": index} for index in range(3)]


def test_restore_explicit_rotation_and_enable_restore_only_one_trigger(trigger_owner):
    from core.application.workflows.workflow_entrypoint_repository import load_workflow_entrypoint
    from core.application.workflows.workflow_webhook_creation_service import (
        WorkflowWebhookUpdateCommand,
        create_workflow_webhook_update_service,
    )

    owner = trigger_owner
    service = create_workflow_webhook_update_service(
        resolve_project_binding=lambda *_: (owner.project, {})
    )
    _delete(owner, "registry")
    owner.service.restore("registry_workflow", owner.workflow, scope=owner.scope)
    current = load_workflow_entrypoint(owner.store, owner.key)
    command = WorkflowWebhookUpdateCommand.for_rotation(
        workflow_id=owner.workflow,
        entry_key=owner.key,
        existing=current,
        project_id=owner.project,
        expected_input_names=set(),
    )
    _, fresh = service.update(bl=owner.store, command=command)
    assert fresh
    current = load_workflow_entrypoint(owner.store, owner.key)
    assert current["enabled"] is False
    command = WorkflowWebhookUpdateCommand.from_request(
        workflow_id=owner.workflow,
        entry_key=owner.key,
        existing=current,
        request={"enabled": True, "project_id": owner.project},
        expected_input_names=set(),
    )
    service.update(bl=owner.store, command=command)
    assert (
        owner.store.get_project_secret(project_id=owner.project, secret_key=current["secret_ref"])
        == fresh
    )
    with transaction_connection(owner.store.conn) as connection:
        _create_job(owner, connection)
    with psycopg.connect(owner.dsn) as conn:
        assert conn.execute(
            "SELECT enabled FROM workflow_entrypoints WHERE entry_key=%s", (owner.key,)
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT enabled FROM workflow_provider_triggers WHERE workflow_id=%s", (owner.workflow,)
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT enabled,status FROM scheduled_jobs WHERE id=%s", (owner.schedule,)
        ).fetchone() == (False, "paused")


@pytest.mark.parametrize("path", ["registry", "trash"])
def test_version_lock_conflict_rolls_back_complete_lifecycle(trigger_owner, path):
    from core.application.workflows.project_credential_versions import (
        WorkflowCredentialVersionConflict,
    )

    owner = trigger_owner
    before = _state(owner)
    with psycopg.connect(owner.dsn) as holder:
        holder.execute(
            "SELECT id FROM project_secrets WHERE project_id=%s FOR UPDATE", (owner.project,)
        )
        with pytest.raises(WorkflowCredentialVersionConflict):
            _delete(owner, path)
        assert _state(owner) == before


@pytest.mark.parametrize("oauth", [False, True])
def test_actual_callback_version_writer_foreign_metadata_and_no_resurrection(
    trigger_owner, monkeypatch, oauth
):
    from core.application.extensions.callback_registration import (
        ExtensionCallbackRegistrationService,
        OAuthCallbackRegistrationPreconditionError,
    )
    from core.infrastructure.db.platform_stores import PlatformIntegrationStore

    owner = trigger_owner
    store = PlatformIntegrationStore(owner.dsn)
    service = ExtensionCallbackRegistrationService(store, project_repository=owner.store)
    declaration = {
        "purpose": "oauth_authorization" if oauth else "inbound_events",
        "auth": {"type": "none" if oauth else "shared_secret_header"},
        "connection_scoped": False,
    }
    monkeypatch.setattr(service, "_require_callback", lambda *_args, **_kwargs: declaration)
    try:
        kwargs = {
            "extension_id": "synthetic-lifecycle",
            "callback_id": "callback",
            "project_id": owner.project,
            "base_url": "https://fixture.invalid",
        }
        if oauth:
            with transaction_connection(store.conn):
                result = service.ensure_oauth_registration(**kwargs)
            ref = result.registration["auth_config"]["capability_token_ref"]
        else:
            result = service.create_registration(**kwargs)
            ref = result.registration["auth_config"]["secret_ref"]
        assert store.get_project_secret(project_id=owner.project, secret_key=ref)
        with psycopg.connect(owner.dsn) as conn:
            conn.execute(
                "UPDATE workflow_entrypoints SET secret_ref=%s WHERE entry_key=%s", (ref, owner.key)
            )
        _delete(owner, "registry")
        assert store.get_project_secret(project_id=owner.project, secret_key=ref)
        if oauth:
            with transaction_connection(store.conn):
                again = service.ensure_oauth_registration(**kwargs)
            assert again.callback_token == result.callback_token
            with psycopg.connect(owner.dsn) as conn:
                conn.execute(
                    "UPDATE project_secrets SET active=false WHERE project_id=%s AND secret_key=%s",
                    (owner.project, ref),
                )
            with (
                pytest.raises(OAuthCallbackRegistrationPreconditionError),
                transaction_connection(store.conn),
            ):
                service.ensure_oauth_registration(**kwargs)
            assert store.get_project_secret(project_id=owner.project, secret_key=ref) == ""
    finally:
        store.conn.close()


@pytest.mark.parametrize("oauth", [False, True])
def test_callback_guard_conflict_escapes_actual_owner_without_registration(
    trigger_owner, monkeypatch, oauth
):
    from secrets import token_urlsafe
    from uuid import UUID, uuid4

    from core.application.extensions import callback_registration as callbacks
    from core.application.extensions.callback_contract import (
        callback_capability_ref,
        callback_secret_ref,
    )
    from core.application.workflows.project_credential_versions import (
        WorkflowCredentialVersionConflict,
    )
    from core.infrastructure.db.platform_stores import PlatformIntegrationStore

    owner = trigger_owner
    registration_id = uuid4()
    ref = (callback_capability_ref if oauth else callback_secret_ref)(str(registration_id))
    store = PlatformIntegrationStore(owner.dsn)
    service = callbacks.ExtensionCallbackRegistrationService(store, project_repository=owner.store)
    monkeypatch.setattr(
        service,
        "_require_callback",
        lambda *_args, **_kwargs: {
            "purpose": "oauth_authorization" if oauth else "inbound_events",
            "auth": {"type": "none" if oauth else "shared_secret_header"},
            "connection_scoped": False,
        },
    )
    monkeypatch.setattr(callbacks.uuid, "uuid4", lambda: UUID(str(registration_id)))
    kwargs = {
        "extension_id": "synthetic-lifecycle",
        "callback_id": "callback",
        "project_id": owner.project,
        "base_url": "https://fixture.invalid",
    }

    def register() -> None:
        if oauth:
            with (
                transaction_connection(store.conn) as physical,
                physical.cursor() as cursor,
            ):
                cursor.execute(
                    "UPDATE projects SET name='must roll back' WHERE id=%s",
                    (owner.project,),
                )
                service.ensure_oauth_registration(**kwargs)
        else:
            service.create_registration(**kwargs)

    try:
        with transaction_connection(store.conn):
            assert store.save_project_secret(
                project_id=owner.project,
                secret_key=ref,
                secret_value=token_urlsafe(32),
                active=False,
            )
        with psycopg.connect(owner.dsn) as holder:
            holder.execute(
                "SELECT id FROM project_secrets WHERE project_id=%s AND secret_key=%s FOR UPDATE",
                (owner.project, ref),
            )
            with pytest.raises(WorkflowCredentialVersionConflict):
                register()
        with psycopg.connect(owner.dsn) as conn:
            assert conn.execute(
                "SELECT name FROM projects WHERE id=%s", (owner.project,)
            ).fetchone() == ("Retirement fixture",)
            assert conn.execute(
                "SELECT count(*) FROM extension_callback_registrations WHERE project_id=%s",
                (owner.project,),
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT count(*),bool_or(active) FROM project_secrets WHERE project_id=%s AND secret_key=%s",
                (owner.project, ref),
            ).fetchone() == (1, False)
    finally:
        store.conn.close()


def test_stale_route_preloaded_webhook_command_cannot_change_or_resurrect_owner(trigger_owner):
    from core.application.workflows.workflow_entrypoint_repository import load_workflow_entrypoint
    from core.application.workflows.workflow_webhook_creation_service import (
        WorkflowWebhookCreationError,
        WorkflowWebhookUpdateCommand,
        create_workflow_webhook_update_service,
    )

    owner = trigger_owner
    service = create_workflow_webhook_update_service(
        resolve_project_binding=lambda *_: (owner.project, {})
    )
    current = load_workflow_entrypoint(owner.store, owner.key)
    command = WorkflowWebhookUpdateCommand.for_rotation(
        workflow_id=owner.workflow,
        entry_key=owner.key,
        existing=current,
        project_id=owner.project,
        expected_input_names=set(),
    )
    with psycopg.connect(owner.dsn) as conn:
        conn.execute(
            "UPDATE workflow_entrypoints SET description='newer command' WHERE entry_key=%s",
            (owner.key,),
        )
    before = _state(owner)
    with pytest.raises(WorkflowWebhookCreationError) as stale:
        service.update(bl=owner.store, command=command)
    assert stale.value.status_code == 409
    assert _state(owner) == before
    _delete(owner, "trash")
    before = _state(owner)
    with pytest.raises(WorkflowWebhookCreationError) as deleted:
        service.update(bl=owner.store, command=command)
    assert deleted.value.status_code == 404
    assert _state(owner) == before


def _cleanup_client(owner, monkeypatch, scope):
    import logging

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.entrypoints.api import workflow_entrypoints_registry, workflow_entrypoints_router

    monkeypatch.setattr(
        workflow_entrypoints_registry, "_workflow_registry_db", lambda: owner.registry
    )
    app = FastAPI()
    app.include_router(
        workflow_entrypoints_router.create_workflow_entrypoints_router(
            get_logger_only=lambda: owner.store,
            request_scope=lambda *_args, **_kwargs: scope,
            enqueue_task=lambda **_: None,
            artifact_plane=None,
            logger=logging.getLogger(__name__),
            err_database_not_configured="unavailable",
        )
    )
    return TestClient(app)


@pytest.mark.parametrize(
    "boundary",
    ["authorized", "workflow", "account", "project-scope", "workflow-scope", "wrong-project"],
)
def test_trashed_webhook_cleanup_keeps_exact_ownership_boundary(
    trigger_owner, monkeypatch, boundary
):
    from uuid import uuid4

    from core.application.auth.config import DEFAULT_ACCOUNT_ID

    owner = trigger_owner
    other = str(uuid4())
    scope = owner.scope
    if boundary == "account":
        scope = scope.model_copy(update={"account_id": other})
    elif boundary == "project-scope":
        scope = scope.model_copy(update={"allowed_project_ids": frozenset({other})})
    elif boundary == "workflow-scope":
        scope = scope.model_copy(update={"allowed_workflow_ids": frozenset({"other"})})
    with psycopg.connect(owner.dsn) as conn:
        conn.execute(
            "INSERT INTO projects(id,name,account_id) VALUES (%s,'Unrelated cleanup project',%s)",
            (other, DEFAULT_ACCOUNT_ID),
        )
        conn.execute(
            "INSERT INTO project_workflows(project_id,workflow_id) VALUES (%s,%s)",
            (other, owner.workflow),
        )
    try:
        _delete(owner, "registry")
        before = _state(owner)
        client = _cleanup_client(owner, monkeypatch, scope)
        workflow = "unknown-" + owner.workflow if boundary == "workflow" else owner.workflow
        project = other if boundary == "wrong-project" else owner.project
        response = client.delete(
            f"/api/workflows/{workflow}/triggers/webhooks/{owner.key}?project_id={project}"
        )
        if boundary == "authorized":
            assert response.status_code == 200, response.text
            with psycopg.connect(owner.dsn) as conn:
                assert conn.execute(
                    "SELECT count(*) FROM workflow_entrypoints WHERE entry_key=%s", (owner.key,)
                ).fetchone() == (0,)
        else:
            assert response.status_code in {403, 404}, response.text
            assert _state(owner) == before
    finally:
        with psycopg.connect(owner.dsn) as conn:
            conn.execute("DELETE FROM projects WHERE id=%s", (other,))


def test_reference_committed_after_single_inventory_may_be_ordered_after_retirement(
    trigger_owner, monkeypatch
):
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.workflows import workflow_trigger_lifecycle as lifecycle

    owner = trigger_owner
    other = "overlapping-reference-" + uuid4().hex
    with psycopg.connect(owner.dsn) as conn:
        conn.execute(
            "INSERT INTO registry_workflows(workflow_key,display_name,account_id) VALUES (%s,'Reference-only save',%s)",
            (other, DEFAULT_ACCOUNT_ID),
        )
    decisions = []
    retire = lifecycle.retire_entrypoint_credentials

    class InventoryBarrier:
        def __init__(self, cursor):
            self.cursor = cursor
            self.inventory = False

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, query, params=()):
            self.inventory = "WITH scope AS" in query
            return self.cursor.execute(query, params)

        def fetchall(self):
            rows = self.cursor.fetchall()
            if self.inventory:
                decisions.append("inventory")
                with psycopg.connect(owner.dsn) as conn:
                    conn.execute(
                        "INSERT INTO registry_workflow_steps(owner_workflow_id,step_id,step_kind,override) VALUES (%s,'overlap','http_request',%s)",
                        (
                            other,
                            Jsonb({"http_request": {"auth_mode": "bearer", "auth_ref": owner.key}}),
                        ),
                    )
                decisions.append("literal committed")
            return rows

    monkeypatch.setattr(
        lifecycle,
        "retire_entrypoint_credentials",
        lambda cursor, *args, **kwargs: retire(InventoryBarrier(cursor), *args, **kwargs),
    )
    try:
        _delete(owner, "registry")
        decisions.append("deletion committed")
        assert decisions == ["inventory", "literal committed", "deletion committed"]
        with psycopg.connect(owner.dsn) as conn:
            assert conn.execute(
                "SELECT active FROM project_secrets WHERE project_id=%s AND secret_key=%s",
                (owner.project, owner.key),
            ).fetchone() == (False,)
            assert conn.execute(
                "SELECT count(*) FROM registry_workflow_steps WHERE owner_workflow_id=%s", (other,)
            ).fetchone() == (1,)
    finally:
        with psycopg.connect(owner.dsn) as conn:
            conn.execute("DELETE FROM registry_workflows WHERE workflow_id=%s", (other,))


@pytest.mark.parametrize(
    "boundary", ["authorized", "workflow", "account", "project-scope", "workflow-scope"]
)
def test_trashed_provider_cleanup_keeps_exact_ownership_boundary(
    trigger_owner, monkeypatch, boundary
):
    from uuid import uuid4

    owner = trigger_owner
    scope = owner.scope
    if boundary == "account":
        scope = scope.model_copy(update={"account_id": str(uuid4())})
    elif boundary == "project-scope":
        scope = scope.model_copy(update={"allowed_project_ids": frozenset({str(uuid4())})})
    elif boundary == "workflow-scope":
        scope = scope.model_copy(update={"allowed_workflow_ids": frozenset({"other"})})
    _delete(owner, "trash")
    before = _state(owner)
    with psycopg.connect(owner.dsn) as conn:
        trigger_id = conn.execute(
            "SELECT trigger_id FROM workflow_provider_triggers WHERE workflow_id=%s",
            (owner.workflow,),
        ).fetchone()[0]
    client = _cleanup_client(owner, monkeypatch, scope)
    workflow = "unknown-" + owner.workflow if boundary == "workflow" else owner.workflow
    response = client.delete(f"/api/workflows/{workflow}/triggers/providers/{trigger_id}")
    if boundary == "authorized":
        assert response.status_code == 200, response.text
        with psycopg.connect(owner.dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM workflow_provider_triggers WHERE trigger_id=%s", (trigger_id,)
            ).fetchone() == (0,)
    else:
        assert response.status_code in {403, 404}, response.text
        assert _state(owner) == before


def test_authenticated_public_request_reaching_submission_after_delete_has_no_durable_job(
    trigger_owner, monkeypatch
):
    from psycopg.types.json import Jsonb

    from core.application.workflows import workflow_entrypoint_repository
    from core.application.workflows.workflow_entrypoint_repository import load_workflow_entrypoint
    from tests.integration.test_workflow_trigger_lifecycle_postgres import _counts
    from tests.unit.test_workflow_entrypoints_route import (
        _build_entrypoint_client,
        _workflow_entrypoint_test_surface,
    )

    owner = trigger_owner
    with transaction_connection(owner.store.conn):
        assert owner.store.save_project_secret(
            project_id=owner.project, secret_key=owner.key, secret_value="token-123"
        )
    with psycopg.connect(owner.dsn) as conn:
        conn.execute(
            "UPDATE workflow_entrypoints SET auth_mode='bearer_token',auth_config_jsonb=%s WHERE entry_key=%s",
            (Jsonb({"secret_ref": owner.key}), owner.key),
        )
    phases = []

    def enqueue(**_kwargs):
        phases.append("authenticated submission reached")
        _delete(owner, "registry")
        phases.append("deletion committed")
        with transaction_connection(owner.store.conn) as connection:
            return _create_job(owner, connection)

    client, state = _build_entrypoint_client(monkeypatch, enqueue_impl=enqueue)
    surface = _workflow_entrypoint_test_surface()
    monkeypatch.setattr(
        surface,
        "_load_workflow_entrypoint",
        lambda _bl, key: load_workflow_entrypoint(owner.store, key),
    )
    # The unit client's stand-in connection only knows its own workflow; admission
    # must read this owner's real state.
    admission_state = workflow_entrypoint_repository.load_workflow_admission_state
    monkeypatch.setattr(
        workflow_entrypoint_repository,
        "load_workflow_admission_state",
        lambda _bl, workflow_id: admission_state(owner.store, workflow_id),
    )
    authenticate = surface._validate_workflow_entrypoint_auth
    monkeypatch.setattr(
        surface,
        "_validate_workflow_entrypoint_auth",
        lambda **kwargs: authenticate(**{**kwargs, "bl": owner.store}),
    )
    response = client.post(
        "/webhook/workflows/" + owner.key,
        json={"title": "stale authenticated delivery"},
        headers={"Authorization": "Bearer token-123"},
    )
    assert response.status_code >= 400
    assert phases == ["authenticated submission reached", "deletion committed"]
    assert state["enqueued"] == 1
    assert _counts(owner) == (0, 0, 0)


@pytest.mark.parametrize("path", ["registry", "trash"])
@pytest.mark.parametrize("winner", ["rotation", "deletion"])
def test_actual_rotation_and_retirement_serialize_without_late_active_version(
    trigger_owner, monkeypatch, path, winner
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from core.application.workflows import workflow_trigger_lifecycle as lifecycle
    from core.application.workflows.workflow_entrypoint_repository import load_workflow_entrypoint
    from core.application.workflows.workflow_webhook_creation_service import (
        WorkflowWebhookCreationError,
        WorkflowWebhookUpdateCommand,
        create_workflow_webhook_update_service,
    )
    from core.infrastructure.db.platform_stores import PlatformProjectStore
    from tests.integration.test_workflow_trigger_lifecycle_postgres import _wait_for_lock

    owner = trigger_owner
    writer = PlatformProjectStore(owner.dsn)
    with psycopg.connect(owner.dsn) as setup:
        setup.execute(
            "UPDATE workflow_entrypoints SET auth_config_jsonb=jsonb_build_object('secret_ref',secret_ref) WHERE entry_key=%s",
            (owner.key,),
        )
    service = create_workflow_webhook_update_service(
        resolve_project_binding=lambda *_: (owner.project, {})
    )
    command = WorkflowWebhookUpdateCommand.for_rotation(
        workflow_id=owner.workflow,
        entry_key=owner.key,
        existing=load_workflow_entrypoint(writer, owner.key),
        project_id=owner.project,
        expected_input_names=set(),
    )
    reached = threading.Event()
    release = threading.Event()
    contender_started = threading.Event()
    contender_pid = []
    original_save = writer.save_project_secret
    original_retire = lifecycle.retire_entrypoint_credentials

    def held_save(**kwargs):
        result = original_save(**kwargs)
        reached.set()
        assert release.wait(10)
        return result

    def held_retirement(*args, **kwargs):
        reached.set()
        assert release.wait(10)
        return original_retire(*args, **kwargs)

    if winner == "rotation":
        monkeypatch.setattr(writer, "save_project_secret", held_save)
    else:
        monkeypatch.setattr(lifecycle, "retire_entrypoint_credentials", held_retirement)

    def rotate():
        return service.update(bl=writer, command=command)

    def delete():
        return _delete(owner, path)

    def contender():
        store = (
            (owner.registry if path == "registry" else owner.store)
            if winner == "rotation"
            else writer
        )
        with store.conn.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            contender_pid.append(cursor.fetchone()[0])
        contender_started.set()
        return delete() if winner == "rotation" else rotate()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(rotate if winner == "rotation" else delete)
            try:
                assert reached.wait(10)
                second = executor.submit(contender)
                assert contender_started.wait(10)
                _wait_for_lock(owner.dsn, contender_pid[0])
            finally:
                release.set()
            first.result(timeout=10)
            if winner == "rotation":
                second.result(timeout=10)
            else:
                with pytest.raises(WorkflowWebhookCreationError) as exc:
                    second.result(timeout=10)
                assert exc.value.status_code == 404
        with psycopg.connect(owner.dsn) as connection:
            assert connection.execute(
                "SELECT count(*),bool_or(active) FROM project_secrets WHERE project_id=%s AND secret_key=%s",
                (owner.project, owner.key),
            ).fetchone() == (2 if winner == "rotation" else 1, False)
            assert connection.execute(
                "SELECT deleted_at IS NOT NULL FROM registry_workflows WHERE workflow_id=%s",
                (owner.workflow,),
            ).fetchone() == (True,)
    finally:
        release.set()
        writer.conn.close()


@pytest.mark.parametrize("operation", ["create", "enable", "regenerate"])
def test_deleted_owner_refuses_trigger_mutations_without_changes(trigger_owner, operation):
    from core.application.workflows.workflow_entrypoint_repository import load_workflow_entrypoint
    from core.application.workflows.workflow_webhook_creation_service import (
        WorkflowWebhookCreateCommand,
        WorkflowWebhookCreationError,
        WorkflowWebhookUpdateCommand,
        create_workflow_webhook_creation_service,
        create_workflow_webhook_mutation_service,
        create_workflow_webhook_update_service,
    )

    owner = trigger_owner
    current = load_workflow_entrypoint(owner.store, owner.key)

    def resolve(*_):
        return owner.project, {}

    _delete(owner, "registry")
    before = _state(owner)
    if operation == "create":
        create_command = WorkflowWebhookCreateCommand.from_request(
            workflow_id=owner.workflow,
            project_id=owner.project,
            entry_key=owner.key + "new",
            request={},
            expected_input_names=set(),
        )
        creation = create_workflow_webhook_creation_service(resolve_project_binding=resolve)
        mutate = partial(creation.create, bl=owner.store, command=create_command)
    elif operation == "enable":
        update_command = WorkflowWebhookUpdateCommand.from_request(
            workflow_id=owner.workflow,
            entry_key=owner.key,
            existing=current,
            request={"enabled": True},
            expected_input_names=set(),
        )
        update = create_workflow_webhook_update_service(resolve_project_binding=resolve)
        mutate = partial(update.update, bl=owner.store, command=update_command)
    else:
        mutation = create_workflow_webhook_mutation_service()
        mutate = partial(
            mutation.regenerate,
            bl=owner.store,
            entry_key=owner.key,
            next_entry_key=owner.key + "new",
        )
    with pytest.raises(WorkflowWebhookCreationError) as exc:
        mutate()
    assert exc.value.status_code == 404
    assert _state(owner) == before


def test_inactive_projection_consumer_retains_exact_current_authority(trigger_owner):
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    from core.application.auth.config import DEFAULT_ACCOUNT_ID

    owner = trigger_owner
    suffix = uuid4().hex
    extension, release, install = "fixture." + suffix, "release_" + suffix, "install_" + suffix
    try:
        with psycopg.connect(owner.dsn) as connection:
            connection.execute(
                "INSERT INTO extension_releases(release_id,extension_id,release_version,account_id) VALUES (%s,%s,'1.0.0',%s)",
                (release, extension, DEFAULT_ACCOUNT_ID),
            )
            connection.execute(
                "INSERT INTO extension_installs(install_id,extension_id,release_id,account_id) VALUES (%s,%s,%s,%s)",
                (install, extension, release, DEFAULT_ACCOUNT_ID),
            )
            connection.execute(
                "INSERT INTO extension_registry_projections(account_id,install_id,release_id,extension_id,artifact_kind,artifact_id,is_active,projection_jsonb) VALUES (%s,%s,%s,%s,'workflow','consumer',false,%s)",
                (
                    DEFAULT_ACCOUNT_ID,
                    install,
                    release,
                    extension,
                    Jsonb(
                        {
                            "steps": [
                                {
                                    "step_kind": "http_request",
                                    "override": {
                                        "http_request": {
                                            "auth_mode": "bearer",
                                            "auth_ref": "secret:" + owner.key,
                                        }
                                    },
                                }
                            ]
                        }
                    ),
                ),
            )
        _delete(owner, "registry")
        with psycopg.connect(owner.dsn) as connection:
            assert connection.execute(
                "SELECT active FROM project_secrets WHERE project_id=%s AND secret_key=%s",
                (owner.project, owner.key),
            ).fetchone() == (True,)
    finally:
        with psycopg.connect(owner.dsn) as connection:
            connection.execute("DELETE FROM extension_installs WHERE install_id=%s", (install,))
            connection.execute("DELETE FROM extension_releases WHERE release_id=%s", (release,))


def test_same_literal_key_in_foreign_account_remains_byte_exact(trigger_owner):
    from uuid import uuid4

    owner = trigger_owner
    account, project = str(uuid4()), str(uuid4())
    try:
        with psycopg.connect(owner.dsn) as connection:
            connection.execute(
                "INSERT INTO accounts(id,account_key) VALUES (%s,%s)",
                (account, "fixture-" + account),
            )
            connection.execute(
                "INSERT INTO projects(id,name,account_id) VALUES (%s,'Foreign sentinel',%s)",
                (project, account),
            )
            connection.execute(
                "INSERT INTO project_secrets(project_id,secret_key,secret_value_ciphertext) VALUES (%s,%s,'foreign-synthetic')",
                (project, owner.key),
            )
            before = connection.execute(
                "SELECT to_jsonb(s) FROM project_secrets s WHERE project_id=%s", (project,)
            ).fetchall()
        _delete(owner, "registry")
        with psycopg.connect(owner.dsn) as connection:
            assert (
                connection.execute(
                    "SELECT to_jsonb(s) FROM project_secrets s WHERE project_id=%s", (project,)
                ).fetchall()
                == before
            )
    finally:
        with psycopg.connect(owner.dsn) as connection:
            connection.execute("DELETE FROM projects WHERE id=%s", (project,))
            connection.execute("DELETE FROM accounts WHERE id=%s", (account,))


def test_legacy_top_level_only_rotation_leaves_unreferenced_old_key_retained(trigger_owner):
    from core.application.workflows.workflow_entrypoint_repository import load_workflow_entrypoint
    from core.application.workflows.workflow_webhook_creation_service import (
        WorkflowWebhookUpdateCommand,
        create_workflow_webhook_update_service,
    )

    owner = trigger_owner
    current = load_workflow_entrypoint(owner.store, owner.key)
    assert current["secret_ref"] == owner.key
    assert current["auth_config_jsonb"] == {}
    command = WorkflowWebhookUpdateCommand.for_rotation(
        workflow_id=owner.workflow,
        entry_key=owner.key,
        existing=current,
        project_id=owner.project,
        expected_input_names=set(),
    )
    service = create_workflow_webhook_update_service(
        resolve_project_binding=lambda *_: (owner.project, {})
    )
    _, token = service.update(bl=owner.store, command=command)
    current = load_workflow_entrypoint(owner.store, owner.key)
    assert current["secret_ref"] != owner.key
    assert (
        owner.store.get_project_secret(project_id=owner.project, secret_key=current["secret_ref"])
        == token
    )
    _delete(owner, "registry")
    with psycopg.connect(owner.dsn) as connection:
        assert connection.execute(
            "SELECT active FROM project_secrets WHERE project_id=%s AND secret_key=%s",
            (owner.project, owner.key),
        ).fetchone() == (True,)
        assert connection.execute(
            "SELECT active FROM project_secrets WHERE project_id=%s AND secret_key=%s",
            (owner.project, current["secret_ref"]),
        ).fetchone() == (False,)


def test_guard_refresh_is_reentrant_and_includes_new_backdated_retained_version(trigger_owner):
    from uuid import uuid4

    from core.application.workflows.project_credential_versions import (
        guard_project_credential_versions,
    )

    owner = trigger_owner
    new_id = str(uuid4())
    observed = []
    with psycopg.connect(owner.dsn) as connection, connection.cursor() as cursor:

        class BetweenPasses:
            def execute(self, *args):
                return cursor.execute(*args)

            def fetchall(self):
                rows = cursor.fetchall()
                observed.append({str(row[0]) for row in rows})
                if len(observed) == 1:
                    # A newly visible retained version must be acquired by the fresh
                    # pass; its age and active flag cannot select a different anchor.
                    connection.execute(
                        "INSERT INTO project_secrets(id,project_id,secret_key,active,created_at) VALUES (%s,%s,%s,false,'2000-01-01')",
                        (new_id, owner.project, owner.key),
                    )
                return rows

        guard_project_credential_versions(
            BetweenPasses(), project_id=owner.project, secret_key=owner.key
        )
        assert len(observed) == 2
        assert new_id not in observed[0]
        assert new_id in observed[1]
        guard_project_credential_versions(cursor, project_id=owner.project, secret_key=owner.key)
    _delete(owner, "registry")
    with psycopg.connect(owner.dsn) as connection:
        assert connection.execute(
            "SELECT count(*),bool_or(active) FROM project_secrets WHERE project_id=%s AND secret_key=%s",
            (owner.project, owner.key),
        ).fetchone() == (2, False)


def test_different_workflow_actual_rotation_conflict_rolls_back_without_partial_version(
    trigger_owner, monkeypatch
):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.workflows.workflow_entrypoint_repository import load_workflow_entrypoint
    from core.application.workflows.workflow_webhook_creation_service import (
        WorkflowWebhookCreationError,
        WorkflowWebhookUpdateCommand,
        create_workflow_webhook_update_service,
    )
    from core.infrastructure.db.platform_stores import PlatformProjectStore

    owner = trigger_owner
    other, entry = "shared-" + uuid4().hex, uuid4().hex
    writer = PlatformProjectStore(owner.dsn)
    reached, release = threading.Event(), threading.Event()
    try:
        with psycopg.connect(owner.dsn) as connection:
            connection.execute(
                "UPDATE workflow_entrypoints SET auth_config_jsonb=jsonb_build_object('secret_ref',secret_ref) WHERE entry_key=%s",
                (owner.key,),
            )
            connection.execute(
                "INSERT INTO registry_workflows(workflow_key,display_name,account_id) VALUES (%s,'Shared writer',%s)",
                (other, DEFAULT_ACCOUNT_ID),
            )
            connection.execute(
                "INSERT INTO project_workflows(project_id,workflow_id) VALUES (%s,%s)",
                (owner.project, other),
            )
            connection.execute(
                "INSERT INTO workflow_entrypoints(workflow_id,project_id,entry_key,secret_ref,auth_config_jsonb) VALUES (%s,%s,%s,%s,jsonb_build_object('secret_ref',%s::text))",
                (other, owner.project, entry, owner.key, owner.key),
            )
        service = create_workflow_webhook_update_service(
            resolve_project_binding=lambda *_: (owner.project, {})
        )
        commands = [
            WorkflowWebhookUpdateCommand.for_rotation(
                workflow_id=wf,
                entry_key=key,
                existing=load_workflow_entrypoint(store, key),
                project_id=owner.project,
                expected_input_names=set(),
            )
            for wf, key, store in [(owner.workflow, owner.key, writer), (other, entry, owner.store)]
        ]
        original = writer.save_project_secret

        def hold(**kwargs):
            result = original(**kwargs)
            reached.set()
            assert release.wait(10)
            return result

        monkeypatch.setattr(writer, "save_project_secret", hold)
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(service.update, bl=writer, command=commands[0])
            try:
                assert reached.wait(10)
                with pytest.raises(WorkflowWebhookCreationError) as exc:
                    service.update(bl=owner.store, command=commands[1])
                assert exc.value.status_code == 409
                assert exc.value.code == "workflow_credential_version_conflict"
            finally:
                release.set()
            first.result(timeout=10)
        with psycopg.connect(owner.dsn) as connection:
            assert connection.execute(
                "SELECT count(*),count(*) FILTER(WHERE active) FROM project_secrets WHERE project_id=%s AND secret_key=%s",
                (owner.project, owner.key),
            ).fetchone() == (2, 1)
        _delete(owner, "registry")
        with psycopg.connect(owner.dsn) as connection:
            # The other workflow's still-visible exact ref protects shared authority.
            assert connection.execute(
                "SELECT count(*) FROM project_secrets WHERE project_id=%s AND secret_key=%s AND active",
                (owner.project, owner.key),
            ).fetchone() == (1,)
    finally:
        release.set()
        writer.conn.close()
        with psycopg.connect(owner.dsn) as connection:
            connection.execute("DELETE FROM registry_workflows WHERE workflow_id=%s", (other,))


def test_old_snapshot_version_inversion_conflicts_and_rolls_back_actual_delete(
    trigger_owner, monkeypatch
):
    """Named cursors expose SQL snapshot/row acquisition, not service scheduling hooks."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from uuid import uuid4

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.workflows import workflow_trigger_lifecycle as lifecycle
    from core.application.workflows import workflow_webhook_creation_service as webhook
    from core.application.workflows.project_credential_versions import (
        WorkflowCredentialVersionConflict,
    )
    from core.application.workflows.workflow_entrypoint_repository import load_workflow_entrypoint
    from core.infrastructure.db.platform_stores import PlatformProjectStore

    owner = trigger_owner
    x = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    writers = [PlatformProjectStore(owner.dsn), PlatformProjectStore(owner.dsn)]
    workflows = ["inversion-" + uuid4().hex for _ in writers]
    entries = [uuid4().hex for _ in writers]
    b_written, b_release, b_committed = (threading.Event() for _ in range(3))
    c_locked_y, a_refresh, a_unwound, a_locked = (threading.Event() for _ in range(4))
    observed = {}
    named_cursors = []
    try:
        with psycopg.connect(owner.dsn) as connection:
            connection.execute(
                "UPDATE project_secrets SET id=%s WHERE project_id=%s AND secret_key=%s",
                (x, owner.project, owner.key),
            )
            for workflow, entry in zip(workflows, entries, strict=True):
                connection.execute(
                    "INSERT INTO registry_workflows(workflow_key,display_name,account_id) VALUES (%s,'Inversion writer',%s)",
                    (workflow, DEFAULT_ACCOUNT_ID),
                )
                connection.execute(
                    "INSERT INTO project_workflows(project_id,workflow_id) VALUES (%s,%s)",
                    (owner.project, workflow),
                )
                connection.execute(
                    "INSERT INTO workflow_entrypoints(workflow_id,project_id,entry_key,secret_ref,auth_config_jsonb) VALUES (%s,%s,%s,%s,jsonb_build_object('secret_ref',%s::text))",
                    (workflow, owner.project, entry, owner.key, owner.key),
                )
        before = _state(owner)
        service = webhook.create_workflow_webhook_update_service(
            resolve_project_binding=lambda *_: (owner.project, {})
        )
        commands = [
            webhook.WorkflowWebhookUpdateCommand.for_rotation(
                workflow_id=workflow,
                entry_key=entry,
                existing=load_workflow_entrypoint(writer, entry),
                project_id=owner.project,
                expected_input_names=set(),
            )
            for workflow, entry, writer in zip(workflows, entries, writers, strict=True)
        ]
        real_save = writers[0].save_project_secret

        def hold_b(**kwargs):
            result = real_save(**kwargs)
            b_written.set()
            assert b_release.wait(10)
            return result

        monkeypatch.setattr(writers[0], "save_project_secret", hold_b)

        def rotate_b():
            result = service.update(bl=writers[0], command=commands[0])
            b_committed.set()
            return result

        class SnapshotCursor:
            def __init__(self, cursor, role):
                self.cursor, self.role, self.named, self.used = cursor, role, None, False

            def __getattr__(self, name):
                return getattr(self.cursor, name)

            def execute(self, statement, parameters):
                if not self.used and statement.startswith("SELECT id FROM project_secrets"):
                    self.used = True
                    # Exact production guard SQL and parameters on its same physical
                    # transaction. DECLARE fixes A's old snapshot before B commits;
                    # FETCH acquires rows later. The assertions below prove that premise.
                    self.named = self.cursor.connection.cursor(name="inversion_" + self.role)
                    named_cursors.append(self.named)
                    self.named.execute(statement, parameters)
                    if self.role == "a":
                        b_release.set()
                        assert b_committed.wait(10)
                    return None
                return self.cursor.execute(statement, parameters)

            def fetchall(self):
                if self.named is None:
                    return self.cursor.fetchall()
                named, self.named = self.named, None
                if self.role == "a":
                    rows = named.fetchall()
                    observed["a_old_snapshot"] = [str(row[0]) for row in rows]
                    assert observed["a_old_snapshot"] == [x]
                    observed["a_backend"] = self.cursor.connection.info.backend_pid
                    observed["a_locked_x"] = True
                    a_locked.set()
                    assert a_refresh.wait(10)
                else:
                    first = named.fetchone()
                    assert first is not None
                    assert str(first[0]) != x
                    observed["y"] = str(first[0])
                    assert observed["y"] < x
                    c_locked_y.set()
                    assert a_unwound.wait(10)
                    rows = [first, *named.fetchall()]
                    assert {str(row[0]) for row in rows} == {x, observed["y"]}
                named.close()
                return rows

        real_retire = lifecycle.retire_entrypoint_credentials

        def retire_with_old_snapshot(cursor, *args, **kwargs):
            return real_retire(SnapshotCursor(cursor, "a"), *args, **kwargs)

        monkeypatch.setattr(lifecycle, "retire_entrypoint_credentials", retire_with_old_snapshot)
        real_webhook_transaction = webhook._webhook_transaction

        @contextmanager
        def writer_transaction(store, workflow_id):
            with real_webhook_transaction(store, workflow_id) as cursor:
                yield SnapshotCursor(cursor, "c") if store is writers[1] else cursor

        monkeypatch.setattr(webhook, "_webhook_transaction", writer_transaction)
        real_delete_transaction = owner.registry._transaction

        @contextmanager
        def delete_transaction():
            with real_delete_transaction() as cursor:
                cursor.execute(
                    "UPDATE registry_workflows SET description='rollback inversion probe' WHERE workflow_id=%s",
                    (owner.workflow,),
                )
                yield cursor

        monkeypatch.setattr(owner.registry, "_transaction", delete_transaction)

        def delete_a():
            try:
                return _delete(owner, "registry")
            finally:
                # This event is outside the actual registry owner, after rollback.
                a_unwound.set()

        with ThreadPoolExecutor(max_workers=3) as executor:
            b_future = executor.submit(rotate_b)
            try:
                assert b_written.wait(10)
                a_future = executor.submit(delete_a)
                assert b_committed.wait(10)
                # A's first FETCH must finish before C begins acquiring Y then X.
                # An actual lock observer distinguishes acquisition from scheduling.
                with psycopg.connect(owner.dsn, autocommit=True) as observer:
                    assert a_locked.wait(10)
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        observer.execute(
                            "SELECT id FROM project_secrets WHERE id=%s FOR UPDATE NOWAIT", (x,)
                        )
                c_future = executor.submit(service.update, bl=writers[1], command=commands[1])
                assert c_locked_y.wait(10)
                with (
                    psycopg.connect(owner.dsn, autocommit=True) as observer,
                    pytest.raises(psycopg.errors.LockNotAvailable),
                ):
                    observer.execute(
                        "SELECT id FROM project_secrets WHERE id=%s FOR UPDATE NOWAIT",
                        (observed["y"],),
                    )
                a_refresh.set()
                with pytest.raises(WorkflowCredentialVersionConflict) as failure:
                    a_future.result(timeout=10)
                assert failure.value.__cause__.sqlstate == "55P03"
                _, final_token = c_future.result(timeout=10)
                b_future.result(timeout=10)
            finally:
                b_release.set()
                a_refresh.set()
                a_unwound.set()
        after = _state(owner)
        assert {key: value for key, value in after.items() if key != "project_secrets"} == {
            key: value for key, value in before.items() if key != "project_secrets"
        }
        assert (
            writers[1].get_project_secret(project_id=owner.project, secret_key=owner.key)
            == final_token
        )
        with psycopg.connect(owner.dsn) as connection:
            rows = connection.execute(
                "SELECT id,active,secret_value_ciphertext FROM project_secrets WHERE project_id=%s AND secret_key=%s",
                (owner.project, owner.key),
            ).fetchall()
        assert len(rows) == 3
        assert sum(bool(row[1]) for row in rows) == 1
        assert all(not row[1] for row in rows if str(row[0]) in {x, observed["y"]})
        assert all(row[2] != final_token for row in rows)
    finally:
        b_release.set()
        a_refresh.set()
        a_unwound.set()
        for cursor in named_cursors:
            if not cursor.closed:
                cursor.close()
        for writer in writers:
            writer.conn.close()
        with psycopg.connect(owner.dsn) as connection:
            connection.execute(
                "DELETE FROM registry_workflows WHERE workflow_id=ANY(%s)", (workflows,)
            )
