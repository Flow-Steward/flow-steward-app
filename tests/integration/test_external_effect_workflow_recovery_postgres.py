"""Persisted operator decisions traverse existing graph bridges into real claims."""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from core.application.management.command_models_api_inputs import ApprovalDecisionInput
from core.application.management.scope import ScopeContext
from core.application.orchestration.approval_payload import _resolve_gate
from core.application.orchestration.graph.builder_approval_gate import _registry_approval_gate_node
from core.application.orchestration.graph.nodes import execute_step_node
from core.application.orchestration.graph.resume_checkpoint import (
    _build_state_based_approval_update,
)
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.workflows.workflow_definition_snapshot import definition_digest
from core.infrastructure.db.external_effect_store import get_external_effect
from tests._workflow_definitions import edge, node
from tests.unit.test_step_hub_external_effects import DEFINITION, EXTENSION, _apply, _hub, _run

pytestmark = pytest.mark.integration


@pytest.fixture
def workflow_case(disposable_db, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "workflow-recovery-test-root")
    clear_run_definition_cache()
    account, workspace, project, job = (str(uuid4()) for _ in range(4))
    install, thread = f"install-{uuid4().hex}", f"thread-{uuid4().hex}"
    conn = psycopg.connect(disposable_db.dsn, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (id,account_key) VALUES (%s,%s)",
            (account, f"workflow-resolution-{uuid4().hex}"),
        )
        cur.execute(
            "INSERT INTO workspaces (id,account_id,workspace_key,display_name) VALUES (%s,%s,%s,'Effect recovery')",
            (workspace, account, f"workspace-{uuid4().hex}"),
        )
        cur.execute(
            "INSERT INTO projects (id,account_id,workspace_id,name) VALUES (%s,%s,%s,%s)",
            (project, account, workspace, f"project-{uuid4().hex}"),
        )
        cur.execute(
            "INSERT INTO extension_installs (extension_id,install_id,account_id) VALUES ('example.host-fixture',%s,%s)",
            (install, account),
        )
        cur.execute(
            "INSERT INTO jobs (id,account_id,workspace_id,project_id,task_id,thread_id,status,selected_workflow,meta) VALUES (%s,%s,%s,%s,'effect-recovery',%s,'awaiting_approval','wf-effects','{}'::jsonb)",
            (job, account, workspace, project, thread),
        )
    extension = copy.deepcopy(EXTENSION)
    extension["extension_install_id"] = install
    store = SimpleNamespace(conn=conn, cursor=conn.cursor, log_event=lambda **kwargs: None)
    provider_calls = []
    lookup_calls = []
    consumer_calls = []
    remote_records = {}

    def provider(*_args, **kwargs):
        operation = kwargs.get("action_id")
        business_key = kwargs.get("input_payload", {}).get("record_id")
        if operation == "lookup_record":
            lookup_calls.append(kwargs)
            record = remote_records.get(business_key)
            return {"status": "succeeded", "response": {"ok": True, "result": record or {}}}
        if operation == "consume_record":
            consumer_calls.append(kwargs)
            record = next(
                (value for value in remote_records.values() if value["record_id"] == business_key),
                None,
            )
            return {
                "status": "succeeded" if record else "failed",
                "response": {"ok": bool(record), "result": record or {}},
            }
        provider_calls.append(kwargs)
        if getattr(case, "remote_write_succeeded", False):
            remote_records[business_key] = {
                "record_id": "provider-record-from-exact-business-lookup"
            }
        for descriptor in kwargs.get("artifact_outputs") or []:
            if getattr(case, "plane", None) is not None:
                case.plane.write_artifact_content(
                    str(descriptor["artifact_id"]),
                    body=b"real provider output",
                    content_type="text/plain",
                )
        if len(provider_calls) == 1:
            return {
                "status": "succeeded"
                if getattr(case, "nominal_provider_success", False)
                else "failed",
                "detail": "provider_timeout",
                "response": {
                    "ok": False,
                    "external_effect_status": "timeout_unknown",
                    "error_code": "provider_timeout",
                    **(
                        {"result": {"record_id": case.partial_id}}
                        if getattr(case, "partial_id", None)
                        else {}
                    ),
                },
            }
        if getattr(case, "ordinary_cycle", False):
            record = {"record_id": "provider-record-after-authorized-retry"}
            remote_records[business_key] = record
            return {"status": "succeeded", "response": {"ok": True, "result": record}}
        return {
            "status": "succeeded",
            "response": {"ok": True, "record": "provider-record-after-intentional-retry"},
        }

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance", lambda: registry
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
        lambda *args, **kwargs: (extension, store),
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action", provider
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "core.application.orchestration.agents.common.emit_event", lambda *args, **kwargs: None
    )
    for module in ("connector_events", "connector_extension_runtime", "connector_step_runtime"):
        monkeypatch.setattr(
            f"core.application.orchestration.{module}._shared_logger", lambda: store
        )
    monkeypatch.setattr(
        "core.application.orchestration.connector_events._shared_job_store", lambda: store
    )
    monkeypatch.setattr(
        "core.application.orchestration.connector_events._emit_connector_tool_event",
        lambda *args, **kwargs: None,
    )
    case = SimpleNamespace(
        conn=conn,
        store=store,
        account=account,
        workspace=workspace,
        project=project,
        job=job,
        thread=thread,
        install=install,
        provider_calls=provider_calls,
        lookup_calls=lookup_calls,
        consumer_calls=consumer_calls,
        remote_records=remote_records,
        extension=extension,
        dsn=disposable_db.dsn,
        workflow_registry_ids=[],
        interrupt_ids={},
    )
    yield case
    with conn.cursor() as cur:
        cur.execute("DELETE FROM command_audit_log WHERE account_id=%s", (account,))
        cur.execute("DELETE FROM external_effects WHERE account_id=%s", (account,))
        cur.execute("DELETE FROM jobs WHERE id=%s", (job,))
        for registry_id in case.workflow_registry_ids:
            cur.execute(
                "DELETE FROM workflow_definition_snapshots WHERE workflow_registry_id=%s",
                (registry_id,),
            )
            cur.execute("DELETE FROM registry_workflows WHERE id=%s", (registry_id,))
        cur.execute("DELETE FROM extension_installs WHERE install_id=%s", (install,))
        cur.execute("DELETE FROM projects WHERE id=%s", (project,))
        cur.execute("DELETE FROM workspaces WHERE id=%s", (workspace,))
        cur.execute("DELETE FROM accounts WHERE id=%s", (account,))
    conn.close()
    clear_run_definition_cache()


