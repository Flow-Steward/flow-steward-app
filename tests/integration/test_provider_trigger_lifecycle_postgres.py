"""Signed synthetic provider deliveries and retained workflow routing on real PG."""

from __future__ import annotations

import hashlib
import hmac
import logging
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tests.integration import test_workflow_trigger_lifecycle_postgres as _trigger_lifecycle
from tests.integration.test_workflow_trigger_lifecycle_postgres import _delete

# Shared disposable fixture; bound by assignment so test parameters can use its name.
trigger_owner = _trigger_lifecycle.trigger_owner

pytestmark = pytest.mark.integration


def _provider_client(owner, monkeypatch, *, enqueue=None, deferred=None):
    from core.entrypoints.api import provider_webhooks as facade
    from core.entrypoints.api import provider_webhooks_router as router
    from core.infrastructure.runtime import entrypoint_db_adapters

    calls = []
    signing_key = b"synthetic-local-provider-key"

    class Store:
        conn = owner.store.conn

        def get_provider(self, key):
            return {
                "provider_key": key,
                "provider_type_key": "task_provider",
                "enabled": True,
                "account_id": str(owner.scope.account_id),
            }

        def record_domain_event(self, **kwargs):
            return kwargs

    def signature(_key, *, request_headers, raw_body, **kwargs):
        expected = hmac.new(signing_key, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(
            request_headers.get("x-signature", ""), expected
        ), "invalid signature"

    def project_signature(*args, **kwargs):
        calls.append("project-auth")
        return signature(*args, **kwargs)

    monkeypatch.setattr(
        entrypoint_db_adapters,
        "provider_signature_enforcers",
        lambda: (signature, project_signature, signature),
    )
    monkeypatch.setattr(
        entrypoint_db_adapters,
        "ingress_runtime",
        lambda: SimpleNamespace(resolve_ingress_project_id=lambda **_: owner.project),
    )
    monkeypatch.setattr(
        facade,
        "_resolve_task_provider_project_run_config",
        lambda *_args, **_kwargs: (owner.project, {"project_name": "Fixture"}),
    )
    monkeypatch.setattr(
        facade,
        "_apply_provider_project_state_updates",
        lambda *_args, **_kwargs: (
            calls.append("deferred-state") or (deferred() if deferred else None)
        ),
    )
    monkeypatch.setattr(
        router,
        "_claim_provider_webhook_delivery",
        lambda *_args, **_kwargs: calls.append("replay") or {"claimed": enqueue is not None},
    )

    monkeypatch.setattr(
        facade,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            validate_inputs_for_workflow_id=lambda *_args, **_kwargs: SimpleNamespace(
                ok=True, errors=[], inputs={}, interface={"enabled": False}
            )
        ),
    )
    if enqueue is not None:
        monkeypatch.setattr(facade, "enqueue_workflow_start", enqueue)
        monkeypatch.setattr(
            router,
            "_mark_provider_webhook_delivery_accepted",
            lambda *_args, **_kwargs: calls.append("accepted"),
        )

    def normalize(**kwargs):
        kwargs["deferred_project_state_updates"].append({"invocation": {"status": "succeeded"}})
        return {
            "task_id": "synthetic-event",
            "project_id": owner.project,
            "trigger_config": {},
            "files": [],
        }

    app = FastAPI()
    app.include_router(
        facade.create_provider_webhooks_router(
            env_flag=lambda _name, default: default,
            request_scope=lambda *_args, **_kwargs: owner.scope,
            to_json_obj=lambda value: value,
            get_logger_only=lambda: Store(),
            service_error_response=lambda exc, **_: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
            api_error_response=lambda exc, **_: JSONResponse({"error": str(exc)}, status_code=500),
            event_command_service=lambda: SimpleNamespace(_enqueue_task=lambda **_: None),
            resolve_task_provider_project_run_config=lambda *_args, **_kwargs: (
                owner.project,
                {"project_name": "Fixture"},
            ),
            resolve_provider_extension_for_runtime=lambda *_args, **_kwargs: {},
            provider_supports_ingress_events=lambda _: False,
            normalize_task_provider_payload=normalize,
            ingress_notification_provider_payload=lambda **_: {},
            logger=logging.getLogger(__name__),
            err_database_not_configured="unavailable",
            err_webhook_processing_failed="failed",
        )
    )
    body = b'{"event":"synthetic-event"}'
    headers = {
        "content-type": "application/json",
        "x-signature": hmac.new(signing_key, body, hashlib.sha256).hexdigest(),
    }
    return TestClient(app), calls, body, headers


def test_deleted_retained_provider_never_falls_back_to_generic_effects(trigger_owner, monkeypatch):
    owner = trigger_owner
    _delete(owner, "registry")
    client, calls, body, headers = _provider_client(owner, monkeypatch)
    response = client.post("/webhook/providers/" + owner.provider, content=body, headers=headers)
    assert calls == [], f"deleted workflow delivery crossed into generic provider effects: {calls}"
    assert response.status_code == 404


