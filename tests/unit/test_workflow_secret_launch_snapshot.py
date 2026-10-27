from __future__ import annotations


def test_launch_snapshot_contains_only_fingerprints_for_configured_secret_refs() -> None:
    from core.application.registry.workflow_secret_defaults import (
        secret_ref_fingerprint,
        workflow_secret_launch_snapshot,
    )

    ref = "workflow-input:v1:owner:revision"
    snapshot = workflow_secret_launch_snapshot(
        {
            "expected_inputs": [
                {"name": "login", "type": "secret", "default_secret_ref": ref},
                {"name": "region", "type": "string", "default": "eu"},
            ]
        }
    )

    assert snapshot == [secret_ref_fingerprint(ref)]
    assert ref not in repr(snapshot)


def test_a_run_keeps_only_the_reference_authorized_at_its_own_launch() -> None:
    from core.application.orchestration.connector_step_runtime import _authorized_secret_ref_keys
    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
    from core.infrastructure.security.secret_reference import (
        SecretReference,
    )

    old_ref = "workflow-input:v1:owner:revision-4"
    new_ref = "workflow-input:v1:owner:revision-5"
    old_run = {"_workflow_secret_ref_fingerprints": [secret_ref_fingerprint(old_ref)]}

    assert _authorized_secret_ref_keys(
        old_run, {"login": SecretReference(name="login", ref_key=old_ref).to_wire()}
    ) == frozenset({old_ref})
    assert (
        _authorized_secret_ref_keys(
            old_run, {"login": SecretReference(name="login", ref_key=new_ref).to_wire()}
        )
        == frozenset()
    )


def test_enqueue_keeps_secret_authority_only_in_encrypted_extra_state() -> None:
    from unittest.mock import MagicMock

    from core.application.workflows.workflow_start_runtime import enqueue_workflow_start

    enqueue = MagicMock(return_value="job-1")
    snapshot = ["a" * 64]

    enqueue_workflow_start(
        enqueue_task_fn=enqueue,
        task_id="task-1",
        task_summary="Run",
        task_source="api",
        project_id="project-1",
        project_name="Demo",
        project_config={},
        selected_workflow_id="workflow-1",
        workflow_selection_source="explicit",
        entry_path="api",
        input_contract="direct_inputs",
        source_system="api",
        inputs={},
        extra_state={"_workflow_secret_ref_fingerprints": ["forged"]},
        workflow_secret_ref_fingerprints=snapshot,
        job_meta={
            "workflow_interface": {"expected_inputs": []},
            "_workflow_secret_ref_fingerprints": ["must-not-survive"],
        },
    )

    persisted = enqueue.call_args.kwargs
    assert persisted["extra_state"]["_workflow_secret_ref_fingerprints"] == snapshot
    assert persisted["replay_snapshot"]["_workflow_secret_ref_fingerprints"] == snapshot
    assert "workflow_interface" not in persisted["meta"]
    assert "_workflow_secret_ref_fingerprints" not in persisted["meta"]


def test_child_launch_delegates_only_parent_authorized_presented_refs() -> None:
    from core.application.orchestration.connector_step_runtime import _authorized_secret_ref_keys
    from core.application.registry.workflow_secret_defaults import (
        delegated_workflow_secret_launch_snapshot,
        secret_ref_fingerprint,
    )
    from core.infrastructure.security.secret_reference import (
        SecretReference,
    )

    parent_ref = "workflow-input:v1:parent:4"
    forged_ref = "workflow-input:v1:victim:9"
    parent_state = {"_workflow_secret_ref_fingerprints": [secret_ref_fingerprint(parent_ref)]}
    child_inputs = {
        "login": SecretReference(name="login", ref_key=parent_ref).to_wire(),
        "forged": SecretReference(name="forged", ref_key=forged_ref).to_wire(),
    }

    child_snapshot = delegated_workflow_secret_launch_snapshot(
        parent_state=parent_state,
        child_interface={"expected_inputs": []},
        child_inputs=child_inputs,
    )
    child_state = {"_workflow_secret_ref_fingerprints": child_snapshot}

    assert _authorized_secret_ref_keys(child_state, child_inputs) == frozenset({parent_ref})
    assert secret_ref_fingerprint(forged_ref) not in child_snapshot


