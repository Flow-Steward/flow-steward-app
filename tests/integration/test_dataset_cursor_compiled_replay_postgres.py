from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def test_compiled_for_each_replay_uses_checkpoint_and_external_effect_ledger(
    disposable_db,
    monkeypatch,
    request,
    tmp_path,
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from core.application.auth.config import (
        DEFAULT_ACCOUNT_ID,
        DEFAULT_TENANT_ID,
        DEFAULT_WORKSPACE_ID,
    )
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.control_flow_step_node import control_flow_step_node
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.orchestration.state import OrchestratorState
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from core.infrastructure.db import psycopg_runtime
    from tests._step_run_state import prime
    from tests._workflow_definitions import edge, node

    conn = psycopg_runtime.connect(disposable_db.dsn, autocommit=True)
    request.addfinalizer(conn.close)
    project_id = str(uuid4())
    job_id = str(uuid4())
    install_id = f"pi_dataset_cursor_{uuid4().hex}"
    release_id = f"pr_dataset_cursor_{uuid4().hex}"
    extension_id = "fixture.dataset-cursor-effect"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, f"dataset-cursor-replay-{project_id[:8]}", DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            "INSERT INTO extension_releases "
            "(release_id, extension_id, release_version, account_id) "
            "VALUES (%s, %s, '1.0.0', %s)",
            (release_id, extension_id, DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            "INSERT INTO extension_installs "
            "(extension_id, account_id, install_id, release_id, descriptor_source) "
            "VALUES (%s, %s, %s, %s, 'extension_scan')",
            (extension_id, DEFAULT_ACCOUNT_ID, install_id, release_id),
        )

    rows = [{"sku": "A"}, {"sku": "B"}]
    plane = ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    )
    dataset, dataset_meta = plane.create_dataset(
        owner_scope={
            "account_id": str(DEFAULT_ACCOUNT_ID),
            "project_id": project_id,
            "workflow_id": "wf_dataset_cursor_replay",
        },
        run_id=job_id,
        step_id="seed-pages",
        rows=rows,
    )
    # For each walks a ready collection, never a cursor it pages through: the
    # dataset rows are read first and each page is one item.
    assert dataset.dataset_id
    assert dataset_meta["row_count"] == len(rows)
    pages = [[row] for row in rows]
    marker_path = tmp_path / "provider-mutations.jsonl"
    fixture_root = (
        Path(__file__).resolve().parents[1] / "fixtures" / "dataset_cursor_effect_provider"
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": extension_id,
        "extension_install_id": install_id,
        "install_id": install_id,
        "release_id": release_id,
        "bundle_root": str(fixture_root),
        "entrypoint": {"command": [sys.executable, "main.py"]},
        "action_definitions": [{"action_id": "send_page", "handler": {"mode": "extension"}}],
        "compiled_manifest": {
            "extension_id": extension_id,
            "entrypoint": {"command": [sys.executable, "main.py"]},
            "operation_manifest": [
                {
                    "operation_id": "send_page",
                    "kind": "action",
                    "outputs": [{"name": "accepted_count", "value_type": "integer"}],
                }
            ],
            "external_effects": [
                {
                    "operation_id": "send_page",
                    "effect_kind": "fixture_delivery",
                    "channel": "local_fixture",
                    "idempotency": "required",
                }
            ],
        },
    }
    # A real For each over the pages: one page per item, the connector step as
    # its body, then an explicit Stop.
    definition = {
        "workflow_id": "wf_dataset_cursor_replay",
        "steps": [
            {
                "step_kind": "connector",
                "step_id": "send-page",
                "group_id": None,
                "for_each_id": "deliver-pages",
                "lane_id": None,
                "connector_id": "fixture.dataset-cursor-effect.send_page",
                "connector_install_id": install_id,
                "input_mapping_jsonb": {
                    "page": "state.current_page",
                    "marker_path": str(marker_path),
                },
            },
            {"step_id": "done", "step_kind": "stop_success", "group_id": None, "lane_id": None},
        ],
        "groups": [],
        "for_each": [
            {
                "for_each_id": "deliver-pages",
                "group_id": None,
                "over": "state.pages",
                "item_name": "current_page",
                "body": {"kind": "sequence", "entry": node("send-page")},
            }
        ],
        "execution_graph": {
            "schema_version": 1,
            "entry": node("deliver-pages", "for_each"),
            "edges": [
                edge(
                    "send-page",
                    {"kind": "for_each_exit", "for_each_id": "deliver-pages", "lane_id": None},
                ),
                edge("deliver-pages", node("done"), source_kind="for_each"),
            ],
        },
        "settings": {},
    }
    registry = SimpleNamespace(
        agents=SimpleNamespace(get_agent=lambda _agent_id: None),
        tools=SimpleNamespace(get_tool=lambda _tool_id: None),
        workflows=SimpleNamespace(get_workflow=lambda _workflow_id: None),
    )
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: registry,
    )
    monkeypatch.setattr(
        "core.infrastructure.tabular_feed.storage.plane_from_state_or_env",
        lambda _state: plane,
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
        lambda *_args, **_kwargs: (extension, conn),
    )

    checkpointer = InMemorySaver()

    def compile_graph(*, interrupt_before: list[str] | None = None):
        builder = StateGraph(OrchestratorState)
        builder.add_node(
            "orchestrator",
            orchestrator_hub_node,
            destinations=("execute_step", "control_flow_step", "orchestrator_validation"),
        )
        builder.add_node("execute_step", execute_step_node)
        builder.add_node("control_flow_step", control_flow_step_node)
        builder.add_node(
            "orchestrator_validation",
            lambda _state: {"status": "completed"},
        )
        builder.add_edge(START, "orchestrator")
        builder.add_edge("execute_step", "orchestrator")
        builder.add_edge("control_flow_step", "orchestrator")
        builder.add_edge("orchestrator_validation", END)
        return builder.compile(
            checkpointer=checkpointer,
            interrupt_before=interrupt_before,
        )

    trusted_scope = {
        "account_id": str(DEFAULT_ACCOUNT_ID),
        "tenant_id": str(DEFAULT_TENANT_ID),
        "workspace_id": str(DEFAULT_WORKSPACE_ID),
        "project_id": project_id,
        "workflow_id": "wf_dataset_cursor_replay",
        "job_id": job_id,
    }
    initial_state = {
        **trusted_scope,
        "trusted_runtime_context": trusted_scope,
        "task_id": "dataset-cursor-replay",
        "task_source": "workflow_test_run",
        "task_envelope": {"task_id": "dataset-cursor-replay"},
        "status": "in_progress",
        "iteration_count": 0,
        "agent_messages": [],
        "orchestrator_journal": [],
        "project_config": trusted_scope,
        "workflow_data": {"pages": pages},
        **prime(copy.deepcopy(definition)),
    }
    original_config = {"configurable": {"thread_id": "dataset-loop-original"}}
    interrupted = compile_graph(interrupt_before=["execute_step"])
    interrupted.invoke(initial_state, original_config)
    checkpoint = interrupted.get_state(original_config)
    assert checkpoint.next == ("execute_step",)
    replay_checkpoint_config = checkpoint.config

    graph = compile_graph()
    original_result = graph.invoke(None, original_config)
    assert original_result["status"] == "completed"
    assert original_result["workflow_terminal"]["kind"] == "stop_success"
    assert original_result["run_cursor"]["frames"] == []
    assert marker_path.read_text(encoding="utf-8").count("\n") == 2

    replay_result = graph.invoke(None, replay_checkpoint_config)
    assert replay_result["status"] == "completed"
    assert replay_result["workflow_terminal"]["kind"] == "stop_success"
    assert marker_path.read_text(encoding="utf-8").count("\n") == 2
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, count(*) FROM external_effects "
            "WHERE project_id = %s AND extension_id = %s GROUP BY status",
            (project_id, "fixture.dataset-cursor-effect"),
        )
        assert cur.fetchall() == [("succeeded", 2)]