def _start(
    case,
    *,
    required_output=False,
    loop=False,
    policy="await_approval",
    inherited=False,
    lookup=False,
):
    definition = copy.deepcopy(DEFINITION)
    definition["steps"][0]["connector_install_id"] = case.install
    # Preserve Main's legacy explicit policy; default cases pass policy=None.
    definition["steps"][0]["error_policy"] = (
        {"on_error": policy} if policy and not inherited else {}
    )
    if getattr(case, "partial_id", None):
        definition["steps"][0]["output_mapping_jsonb"] = {"record_id": "state.record_id"}
    if policy == "route_to_step" and not loop:
        definition["execution_graph"]["edges"].append(edge("publish", node("review"), "error"))
    if required_output:
        definition["steps"][1]["boundary_contract"] = {
            "entry": [{"name": "record_id", "source": "state.record_id", "required": True}]
        }
    if loop:
        definition["steps"][0]["for_each_id"] = "batch"
        definition["for_each"] = [
            {
                "for_each_id": "batch",
                "error_policy": {"on_error": policy} if inherited and policy else {},
                "over": "state.items",
                "item_name": "current_item",
                "body": {"kind": "sequence", "entry": node("publish")},
            }
        ]
        definition["execution_graph"]["entry"] = node("batch", "for_each")
        definition["execution_graph"]["edges"] = [
            edge("publish", {"kind": "for_each_exit", "for_each_id": "batch", "lane_id": None}),
            edge("batch", node("review"), source_kind="for_each"),
            edge("review", node("done")),
        ]
        if inherited and policy == "route_to_step":
            definition["execution_graph"]["edges"].append(
                edge("batch", node("review"), "error", source_kind="for_each")
            )
    if lookup:
        for operation in ("lookup_record", "consume_record"):
            case.extension["action_definitions"].append(
                {"action_id": operation, "handler": {"mode": "extension"}}
            )
            case.extension["compiled_manifest"]["operation_manifest"].append(
                {
                    "operation_id": operation,
                    "operation_kind": "source",
                    "inputs": [{"name": "record_id", "value_type": "text", "required": True}],
                    "outputs": [{"name": "record_id", "value_type": "text", "required": True}],
                }
            )
        definition["steps"][1] = {
            "step_id": "review",
            "step_kind": "connector",
            "connector_id": "example.host-fixture.lookup_record",
            "connector_install_id": case.install,
            "input_mapping_jsonb": {"record_id": "literal:record-a"},
            "output_mapping_jsonb": {"record_id": "state.record_id"},
        }
        definition["steps"].insert(
            2,
            {
                "step_id": "consume",
                "step_kind": "connector",
                "connector_id": "example.host-fixture.consume_record",
                "connector_install_id": case.install,
                "input_mapping_jsonb": {"record_id": "state.record_id"},
                "boundary_contract": {
                    "entry": [{"name": "record_id", "source": "state.record_id", "required": True}]
                },
            },
        )
        definition["execution_graph"]["edges"] = [
            edge("publish", node("review")),
            edge("publish", node("review"), "error"),
            edge("review", node("consume")),
            edge("consume", node("done")),
        ]
    state = _run()
    ref = {"snapshot_id": str(uuid4()), "revision": 1, "digest": definition_digest(definition)}
    prime_run_definition(ref, definition)
    state.update(
        workflow_definition_ref=ref,
        account_id=case.account,
        project_id=case.project,
        workspace_id=case.workspace,
        job_id=case.job,
        thread_id=case.thread,
        items=[{"id": "one"}],
    )
    state["trusted_runtime_context"].update(
        account_id=case.account,
        project_id=case.project,
        workspace_id=case.workspace,
        job_id=case.job,
    )
    state, command = _hub(state)
    assert state["current_step_id"] == "publish", command
    return state


def _effect(case):
    with case.conn.cursor() as cur:
        cur.execute("SELECT effect_id FROM external_effects WHERE account_id=%s", (case.account,))
        effect_id = cur.fetchone()[0]
    return get_external_effect(case.conn, effect_id=str(effect_id))


def _ambiguity(case, state, *, producer_errors=False):
    result = execute_step_node(state)
    state = _apply(state, result)
    assert _effect(case)["status"] == "timeout_unknown"
    assert len(case.provider_calls) == 1
    if producer_errors:
        state["agent_messages"][-1]["artifacts"]["_schema_errors"] = [
            {"field": "record", "message": "required producer field was lost"}
        ]
    return _hub(state)


def _bridge(state, resume, bridge, monkeypatch):
    if bridge == "live":
        monkeypatch.setattr("langgraph.types.interrupt", lambda _payload: resume)
        update = _registry_approval_gate_node(state).update
    else:
        update = _build_state_based_approval_update(
            approved=resume["approved"],
            action=resume["action"],
            comment=resume["comment"],
            resume_value=resume,
            state_values=state,
        )
    return _apply(state, update)


def test_persisted_unknown_precedes_producer_schema_regeneration(workflow_case):
    case = workflow_case
    state, command = _ambiguity(case, _start(case, policy=None), producer_errors=True)
    assert state["status"] == "failed"
    assert command.goto != "human_approval_gate"
    assert not state.get("recovery_pending")
    assert len(case.provider_calls) == 1


@pytest.mark.parametrize("bridge", ["live", "state"])
def test_ordinary_other_outcome_reaches_required_input_error_without_inventing_id(
    workflow_case, monkeypatch, bridge
):
    case = workflow_case
    state = _ordinary_gate(
        case, _start_authored_cycle(case, other_target="consume", state_based=bridge == "state")
    )
    original_message = copy.deepcopy(state["agent_messages"][-1])
    resume = _ordinary_decision(case, state, monkeypatch, approved=True)
    assert not resume.get("external_effect_resolution_ref")
    state = _ordinary_resume(state, resume, bridge, monkeypatch)
    state, command = _hub(state)
    assert command.goto == "orchestrator_validation"
    assert state["status"] == "failed"
    assert "record_id" in state["error"]
    assert "required inputs" in state["error"]
    assert not state.get("record_id")
    assert state["agent_messages"][0] == original_message
    assert _effect(case)["status"] == "timeout_unknown"
    assert _effect(case)["claim_version"] == 1
    assert len(case.provider_calls) == 1
    assert not state.get("external_effect_resolution_ref")


def test_ordinary_other_outcome_keeps_existing_real_data_and_original_error_without_resending(
    workflow_case, monkeypatch
):
    case = workflow_case
    initial = _start_authored_cycle(case, other_target="consume")
    initial["record_id"] = "already-known-real-record"
    case.remote_records["previous-business-operation"] = {"record_id": initial["record_id"]}
    state = _ordinary_gate(case, initial, has_existing_record=True)
    original = copy.deepcopy(state["agent_messages"][-1])
    resume = _ordinary_decision(case, state, monkeypatch, approved=True)
    state = _ordinary_resume(state, resume, "live", monkeypatch)
    state, command = _hub(state)
    assert command.goto == "execute_step"
    assert state["current_step_id"] == "consume"
    assert state["record_id"] == "already-known-real-record"
    assert state["agent_messages"][0] == original
    state = _apply(state, execute_step_node(state))
    assert state["agent_messages"][-1]["status"] == "pass"
    assert case.consumer_calls[0]["input_payload"]["record_id"] == "already-known-real-record"
    assert len(case.provider_calls) == 1
    assert _effect(case)["status"] == "timeout_unknown"
    assert not state.get("external_effect_resolution_ref")


@pytest.mark.parametrize("status", ["cancelled", "completed"])
def test_terminal_guards_precede_ledger_unknown_and_clear_any_retry_carrier(workflow_case, status):
    case = workflow_case
    state = _start(case)
    state = _apply(state, execute_step_node(state))
    original = _effect(case)
    state.update(
        status=status,
        workflow_validation_complete=True,
        external_effect_resolution_ref={"untrusted": "stale"},
    )
    state, _command = _hub(state)
    assert state["status"] == status
    assert not state.get("recovery_pending")
    assert not state.get("external_effect_resolution_ref")
    assert _effect(case)["claim_version"] == original["claim_version"]
    assert len(case.provider_calls) == 1


def test_stale_step_result_does_not_open_an_effect_recovery(workflow_case):
    case = workflow_case
    state = _start(case)
    state = _apply(state, execute_step_node(state))
    state["agent_messages"][-1]["step_execution_id"] = str(uuid4())
    state, _command = _hub(state)
    assert not state.get("recovery_pending")
    assert state["status"] == "failed"
    assert _effect(case)["claim_version"] == 1