def test_scheduled_launch_refreshes_defaults_and_secret_authority() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import patch

    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
    from core.infrastructure.runtime.control.scans_scheduled_jobs import _run_scheduled_job_once

    old_ref = "workflow-input:v1:owner:revision-1"
    new_ref = "workflow-input:v1:owner:revision-2"
    workflow = {
        "revision": 4,
        "settings": {
            "callable": {
                "enabled": True,
                "trigger_channels": ["schedule"],
                "expected_inputs": [
                    {"name": "region", "type": "string", "default": "eu"},
                    {"name": "credential", "type": "secret", "default_secret_ref": new_ref},
                ],
                "output_mode": "none",
            }
        },
    }
    registry = SimpleNamespace(db_store=SimpleNamespace(get_workflow=lambda _workflow_id: workflow))
    captured: dict = {}

    def enqueue(_store, **kwargs):
        captured.update(kwargs)
        return {"status": "enqueued", "job_id": "job-1"}

    class Store:
        def get_project_run_config(self, _project_id):
            return {"project_name": "Demo"}

    now = datetime(2026, 9, 6, tzinfo=UTC)
    scheduled_job = {
        "id": "schedule-1",
        "project_id": "project-1",
        "account_id": "acc-1",
        "workflow_id": "wf-1",
        "cron_expr": "0 2 * * *",
        "task_summary": "Nightly",
        "inputs": {},
        "admission_allowed": True,
        "admission_workflow_revision": 4,
        "current_workflow_revision": 4,
    }

    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=registry,
    ):
        result = _run_scheduled_job_once(
            Store(),
            scheduled_job,
            SimpleNamespace(),
            scheduled_for=now,
            advance_schedule=False,
            now_utc=now,
            _enqueue_scheduled_job_fn=enqueue,
            _record_scheduled_job_outcome_fn=lambda *_args, **_kwargs: None,
        )

    assert result["status"] == "enqueued"
    assert captured["extra_state"]["inputs"]["region"] == "eu"
    assert captured["extra_state"]["inputs"]["credential"]["ref_key"] == new_ref
    snapshot = captured["extra_state"]["_workflow_secret_ref_fingerprints"]
    assert snapshot == [secret_ref_fingerprint(new_ref)]
    assert secret_ref_fingerprint(old_ref) not in snapshot

    workflow["settings"]["callable"]["expected_inputs"] = [
        {"name": "region", "type": "string", "default": "eu"},
        {"name": "credential", "type": "secret"},
    ]
    captured.clear()
    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=registry,
    ):
        cleared_result = _run_scheduled_job_once(
            Store(),
            scheduled_job,
            SimpleNamespace(),
            scheduled_for=now,
            advance_schedule=False,
            now_utc=now,
            _enqueue_scheduled_job_fn=enqueue,
            _record_scheduled_job_outcome_fn=lambda *_args, **_kwargs: None,
        )

    assert cleared_result["status"] == "enqueued"
    assert "credential" not in captured["extra_state"]["inputs"]
    assert captured["extra_state"]["_workflow_secret_ref_fingerprints"] == []