@pytest.mark.parametrize(
    "configuration", ["disabled", "unmatched", "generic", "removed", "other-project"]
)
def test_provider_routing_uses_only_retained_configuration_for_resolved_project(
    trigger_owner, monkeypatch, configuration
):
    from uuid import uuid4

    from core.application.auth.config import DEFAULT_ACCOUNT_ID

    owner = trigger_owner
    foreign_project = str(uuid4())
    with psycopg.connect(owner.dsn) as conn:
        if configuration == "disabled":
            conn.execute(
                "UPDATE workflow_provider_triggers SET enabled=false WHERE workflow_id=%s",
                (owner.workflow,),
            )
        elif configuration == "unmatched":
            conn.execute(
                "UPDATE workflow_provider_triggers SET event_name='different-event' WHERE workflow_id=%s",
                (owner.workflow,),
            )
        elif configuration in {"generic", "removed"}:
            conn.execute(
                "DELETE FROM workflow_provider_triggers WHERE workflow_id=%s", (owner.workflow,)
            )
        else:
            conn.execute(
                "INSERT INTO projects(id,name,account_id) VALUES (%s,'Other routing project',%s)",
                (foreign_project, DEFAULT_ACCOUNT_ID),
            )
            conn.execute(
                "INSERT INTO project_workflows(project_id,workflow_id) VALUES (%s,%s)",
                (foreign_project, owner.workflow),
            )
            conn.execute(
                "UPDATE workflow_provider_triggers SET project_id=%s WHERE workflow_id=%s",
                (foreign_project, owner.workflow),
            )
    if configuration == "removed":
        _delete(owner, "trash")
    try:
        client, calls, body, headers = _provider_client(owner, monkeypatch)
        response = client.post(
            "/webhook/providers/" + owner.provider, content=body, headers=headers
        )
        if configuration in {"disabled", "unmatched"}:
            assert response.status_code == 404
            assert calls == []
        else:
            assert response.status_code == 409
            assert calls == ["project-auth", "deferred-state", "replay"]
    finally:
        if configuration == "other-project":
            with psycopg.connect(owner.dsn) as conn:
                conn.execute("DELETE FROM projects WHERE id=%s", (foreign_project,))


def test_stale_selected_provider_after_delete_has_no_deferred_effects(trigger_owner, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from core.application.workflows import workflow_provider_trigger_repository as repository
    from tests.integration.test_workflow_trigger_lifecycle_postgres import _counts

    owner = trigger_owner
    selected, release = threading.Event(), threading.Event()
    original = repository.list_enabled_provider_triggers_for_provider

    def select(*args, **kwargs):
        rows = original(*args, **kwargs)
        assert rows
        selected.set()
        assert release.wait(15)
        return rows

    monkeypatch.setattr(repository, "list_enabled_provider_triggers_for_provider", select)
    client, calls, body, headers = _provider_client(owner, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        delivery = executor.submit(
            client.post, "/webhook/providers/" + owner.provider, content=body, headers=headers
        )
        try:
            assert selected.wait(10)
            _delete(owner, "registry")
        finally:
            release.set()
        assert delivery.result(timeout=15).status_code == 404
    assert calls == []
    assert _counts(owner) == (0, 0, 0)


def test_provider_guard_spans_deferred_effect_and_canonical_pin(trigger_owner, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from core.application.management.errors import ServiceConflictError
    from tests.integration.test_workflow_trigger_lifecycle_postgres import (
        _counts,
        _create_job,
        _wait_for_lock,
    )

    owner = trigger_owner
    effect, release, deleting = threading.Event(), threading.Event(), threading.Event()
    pids = []

    def deferred():
        # This statement must use the same physical owner held by provider admission.
        with owner.store.conn.cursor() as cursor:
            cursor.execute(
                "UPDATE projects SET name='Accepted provider effect' WHERE id=%s", (owner.project,)
            )
        effect.set()
        assert release.wait(15)

    def enqueue(**kwargs):
        assert kwargs["selected_workflow_id"] == owner.workflow
        return _create_job(owner, owner.store.conn)

    def delete():
        with owner.registry.conn.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pids.append(cursor.fetchone()[0])
        deleting.set()
        _delete(owner, "registry")

    client, calls, body, headers = _provider_client(
        owner, monkeypatch, enqueue=enqueue, deferred=deferred
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        delivery = executor.submit(
            client.post, "/webhook/providers/" + owner.provider, content=body, headers=headers
        )
        try:
            assert effect.wait(10)
            deletion = executor.submit(delete)
            assert deleting.wait(10)
            _wait_for_lock(owner.dsn, pids[0])
        finally:
            release.set()
        assert delivery.result(timeout=15).status_code == 200
        with pytest.raises(ServiceConflictError) as blocked:
            deletion.result(timeout=15)
        assert blocked.value.payload["code"] == "workflow_has_nonterminal_jobs"
    assert calls == ["project-auth", "deferred-state", "replay", "accepted"]
    assert _counts(owner) == (1, 1, 1)
    with psycopg.connect(owner.dsn) as conn:
        assert conn.execute(
            "SELECT name FROM projects WHERE id=%s", (owner.project,)
        ).fetchone() == ("Accepted provider effect",)