def test_notification_event_source_cannot_be_guessed_as_the_current_workflow_step(workflow_case):
    case = workflow_case
    state = _start_authored_cycle(case)
    state = _apply(state, execute_step_node(state))
    with case.conn.cursor() as cur:
        cur.execute(
            """UPDATE external_effects SET source_type='notification_event',
                      source_ref_jsonb='{"event_id":"event-only"}'::jsonb WHERE account_id=%s""",
            (case.account,),
        )
    state, _command = _hub(state)
    assert not state.get("external_effect_resolution_ref")
    assert not state.get("recovery_pending")
    assert _effect(case)["claim_version"] == 1
    assert len(case.provider_calls) == 1


def test_http_artifacts_cannot_authorize_connector_unknown_resolution(workflow_case):
    from datetime import UTC, datetime

    from core.application.orchestration.agent_messages import build_agent_message

    case = workflow_case
    state = _start(case)
    cursor = state["run_cursor"]
    message = build_agent_message(
        from_agent="http_request_step",
        to_agent="orchestrator",
        skill="step",
        status="fail",
        correlation_id=str(uuid4()),
        artifacts={"external_effect_status": "timeout_unknown", "error": "raw_http_timeout"},
        summary="Unknown raw HTTP outcome",
        published_at=datetime.now(UTC).isoformat(),
        step_id=cursor["step_id"],
        step_execution_id=cursor["step_execution_id"],
        attempt=cursor["attempt"],
    )
    state = _apply(state, {"agent_messages": [message]})
    state, command = _hub(state)
    assert command.goto == "human_approval_gate"  # existing explicit legacy policy
    assert state["recovery_pending"]["kind"] == "failure"
    assert not state.get("external_effect_resolution_ref")
    with case.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM external_effects WHERE account_id=%s", (case.account,))
        assert cur.fetchone()[0] == 0
    assert not case.provider_calls


def test_a_missing_checkpoint_fails_existing_resume_without_reconstructing_inputs(
    workflow_case, monkeypatch
):
    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    resume = _ordinary_decision(case, state, monkeypatch, approved=False)
    assert resume.get("external_effect_resolution_ref")
    state = _ordinary_resume(state, resume, "live", monkeypatch)
    pending = _effect(case)
    state["workflow_definition_ref"] = {}
    state, command = _hub(state)
    assert command.goto != "execute_step"
    assert state["status"] == "failed"
    assert len(case.provider_calls) == 1
    assert _effect(case)["claim_token"] == pending["claim_token"]


def test_retry_output_spec_mints_fresh_real_grants_and_old_cleanup_preserves_them(
    workflow_case, monkeypatch, tmp_path
):
    from core.application.extensions import external_effects
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.extensions.runtime_grants import (
        FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV,
    )

    case = workflow_case
    monkeypatch.setenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
    case.plane = ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "outputs"))
    )
    case.extension["compiled_manifest"]["artifact_policies"] = [
        {
            "operation_id": "publish_record",
            "outputs": [
                {
                    "kind": "artifact",
                    "binding_key": "file",
                    "filename": "result.txt",
                    "content_type": "text/plain",
                    "max_size_bytes": 1024,
                }
            ],
        }
    ]
    initial = _start_authored_cycle(case, state_based=True)
    initial["_artifact_plane"] = case.plane
    state = _ordinary_gate(case, initial)
    first_id = case.provider_calls[0]["artifact_outputs"][0]["artifact_id"]
    resume = _ordinary_decision(case, state, monkeypatch, approved=False)
    assert resume.get("external_effect_resolution_ref")
    state = _ordinary_resume(state, resume, "state", monkeypatch)
    state, command = _hub(state)
    assert command.goto == "execute_step"
    result = execute_step_node(state)
    second_id = case.provider_calls[1]["artifact_outputs"][0]["artifact_id"]
    assert first_id != second_id
    assert _effect(case)["status"] == "succeeded", result
    external_effects.reconcile_external_effect_outputs(
        case.store, account_id=case.account, artifact_plane=case.plane
    )
    assert case.plane.get_artifact(second_id) is not None
    assert case.plane.read_artifact_bytes(second_id, max_size_bytes=1024) == b"real provider output"


def test_machine_decision_audits_trusted_token_actor_not_delegated_metadata(
    workflow_case, monkeypatch
):
    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    expected_id = _resolve_gate(state).approval_id
    resume = _ordinary_decision(
        case,
        state,
        monkeypatch,
        approved=False,
        comment="Authorize one exact retry despite uncertainty.",
        token_principal=True,
        scope=ScopeContext(
            account_id=case.account,
            project_id=case.project,
            actor_id="machine-token-identity",
            principal_type="service_account",
            auth_source="machine",
        ),
        external_actor={"id": "untrusted-delegated-name"},
    )
    with case.conn.cursor() as cur:
        cur.execute(
            "SELECT actor_id,actor_type,payload_jsonb FROM command_audit_log WHERE command_id=%s",
            (resume["external_effect_resolution_ref"]["command_id"],),
        )
        actor_id, actor_type, payload = cur.fetchone()
        cur.execute("SELECT meta FROM jobs WHERE id=%s", (case.job,))
        history = cur.fetchone()[0]["approvals"][0]
    assert actor_id == "machine-token-identity"
    assert actor_type == "service_account"
    assert payload["approval_id"] == expected_id
    assert history["external_actor"]["id"] == "untrusted-delegated-name"
    assert history["actor"] == "machine-token-identity"


@pytest.mark.parametrize("producer_errors", [True, False])
def test_real_parallel_lane_unknown_blocks_before_contract_regeneration_or_generic_recovery(
    workflow_case, monkeypatch, producer_errors
):
    from core.application.orchestration.parallel_executor_node import parallel_executor_node
    from tests.unit.test_parallel_executor_node import _definition, _state

    case = workflow_case
    step = {
        **DEFINITION["steps"][0],
        "connector_install_id": case.install,
        "error_policy": {"on_error": "await_approval"},
    }
    definition = _definition({"publish_lane": [step]}, concurrency_limit=1)
    state = _state(
        definition,
        account_id=case.account,
        project_id=case.project,
        workspace_id=case.workspace,
        job_id=case.job,
        workflow_id="wf-effects",
        thread_id=case.thread,
        trusted_runtime_context={
            "account_id": case.account,
            "project_id": case.project,
            "workspace_id": case.workspace,
            "job_id": case.job,
            "workflow_id": "wf-effects",
            "test_mode": False,
        },
    )

    def execute_with_producer_error(local_state):
        update = execute_step_node(local_state)
        if producer_errors:
            update["agent_messages"][-1]["artifacts"]["_schema_errors"] = [
                {"field": "record", "message": "required producer field was lost"}
            ]
        return update

    monkeypatch.setattr(
        "core.application.orchestration.graph.nodes.execute_step_node", execute_with_producer_error
    )
    state = _apply(state, parallel_executor_node(state))
    assert _effect(case)["status"] == "timeout_unknown"
    assert len(case.provider_calls) == 1
    lane = state["parallel_lanes"][0]
    assert lane["attempt"] == 1, "Ambiguous lane operations must not enter contract regeneration"
    state, command = _hub(state)
    assert state["status"] == "failed"
    assert "external_effect_resolution_unsupported" in state["error"]
    assert not state.get("recovery_pending")
    assert not state.get("external_effect_resolution_ref")
    assert command.goto != "human_approval_gate"
    assert _effect(case)["claim_version"] == 1
    assert len(case.provider_calls) == 1