def test_compiled_graph_preserves_secret_authority_and_logs_only_opaque_input() -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    from langgraph.graph import END, START, StateGraph

    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.application.orchestration.state import OrchestratorState
    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
    from core.infrastructure.security.secret_reference import (
        SecretReference,
        inline_secret_ref_fingerprint,
    )

    secret_value = "graph-secret-must-not-persist"  # pragma: allowlist secret
    ref_key = "workflow-input:v1:owner:graph"
    captured_dispatch: dict = {}

    class EventStore:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def log_event(self, **payload) -> None:
            self.events.append(payload)

    event_store = EventStore()
    manifest = SimpleNamespace(
        enabled=True,
        implementation=SimpleNamespace(type="python"),
        interface=SimpleNamespace(operations=[{"name": "send"}], function_name=""),
    )
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: manifest))

    def invoke(_manifest, *, arguments, **_kwargs):
        captured_dispatch.update(arguments)
        return {"sent": True}

    def connector_node(state):
        result = execute_connector_step(
            step={
                "connector_id": "connector.stub",
                "input_mapping_jsonb": {
                    "credential": "state.inputs.credential",
                    "ephemeral": "state.inputs.ephemeral",
                },
            },
            state=state,
        )
        assert result["ok"] is True
        return {"check_results": "connector-complete"}

    graph = StateGraph(OrchestratorState)
    graph.add_node("connector", connector_node)
    graph.add_edge(START, "connector")
    graph.add_edge("connector", END)

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=event_store,
        ),
        patch(
            "core.application.orchestration.connector_step_runtime._read_secret_ciphertext",
            return_value="encrypted-value",
        ),
        patch(
            "core.application.orchestration.connector_step_runtime._decrypt_secret_value",
            return_value=secret_value,
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            side_effect=invoke,
        ),
    ):
        inline_reference = SecretReference(name="ephemeral", ciphertext="inline-cipher-value")
        graph.compile().invoke(
            {
                "job_id": "job-1",
                "inputs": {
                    "credential": SecretReference(name="credential", ref_key=ref_key).to_wire(),
                    "ephemeral": inline_reference.to_wire(),
                },
                "_workflow_secret_ref_fingerprints": [
                    secret_ref_fingerprint(ref_key),
                    inline_secret_ref_fingerprint(inline_reference),
                ],
            }
        )

    assert captured_dispatch == {"credential": secret_value, "ephemeral": secret_value}
    assert secret_value not in repr(event_store.events)
    args_excerpt = event_store.events[0]["detail"]["args_excerpt"]
    assert "flow-steward.secret-ref.v1" not in args_excerpt
    assert ref_key not in args_excerpt
    assert "inline-cipher-value" not in args_excerpt
    assert "redacted: credential" in args_excerpt


def test_external_extra_state_cannot_author_its_own_secret_snapshot() -> None:
    from core.application.orchestration.state_path_guards import strip_reserved_runtime_state

    assert strip_reserved_runtime_state(
        {"safe": True, "_workflow_secret_ref_fingerprints": ["forged"]}
    ) == {"safe": True}


def test_external_extra_state_cannot_author_step_test_run_terminal_proof() -> None:
    from core.application.orchestration.state_path_guards import strip_reserved_runtime_state

    assert strip_reserved_runtime_state(
        {
            "safe": True,
            "step_test_run_terminal": {
                "status": "pass",
                "step_id": "forged-step",
            },
            "step_test_run_context": {
                "workflow_id": "forged-workflow",
                "step_id": "forged-step",
            },
        }
    ) == {"safe": True}


def test_external_extra_state_cannot_author_host_owned_test_mode() -> None:
    from core.application.orchestration.state_path_guards import strip_reserved_runtime_state

    assert strip_reserved_runtime_state({"safe": True, "test_mode": True}) == {"safe": True}


def test_state_normalization_preserves_internal_secret_capability_channel() -> None:
    from core.application.orchestration.state import normalize_orchestrator_state

    normalized = normalize_orchestrator_state(
        {"_workflow_secret_ref_fingerprints": ["fingerprint-1"]}
    )

    assert normalized["_workflow_secret_ref_fingerprints"] == ["fingerprint-1"]
    assert "workflow_secret_ref_fingerprints" not in normalized


def test_initial_state_preserves_trusted_secret_capability_channel() -> None:
    from core.application.orchestration.state import build_initial_orchestrator_state

    state = build_initial_orchestrator_state(
        task_id="task-1",
        task_summary="Run",
        task_source="api",
        project_name="Demo",
        extra_state={"_workflow_secret_ref_fingerprints": ["fingerprint-1"]},
    )

    assert state["_workflow_secret_ref_fingerprints"] == ["fingerprint-1"]