@pytest.mark.parametrize("policy", [None, "fail", "continue", "route_to_step", "retry"])
@pytest.mark.parametrize("producer_errors", [False, True])
def test_persisted_unknown_follows_authored_policy_with_no_implicit_approval(
    workflow_case, policy, producer_errors
):
    case = workflow_case
    state = _start(case, policy=policy)
    state, command = _ambiguity(case, state, producer_errors=producer_errors)
    assert command.goto != "human_approval_gate"
    assert not state.get("recovery_pending")
    assert not state.get("external_effect_resolution_ref")
    assert not state.get("operation_retry_dispatch")
    if policy in {"continue", "route_to_step"}:
        assert state["current_step_id"] == "review"
    else:
        assert state["status"] == "failed"
        if policy == "retry":
            assert "ambig" in state["error"].lower()
    effect = _effect(case)
    assert effect["status"] == "timeout_unknown"
    assert effect["claim_version"] == 1
    assert len(case.provider_calls) == 1
    with case.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM command_audit_log WHERE account_id=%s", (case.account,))
        assert cur.fetchone()[0] == 0


def test_persisted_unknown_uses_inherited_for_each_error_edge_without_approval(workflow_case):
    case = workflow_case
    state = _start(case, loop=True, policy="route_to_step", inherited=True)
    state, command = _ambiguity(case, state, producer_errors=True)
    assert command.goto != "human_approval_gate"
    assert state["current_step_id"] == "review"
    assert state["run_cursor"]["frames"] == []
    assert not state.get("recovery_pending")
    assert _effect(case)["status"] == "timeout_unknown"
    assert len(case.provider_calls) == 1


def test_failed_unknown_partial_id_is_not_promoted_before_continue_or_required_consumer(
    workflow_case,
):
    case = workflow_case
    case.partial_id = "unverified-original-response-id"
    state = _start(case, policy="continue", required_output=True)
    update = execute_step_node(state)
    assert not update.get("record_id"), (
        "An ambiguous producer must not promote its partial response ID"
    )
    assert not (update.get("workflow_data") or {}).get("record_id")
    message = update["agent_messages"][-1]
    assert (
        message["artifacts"]["connector_invocation"]["response"]["result"]["record_id"]
        == case.partial_id
    )
    # Even a nominal pass cannot turn the host's genuine unknown result into a canonical ID.
    message["status"] = "pass"
    state, command = _hub(_apply(state, update))
    assert command.goto == "orchestrator_validation"
    assert state["status"] == "failed"
    assert state["unresolved_step_failure"]["missing"] == ["record_id"]
    assert not state.get("recovery_pending")
    assert _effect(case)["status"] == "timeout_unknown"
    assert len(case.provider_calls) == 1


def test_authored_error_lookup_uses_real_correlated_id_without_repeating_unknown_write(
    workflow_case,
):
    case = workflow_case
    case.remote_write_succeeded = True
    state = _start(case, policy="route_to_step", lookup=True)
    state, command = _ambiguity(case, state, producer_errors=True)
    assert command.goto == "execute_step"
    assert state["current_step_id"] == "review"
    assert not state.get("record_id")
    assert not state.get("recovery_pending")
    state = _apply(state, execute_step_node(state))
    assert state["record_id"] == case.remote_records["record-a"]["record_id"]
    state, command = _hub(state)
    assert command.goto == "execute_step"
    assert state["current_step_id"] == "consume"
    state = _apply(state, execute_step_node(state))
    assert state["agent_messages"][-1]["status"] == "pass"
    assert (
        case.consumer_calls[0]["input_payload"]["record_id"]
        == "provider-record-from-exact-business-lookup"
    )
    state, _ = _hub(state)
    assert state["current_step_id"] == "done"
    assert len(case.provider_calls) == len(case.lookup_calls) == len(case.consumer_calls) == 1
    effect = _effect(case)
    assert effect["status"] == "timeout_unknown"
    assert effect["claim_version"] == 1
    with case.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM external_effects WHERE account_id=%s", (case.account,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM command_audit_log WHERE account_id=%s", (case.account,))
        assert cur.fetchone()[0] == 0


def test_nominal_provider_success_with_host_unknown_keeps_required_original_id_missing(
    workflow_case,
):
    case = workflow_case
    case.nominal_provider_success = True
    case.partial_id = "unverified-nominal-success-id"
    state = _start(case, policy="continue", required_output=True)
    update = execute_step_node(state)
    assert update["agent_messages"][-1]["status"] == "fail"
    assert not update.get("record_id")
    assert not (update.get("workflow_data") or {}).get("record_id")
    invocation = update["agent_messages"][-1]["artifacts"]["connector_invocation"]
    assert invocation["status"] == "failed"
    assert case.partial_id not in str(invocation)
    state, command = _hub(_apply(state, update))
    assert command.goto == "orchestrator_validation"
    assert state["unresolved_step_failure"]["missing"] == ["record_id"]
    assert not state.get("recovery_pending")
    assert _effect(case)["status"] == "timeout_unknown"
    assert len(case.provider_calls) == 1


# Ordinary author graph: a real submission pin, existing gate/decision transaction,
# existing restart bridges, and the normal provider claim. No resolution ref is
# constructed by the fixture; it can only come from the persisted decision outbox.
def _start_authored_cycle(
    case,
    *,
    return_outcome="rejected",
    state_based=False,
    other_target="done",
    manual_autonomy=False,
    container_kind=None,
):
    from core.infrastructure.db.psycopg_runtime._connection import ConnectionAdapter

    case.ordinary_cycle = True
    case.store = SimpleNamespace(
        conn=ConnectionAdapter(case.conn), cursor=case.conn.cursor, log_event=lambda **kwargs: None
    )
    case.extension["compiled_manifest"]["operation_manifest"][0]["outputs"] = [
        {"name": "record_id", "value_type": "text", "required": True}
    ]
    case.extension["action_definitions"].append(
        {"action_id": "consume_record", "handler": {"mode": "extension"}}
    )
    case.extension["compiled_manifest"]["operation_manifest"].append(
        {
            "operation_id": "consume_record",
            "operation_kind": "source",
            "inputs": [{"name": "record_id", "value_type": "text", "required": True}],
            "outputs": [{"name": "record_id", "value_type": "text", "required": True}],
        }
    )
    definition = copy.deepcopy(DEFINITION)
    definition["steps"][0].update(
        connector_install_id=case.install,
        input_mapping_jsonb={"record_id": "state.business_record_key"},
        output_mapping_jsonb={"record_id": "state.record_id"},
        error_policy={"on_error": "route_to_step"},
    )
    if manual_autonomy:
        definition["steps"][0]["autonomy"] = {"mode": "manual"}
    definition["steps"][1] = {
        "step_id": "review",
        "step_kind": "approval",
        "override": {"approval": {"message": "Choose whether to retry this uncertain write."}},
    }
    definition["steps"].insert(
        2,
        {
            "step_id": "consume",
            "step_kind": "connector",
            "connector_id": "example.host-fixture.consume_record",
            "connector_install_id": case.install,
            "input_mapping_jsonb": {"record_id": "state.record_id"},
            "boundary_contract": {
                "entry": [{"name": "record_id", "source": "state.record_id", "required": True}]
            },
        },
    )
    definition["execution_graph"]["edges"] = [
        edge("publish", node("consume")),
        # Approved is not an eligible marked rework source; mark the existing
        # error edge for an approved-return cycle instead of broadening validation.
        edge("publish", node("review"), "error", rework=return_outcome == "approved"),
        edge("review", node("publish"), return_outcome, rework=return_outcome == "rejected"),
        edge(
            "review", node(other_target), "approved" if return_outcome == "rejected" else "rejected"
        ),
        edge("consume", node("done")),
    ]
    if container_kind:
        assert container_kind in {"group", "for_each"}
        owner_key = f"{container_kind}_id"
        for item in definition["steps"]:
            if item["step_id"] != "done":
                item[owner_key] = "retry_body"
        exit_ref = {
            "kind": f"{container_kind}_exit",
            owner_key: "retry_body",
            "lane_id": None,
        }
        for item in definition["execution_graph"]["edges"]:
            if item["target"] == node("done"):
                item["target"] = dict(exit_ref)
        container = {
            owner_key: "retry_body",
            "body": {"kind": "sequence", "entry": node("publish")},
        }
        if container_kind == "for_each":
            container.update(over="state.items", item_name="current_item")
        definition["groups" if container_kind == "group" else "for_each"] = [container]
        definition["execution_graph"]["entry"] = node("retry_body", container_kind)
        definition["execution_graph"]["edges"].append(
            edge("retry_body", node("done"), source_kind=container_kind)
        )
    workflow_id, registry_id, snapshot_id = f"wf-authored-{uuid4().hex}", str(uuid4()), str(uuid4())
    definition["workflow_id"] = workflow_id
    digest = definition_digest(definition)
    with case.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO registry_workflows (id,account_id,workflow_key,display_name,settings) VALUES (%s,%s,%s,'Authored retry',%s::jsonb)",
            (
                registry_id,
                case.account,
                workflow_id,
                json.dumps({"execution_graph": definition["execution_graph"]}),
            ),
        )
        cur.execute(
            "INSERT INTO workflow_definition_snapshots (id,account_id,workflow_registry_id,workflow_id,revision,digest,schema_version,definition_jsonb) VALUES (%s,%s,%s,%s,1,%s,1,%s::jsonb)",
            (snapshot_id, case.account, registry_id, workflow_id, digest, json.dumps(definition)),
        )
        cur.execute(
            "UPDATE jobs SET selected_workflow=%s,workflow_definition_snapshot_id=%s,meta=%s::jsonb WHERE id=%s",
            (
                workflow_id,
                snapshot_id,
                json.dumps(
                    {
                        "workflow_definition_ref": {"revision": 1, "digest": digest},
                        "workflow_test_run": state_based,
                    }
                ),
                case.job,
            ),
        )
    case.workflow_registry_ids.append(registry_id)
    case.pinned_definition = definition
    case.workflow_id = workflow_id
    ref = {"snapshot_id": snapshot_id, "revision": 1, "digest": digest}
    prime_run_definition(ref, definition)
    state = _run()
    state.update(
        workflow_definition_ref=ref,
        workflow_id=workflow_id,
        account_id=case.account,
        project_id=case.project,
        workspace_id=case.workspace,
        job_id=case.job,
        thread_id=case.thread,
        task_source="workflow_test_run" if state_based else "manual",
        business_record_key="record-a",
    )
    state["trusted_runtime_context"].update(
        workflow_id=workflow_id,
        account_id=case.account,
        project_id=case.project,
        workspace_id=case.workspace,
        job_id=case.job,
    )
    if container_kind == "for_each":
        state["items"] = [{"id": "one"}]
    state, command = _hub(state)
    assert command.goto == ("human_approval_gate" if manual_autonomy else "execute_step")
    assert state["current_step_id"] == "publish"
    return state


def _ordinary_gate(case, state, *, has_existing_record=False):
    state, command = _ambiguity(case, state, producer_errors=True)
    assert command.goto == "human_approval_gate"
    assert state["current_step_id"] == "review"
    assert not state.get("recovery_pending")
    if state.get("task_source") == "workflow_test_run":
        state = _apply(state, _registry_approval_gate_node(state).update)
    payload = dict(_resolve_gate(state).payload)
    assert payload["gate_kind"] == "approval"
    assert not payload.get("recovery_kind")
    for private in (
        "external_effect_resolution_ref",
        "external_effect",
        "workflow_definition_ref",
        "run_cursor",
    ):
        assert private not in payload
        assert private not in payload.get("gate_meta", {})
    assert not payload.get("comment_required_on_approve")
    assert not payload.get("comment_required_on_reject")
    if not has_existing_record:
        assert not state.get("record_id")
    return state


def _ordinary_decision(
    case,
    state,
    monkeypatch,
    *,
    approved,
    action=None,
    comment="",
    scope=None,
    decision_id=None,
    fail_history=False,
    connection=None,
    external_actor=None,
    token_principal=False,
):
    from core.application.management.command_services_approval import ApprovalCommandService

    payload = dict(_resolve_gate(state).payload)
    host_gate_key = payload.get("approval_id") or (state.get("recovery_pending") or {}).get(
        "request_id"
    )
    assert host_gate_key
    interrupt_id = case.interrupt_ids.setdefault(host_gate_key, str(uuid4()))
    snapshot = SimpleNamespace(
        values=state,
        tasks=[]
        if state.get("task_source") == "workflow_test_run"
        else [SimpleNamespace(interrupts=[SimpleNamespace(value=payload, id=interrupt_id)])],
    )
    service = ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        get_logger=lambda: case.store,
        enqueue_workflow_resume=lambda **kwargs: None,
        workflow_resume_repository_factory=lambda _bl: None,
        get_thread_state_snapshot=lambda thread: snapshot if thread == case.thread else None,
    )

    def marker(**kwargs):
        if fail_history:
            raise RuntimeError("history projection failed")

    monkeypatch.setattr(service, "_record_in_progress_marker", marker)
    service._claim_and_record_decision(
        bl=SimpleNamespace(conn=connection or case.store.conn),
        thread_id=case.thread,
        scope=scope
        or ScopeContext(
            account_id=case.account,
            project_id=case.project,
            actor_id="actual-operator",
            principal_type="user",
        ),
        decision=ApprovalDecisionInput(
            approval_id=decision_id or payload.get("approval_id") or interrupt_id,
            approved=approved,
            action=action or ("approve" if approved else "reject"),
            comment=comment,
            external_actor=external_actor,
        ),
        token_principal=token_principal,
        idempotency_key="",
    )
    with case.conn.cursor() as cur:
        cur.execute("SELECT meta FROM jobs WHERE id=%s", (case.job,))
        return cur.fetchone()[0]["workflow_resume"]["resume_value"]


def _ordinary_resume(state, resume, bridge, monkeypatch):
    if bridge == "live":
        return _bridge(state, resume, "live", monkeypatch)
    state = _bridge(state, resume, "state", monkeypatch)
    state, command = _hub(state)
    assert command.goto == "human_approval_gate"
    return _apply(state, _registry_approval_gate_node(state).update)


def _audit_rows(case):
    with case.conn.cursor() as cur:
        cur.execute(
            "SELECT command_type,payload_jsonb,result_jsonb FROM command_audit_log WHERE account_id=%s ORDER BY created_at,command_id",
            (case.account,),
        )
        return cur.fetchall()


@pytest.mark.parametrize("bridge", ["live", "state"])
@pytest.mark.parametrize("return_outcome", ["approved", "rejected"])
def test_ordinary_approval_direct_return_retries_exact_unknown_once_with_real_result(
    workflow_case, monkeypatch, bridge, return_outcome
):
    case = workflow_case
    state = _ordinary_gate(
        case,
        _start_authored_cycle(case, return_outcome=return_outcome, state_based=bridge == "state"),
    )
    original = _effect(case)
    resume = _ordinary_decision(case, state, monkeypatch, approved=return_outcome == "approved")
    ref = resume.get("external_effect_resolution_ref")
    assert ref, "Ordinary Approval's direct return must persist exact retry authorization"
    authorized = _effect(case)
    assert authorized["status"] == "timeout_unknown"
    assert authorized["status_reason"] not in {
        "operator_confirmed_no_effect",
        "operator_confirmed_success",
    }
    assert authorized["claim_version"] == original["claim_version"] + 1
    assert authorized["claim_token"] != original["claim_token"]
    assert not authorized["external_object_id"]
    with case.conn.cursor() as cur:
        cur.execute("SELECT meta FROM jobs WHERE id=%s", (case.job,))
        public_history = cur.fetchone()[0]["approvals"]
    assert public_history[0]["comment"] == ""
    for private in (
        "external_effect_resolution_ref",
        "external_effect",
        "workflow_definition_ref",
        "run_cursor",
    ):
        assert private not in public_history[0]
    assert all(row[1].get("comment", "") == "" for row in _audit_rows(case))
    assert "operator_confirmed_no_effect" not in str(_audit_rows(case))
    state = _ordinary_resume(state, resume, bridge, monkeypatch)
    state, command = _hub(state)
    assert command.goto == "execute_step"
    assert state["current_step_id"] == "publish"
    assert state["external_effect_resolution_ref"] == ref
    retry_dispatch = copy.deepcopy(state)
    state = _apply(state, execute_step_node(state))
    assert len(case.provider_calls) == 2
    retried = _effect(case)
    assert retried["effect_id"] == original["effect_id"]
    assert retried["source_ref_jsonb"] == original["source_ref_jsonb"]
    assert retried["request_fingerprint"] == original["request_fingerprint"]
    assert retried["claim_version"] == original["claim_version"] + 2
    assert retried["status"] == "succeeded"
    from core.infrastructure.db.external_effect_store import update_external_effect_status

    assert (
        update_external_effect_status(
            case.conn,
            effect_id=str(original["effect_id"]),
            claim_token=original["claim_token"],
            status="failed",
            status_reason="late_original_callback",
        )
        is None
    )
    assert _effect(case)["status"] == "succeeded"
    assert state["record_id"] == case.remote_records["record-a"]["record_id"]
    assert len(case.remote_records) == 1
    assert not state.get("external_effect_resolution_ref")
    execute_step_node(retry_dispatch)
    assert len(case.provider_calls) == 2
    rows = _audit_rows(case)
    assert len(rows) == 2  # one authorization, one actual claim consumption
    assert str(original["effect_id"]) in str(rows)
    state, command = _hub(state)
    assert command.goto == "execute_step"
    assert state["current_step_id"] == "consume"
    state = _apply(state, execute_step_node(state))
    assert case.consumer_calls[0]["input_payload"]["record_id"] == state["record_id"]
    assert state["agent_messages"][-1]["status"] == "pass"
    assert len(case.provider_calls) == 2


@pytest.mark.parametrize("bridge", ["live", "state"])
@pytest.mark.parametrize(
    "approved,action,want_step,want_calls",
    [(True, "reject", "publish", 2), (False, "approve", "done", 1)],
    ids=["effective-approved", "effective-rejected"],
)
def test_ordinary_retry_uses_canonical_approved_value_not_conflicting_action(
    workflow_case, monkeypatch, bridge, approved, action, want_step, want_calls
):
    case = workflow_case
    state = _ordinary_gate(
        case, _start_authored_cycle(case, return_outcome="approved", state_based=bridge == "state")
    )
    original = _effect(case)
    resume = _ordinary_decision(case, state, monkeypatch, approved=approved, action=action)
    assert resume["approved"] is approved
    assert bool(resume.get("external_effect_resolution_ref")) is approved
    state = _ordinary_resume(state, resume, bridge, monkeypatch)
    state, command = _hub(state)
    assert state["current_step_id"] == want_step
    if want_step == "publish":
        assert command.goto == "execute_step"
        state = _apply(state, execute_step_node(state))
    else:
        assert not state.get("external_effect_resolution_ref")
        assert _effect(case)["claim_version"] == original["claim_version"]
        assert _audit_rows(case) == []
    assert len(case.provider_calls) == want_calls


def test_other_ordinary_outcome_does_not_settle_unknown_or_manufacture_original_id(
    workflow_case, monkeypatch
):
    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    state["lookup_record_id"] = "real-independent-lookup-id"
    original = _effect(case)
    resume = _ordinary_decision(case, state, monkeypatch, approved=True)
    assert not resume.get("external_effect_resolution_ref")
    state = _ordinary_resume(state, resume, "live", monkeypatch)
    state, _command = _hub(state)
    assert state["current_step_id"] == "done"
    assert state["lookup_record_id"] == "real-independent-lookup-id"
    assert not state.get("record_id")
    assert _effect(case)["claim_version"] == original["claim_version"]
    assert _effect(case)["status"] == "timeout_unknown"
    assert _audit_rows(case) == []
    assert len(case.provider_calls) == 1


@pytest.mark.parametrize("bridge", ["live", "state"])
@pytest.mark.parametrize("container_kind", ["group", "for_each"])
def test_other_ordinary_outcome_exits_container_without_settling_unknown(
    workflow_case, monkeypatch, container_kind, bridge
):
    case = workflow_case
    state = _ordinary_gate(
        case,
        _start_authored_cycle(case, container_kind=container_kind, state_based=bridge == "state"),
    )
    assert state["run_cursor"]["frames"][-1][f"{container_kind}_id"] == "retry_body"
    original = _effect(case)
    resume = _ordinary_decision(case, state, monkeypatch, approved=True)
    assert not resume.get("external_effect_resolution_ref")
    assert _audit_rows(case) == []
    state = _ordinary_resume(state, resume, bridge, monkeypatch)
    state, _command = _hub(state)
    assert state["current_step_id"] == "done"
    assert state["run_cursor"]["frames"] == []
    assert not state.get("external_effect_resolution_ref")
    assert not state.get("record_id")
    assert _effect(case)["status"] == "timeout_unknown"
    assert _effect(case)["claim_token"] == original["claim_token"]
    assert _effect(case)["claim_version"] == original["claim_version"]
    assert _audit_rows(case) == []
    assert len(case.provider_calls) == 1
    assert not case.consumer_calls
    assert not case.remote_records


def test_ordinary_return_refuses_exhausted_rework_before_any_fence_or_receipt(
    workflow_case, monkeypatch
):
    from core.application.management.errors import ServiceConflictError
    from core.application.orchestration.run_cursor import (
        TRANSITION_ESCALATE,
        Outcome,
        RunCursor,
        resolve_after_step,
    )
    from core.application.orchestration.run_definition import load_run_definition

    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    state["run_cursor"]["rework"].update(used=1, allowance=1)
    transition = resolve_after_step(
        load_run_definition(state),
        RunCursor.load(copy.deepcopy(state["run_cursor"])),
        Outcome("rejected"),
    )
    assert transition.kind == TRANSITION_ESCALATE
    original = _effect(case)
    with pytest.raises(ServiceConflictError):
        _ordinary_decision(case, state, monkeypatch, approved=False)
    assert _effect(case)["claim_token"] == original["claim_token"]
    assert _audit_rows(case) == []
    with case.conn.cursor() as cur:
        cur.execute("SELECT meta FROM jobs WHERE id=%s", (case.job,))
        meta = cur.fetchone()[0]
    assert not meta.get("approvals")
    assert not meta.get("workflow_resume")
    assert len(case.provider_calls) == 1


@pytest.mark.parametrize("damage", ["pin", "execution", "attempt", "job_pin_removed"])
def test_ordinary_retry_refuses_wrong_job_pin_or_current_gate_execution(
    workflow_case, monkeypatch, damage
):
    from core.application.management.errors import (
        ServiceConflictError,
        ServiceUnavailableError,
        ServiceValidationError,
    )

    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    original = _effect(case)
    if damage == "pin":
        state["workflow_definition_ref"] = {
            **state["workflow_definition_ref"],
            "snapshot_id": str(uuid4()),
        }
        prime_run_definition(state["workflow_definition_ref"], case.pinned_definition)
    elif damage == "execution":
        state["run_cursor"]["step_execution_id"] = str(uuid4())
    elif damage == "attempt":
        state["run_cursor"]["attempt"] += 1
    else:
        with case.conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET workflow_definition_snapshot_id=NULL WHERE id=%s", (case.job,)
            )
    with pytest.raises((ServiceConflictError, ServiceValidationError, ServiceUnavailableError)):
        _ordinary_decision(case, state, monkeypatch, approved=False)
    assert _effect(case)["claim_token"] == original["claim_token"]
    assert _audit_rows(case) == []
    assert len(case.provider_calls) == 1


def test_ordinary_retry_uses_job_archive_even_if_live_registry_graph_changes(
    workflow_case, monkeypatch
):
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
    )

    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    before = WorkflowDefinitionSnapshotRepository(case.store.conn).load_for_job(case.job)
    assert str(before["id"]) == state["workflow_definition_ref"]["snapshot_id"]
    with case.conn.cursor() as cur:
        cur.execute(
            "UPDATE registry_workflows SET revision=2,settings=%s::jsonb WHERE workflow_id=%s",
            (
                json.dumps(
                    {"execution_graph": {"schema_version": 1, "entry": node("done"), "edges": []}}
                ),
                case.workflow_id,
            ),
        )
    resume = _ordinary_decision(case, state, monkeypatch, approved=False)
    assert resume.get("external_effect_resolution_ref")
    after = WorkflowDefinitionSnapshotRepository(case.store.conn).load_for_job(case.job)
    assert before == after
    state = _ordinary_resume(state, resume, "live", monkeypatch)
    state, _command = _hub(state)
    state = _apply(state, execute_step_node(state))
    assert len(case.provider_calls) == 2
    assert state["record_id"] == case.remote_records["record-a"]["record_id"]


@pytest.mark.parametrize(
    "change",
    [
        "payload",
        "install",
        "removed_declaration",
        "changed_declaration",
        "malformed_ref",
        "scalar_ref",
        "empty_ref",
    ],
)
def test_bound_ordinary_retry_fails_closed_before_provider_on_changed_request_or_release(
    workflow_case, monkeypatch, change
):
    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    resume = _ordinary_decision(case, state, monkeypatch, approved=False)
    assert resume.get("external_effect_resolution_ref")
    state = _ordinary_resume(state, resume, "live", monkeypatch)
    state, _command = _hub(state)
    fence = _effect(case)
    if change == "payload":
        # The same pinned mapping now renders a different provider-visible value.
        state["business_record_key"] = "different-provider-payload"
    elif change == "install":
        case.extension["extension_install_id"] = "replacement-install"
    elif change == "removed_declaration":
        case.extension["compiled_manifest"]["external_effects"] = []
    elif change == "changed_declaration":
        case.extension["compiled_manifest"]["external_effects"][0]["channel"] = "email"
    else:
        state["external_effect_resolution_ref"] = (
            "corrupt-issued-ref"
            if change == "scalar_ref"
            else {}
            if change == "empty_ref"
            else {
                "effect_id": str(fence["effect_id"]),
                "phase": "association",
            }
        )
        case.extension["compiled_manifest"]["external_effects"] = []
    update = execute_step_node(state)
    assert update["agent_messages"][-1]["status"] == "fail"
    assert len(case.provider_calls) == 1
    assert _effect(case)["claim_token"] == fence["claim_token"]
    assert len(_audit_rows(case)) == 1


@pytest.mark.parametrize("change", ["removed_declaration", "changed_declaration", "install"])
def test_rejected_bound_retry_is_terminal_before_ordinary_failure_continuation(
    workflow_case, monkeypatch, change
):
    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    resume = _ordinary_decision(case, state, monkeypatch, approved=False)
    state = _ordinary_resume(state, resume, "live", monkeypatch)
    state, _ = _hub(state)
    fence = _effect(case)
    if change == "removed_declaration":
        case.extension["compiled_manifest"]["external_effects"] = []
    elif change == "changed_declaration":
        case.extension["compiled_manifest"]["external_effects"][0]["channel"] = "email"
    else:
        case.extension["extension_install_id"] = "replacement-install"
    blocked = execute_step_node(state)
    assert blocked["agent_messages"][-1]["status"] == "fail"
    assert len(case.provider_calls) == 1
    assert _effect(case)["claim_token"] == fence["claim_token"]
    assert len(_audit_rows(case)) == 1
    state = _apply(state, blocked)
    state, command = _hub(state)
    # Regeneration/error routing must never turn this refused authorization
    # into an unbound invocation, even if the current declaration disappeared.
    assert command.goto == "orchestrator_validation"
    assert state["status"] == "failed"
    assert not state.get("recovery_pending")
    assert not state.get("external_effect_resolution_ref")
    assert len(case.provider_calls) == 1
    assert _effect(case)["claim_token"] == fence["claim_token"]
    assert len(_audit_rows(case)) == 1


def test_predecision_association_is_never_provider_retry_authority(workflow_case):
    case = workflow_case
    initial = _start_authored_cycle(case)
    state = _ordinary_gate(case, initial)
    association = state.get("external_effect_resolution_ref")
    assert isinstance(association, dict)
    assert association.get("phase")
    assert _audit_rows(case) == []
    target = copy.deepcopy(initial)
    target["external_effect_resolution_ref"] = copy.deepcopy(association)
    case.extension["compiled_manifest"]["external_effects"] = []
    result = execute_step_node(target)
    assert result["agent_messages"][-1]["status"] == "fail"
    assert len(case.provider_calls) == 1
    assert _effect(case)["claim_version"] == 1


def test_duplicate_ordinary_decisions_cannot_issue_two_retry_authorizations(
    workflow_case, monkeypatch
):
    from core.application.management.errors import ServiceConflictError
    from core.infrastructure.db import psycopg_runtime

    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))

    def decide():
        with psycopg_runtime.connect(case.dsn, autocommit=True) as conn:
            try:
                return _ordinary_decision(
                    case, copy.deepcopy(state), monkeypatch, approved=False, connection=conn
                )
            except ServiceConflictError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _number: decide(), range(2)))
    accepted = [value for value in results if value is not None]
    assert len(accepted) == 1
    assert accepted[0].get("external_effect_resolution_ref")
    assert len(_audit_rows(case)) == 1
    assert len(case.provider_calls) == 1


def test_ordinary_retry_history_failure_rolls_back_neutral_fence(workflow_case, monkeypatch):
    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    original = _effect(case)
    with pytest.raises(RuntimeError, match="history projection failed"):
        _ordinary_decision(case, state, monkeypatch, approved=False, fail_history=True)
    assert _effect(case)["claim_token"] == original["claim_token"]
    assert _audit_rows(case) == []


@pytest.mark.parametrize("wrong", ["account", "project", "actor", "gate"])
def test_ordinary_retry_keeps_existing_actor_scope_and_stale_gate_checks(
    workflow_case, monkeypatch, wrong
):
    from core.application.management.errors import (
        ServiceConflictError,
        ServiceNotFoundError,
        ServiceValidationError,
    )

    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    original = _effect(case)
    scope = ScopeContext(
        account_id=str(uuid4()) if wrong == "account" else case.account,
        project_id=str(uuid4()) if wrong == "project" else case.project,
        actor_id=None if wrong == "actor" else "actual-operator",
        principal_type="user",
    )
    with pytest.raises((ServiceConflictError, ServiceNotFoundError, ServiceValidationError)):
        _ordinary_decision(
            case,
            state,
            monkeypatch,
            approved=False,
            scope=scope,
            decision_id="old-gate" if wrong == "gate" else None,
        )
    assert _effect(case)["claim_token"] == original["claim_token"]
    assert _audit_rows(case) == []
    assert len(case.provider_calls) == 1


def test_neutral_authorization_is_pending_not_a_fresh_factual_operator_resolution(
    workflow_case, monkeypatch
):
    from core.application.extensions.external_effect_resolution import (
        ExternalEffectResolutionError,
        resolve_external_effect,
    )

    case = workflow_case
    state = _ordinary_gate(case, _start_authored_cycle(case))
    resume = _ordinary_decision(case, state, monkeypatch, approved=False)
    assert resume.get("external_effect_resolution_ref")
    pending = _effect(case)
    with pytest.raises(ExternalEffectResolutionError, match="already_resolved"):
        resolve_external_effect(
            case.store.conn,
            account_id=case.account,
            project_id=case.project,
            effect_id=str(pending["effect_id"]),
            observed_version=pending["claim_version"],
            action="reject",
            comment="A different client cannot turn pending retry into a new ambiguity.",
            actor_id="another-operator",
            approval_id="other-gate",
            expected_source_ref=pending["source_ref_jsonb"],
        )
    assert _effect(case)["claim_token"] == pending["claim_token"]
    assert len(_audit_rows(case)) == 1
    assert len(case.provider_calls) == 1


@pytest.mark.parametrize("approved", [True, False])
def test_legacy_explicit_await_unknown_stays_generic_without_retry_authority(
    workflow_case, monkeypatch, approved
):
    case = workflow_case
    state, command = _ambiguity(case, _start(case))
    assert command.goto == "human_approval_gate"
    assert state["recovery_pending"]["kind"] == "failure"
    assert not state.get("external_effect_resolution_ref")
    payload = _resolve_gate(state).payload
    assert payload.get("recovery_kind") != "external_effect_unknown"
    original = _effect(case)
    resume = _ordinary_decision(case, state, monkeypatch, approved=approved)
    assert not resume.get("external_effect_resolution_ref")
    assert _effect(case)["claim_token"] == original["claim_token"]
    assert _effect(case)["status"] == "timeout_unknown"
    assert _audit_rows(case) == []
    assert len(case.provider_calls) == 1


def _park_authored_cycle_job(case):
    with case.conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status='awaiting_approval' WHERE id=%s", (case.job,))


def _manual_issued_retry_pause(case, monkeypatch):
    state = _start_authored_cycle(case, return_outcome="approved", manual_autonomy=True)
    assert state["task_source"] == "manual"
    assert not state.get("test_mode")
    assert not state["trusted_runtime_context"].get("test_mode")
    first_pending = copy.deepcopy(state["recovery_pending"])
    assert first_pending["kind"] == "autonomy"
    assert not case.provider_calls
    initial = _ordinary_decision(case, state, monkeypatch, approved=True)
    assert not initial.get("external_effect_resolution_ref")
    state = _bridge(state, initial, "live", monkeypatch)
    state, command = _hub(state)
    assert command.goto == "execute_step"
    state = _ordinary_gate(case, state)
    _park_authored_cycle_job(case)
    resume = _ordinary_decision(case, state, monkeypatch, approved=True)
    issued = resume["external_effect_resolution_ref"]
    assert _effect(case)["claim_version"] == 2
    assert len(_audit_rows(case)) == 1
    state = _bridge(state, resume, "live", monkeypatch)
    state, command = _hub(state)
    assert command.goto == "human_approval_gate"
    pending = state["recovery_pending"]
    assert pending["kind"] == "autonomy"
    assert pending["request_id"] != first_pending["request_id"]
    assert pending["step_execution_id"] != first_pending["step_execution_id"]
    assert pending["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert pending["attempt"] == state["run_cursor"]["attempt"]
    assert state["external_effect_resolution_ref"] == issued
    assert len(case.provider_calls) == 1
    assert not case.remote_records
    return state, issued


@pytest.mark.parametrize("bridge", ["live", "state"])
@pytest.mark.parametrize("approved", [True, False])
def test_manual_autonomy_preserves_same_issued_retry_until_one_real_claim(
    workflow_case, monkeypatch, bridge, approved
):
    case = workflow_case
    state, issued = _manual_issued_retry_pause(case, monkeypatch)
    _park_authored_cycle_job(case)
    resumed = _ordinary_decision(
        case,
        state,
        monkeypatch,
        approved=approved,
        comment="" if approved else "Reviewed; retain existing autonomy feedback semantics.",
    )
    assert resumed["external_effect_resolution_ref"] == issued
    with case.conn.cursor() as cur:
        cur.execute("SELECT meta FROM jobs WHERE id=%s", (case.job,))
        history = cur.fetchone()[0]["approvals"]
    assert len(history) == 3
    assert history[-1]["approval_id"] != issued["approval_id"]
    assert history[-1]["approval_id"] == case.interrupt_ids[state["recovery_pending"]["request_id"]]
    assert history[-1]["approval_id"] != state["recovery_pending"]["request_id"]
    assert len(_audit_rows(case)) == 1  # Autonomy never issues another authorization.
    assert _effect(case)["claim_version"] == 2
    state = _bridge(state, resumed, bridge, monkeypatch)
    state, command = _hub(state)
    assert command.goto == "execute_step"
    assert state["external_effect_resolution_ref"] == issued
    retry_dispatch = copy.deepcopy(state)
    state = _apply(state, execute_step_node(state))
    assert len(case.provider_calls) == 2
    assert len(case.remote_records) == 1
    assert state["record_id"] == case.remote_records["record-a"]["record_id"]
    assert _effect(case)["status"] == "succeeded"
    assert _effect(case)["claim_version"] == 3
    assert len(_audit_rows(case)) == 2  # One authorization and one consumption.
    execute_step_node(retry_dispatch)
    assert len(case.provider_calls) == 2
    assert len(_audit_rows(case)) == 2


@pytest.mark.parametrize("changed", ["target", "execution", "attempt", "old_authored_gate"])
def test_manual_autonomy_refuses_stale_issued_retry_without_new_grant(
    workflow_case, monkeypatch, changed
):
    from core.application.management.errors import ServiceConflictError

    case = workflow_case
    state, issued = _manual_issued_retry_pause(case, monkeypatch)
    _park_authored_cycle_job(case)
    decision_id = None
    if changed == "target":
        state["recovery_pending"]["step_id"] = "consume"
    elif changed == "execution":
        state["recovery_pending"]["step_execution_id"] = str(uuid4())
    elif changed == "attempt":
        state["recovery_pending"]["attempt"] += 1
    else:
        decision_id = issued["approval_id"]
    before = _effect(case)
    with pytest.raises(ServiceConflictError):
        _ordinary_decision(case, state, monkeypatch, approved=True, decision_id=decision_id)
    assert _effect(case) == before
    assert len(_audit_rows(case)) == 1
    assert len(case.provider_calls) == 1
    assert not case.remote_records
