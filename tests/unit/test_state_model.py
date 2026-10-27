from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _import_state_module():
    fake_lc_core = ModuleType("langchain_core")
    fake_lc_messages = ModuleType("langchain_core.messages")

    class _BaseMessage:
        pass

    fake_lc_messages.BaseMessage = _BaseMessage
    fake_lc_core.messages = fake_lc_messages

    fake_langgraph = ModuleType("langgraph")
    fake_langgraph_graph = ModuleType("langgraph.graph")
    fake_langgraph_message = ModuleType("langgraph.graph.message")
    fake_langgraph_message.add_messages = lambda a, b: (a or []) + (b or [])
    fake_langgraph_graph.message = fake_langgraph_message
    fake_langgraph.graph = fake_langgraph_graph

    with patch.dict(
        sys.modules,
        {
            "langchain_core": fake_lc_core,
            "langchain_core.messages": fake_lc_messages,
            "langgraph": fake_langgraph,
            "langgraph.graph": fake_langgraph_graph,
            "langgraph.graph.message": fake_langgraph_message,
        },
    ):
        from core.application.orchestration import state as state_module
    return state_module


def test_normalize_orchestrator_state_derives_policy_and_defaults():
    state_module = _import_state_module()
    normalize_orchestrator_state = state_module.normalize_orchestrator_state

    state = normalize_orchestrator_state(
        {
            "task_id": "TASK-1",
            "task_summary": "Implement feature",
            "project_config": {"project_policy": {"max_retries": 2}},
            "status": "",
        }
    )

    assert state["project_policy"] == {"max_retries": 2}
    assert state["task_source"] == "manual"
    assert state["status"] == "in_progress"


def test_build_initial_orchestrator_state_applies_defaults_and_extra_overrides():
    state_module = _import_state_module()
    build_initial_orchestrator_state = state_module.build_initial_orchestrator_state

    state = build_initial_orchestrator_state(
        task_id="TASK-2",
        task_summary="Fix bug",
        task_source="manual",
        project_name="demo",
        project_config={
            "account_id": "acc_alpha",
            "tenant_id": "tenant_alpha",
        },
        extra_state={
            "job_id": 42,
            "account_id": "acc_alpha",
            "tenant_id": "tenant_alpha",
            "custom_runtime_hint": "kept",
            "task_source": "web",
        },
    )

    assert state["task_id"] == "TASK-2"
    assert state["task_summary"] == "Fix bug"
    assert state["task_source"] == "web"  # extra_state should win
    assert state["project_name"] == "demo"
    assert state["job_id"] == "42"
    assert state["account_id"] == "acc_alpha"
    assert state["project_config"]["account_id"] == "acc_alpha"
    assert state["project_config"]["tenant_id"] == "tenant_alpha"
    assert state["trusted_runtime_context"] == {
        "account_id": "acc_alpha",
        "tenant_id": "tenant_alpha",
        "job_id": "42",
    }
    assert state["custom_runtime_hint"] == "kept"
    assert state["workflow_path"] == []
    assert state["messages"] == []


def test_build_initial_orchestrator_state_rejects_extra_state_scope_poisoning():
    state_module = _import_state_module()
    build_initial_orchestrator_state = state_module.build_initial_orchestrator_state

    state = build_initial_orchestrator_state(
        task_id="TASK-SCOPE",
        task_summary="trusted scope",
        task_source="manual",
        project_name="demo",
        project_config={
            "account_id": "caller_account",
            "tenant_id": "caller_tenant",
            "workspace_id": "caller_workspace",
            "project_id": "caller_project",
            "workflow_id": "caller_workflow",
        },
        extra_state={
            "account_id": "victim_account",
            "project_id": "victim_project",
            "project_config": {
                "account_id": "victim_account",
                "project_id": "victim_project",
            },
            "_trusted_runtime_context": {
                "account_id": "victim_account",
                "project_id": "victim_project",
            },
            "trusted_runtime_context": {
                "account_id": "victim_account",
                "project_id": "victim_project",
            },
            "_test_mode_suppressed_outputs": {
                "state.forged": {
                    "producer_step_id": "attacker",
                    "producer_output": "forged",
                }
            },
        },
    )

    assert state["account_id"] == "caller_account"
    assert state["project_id"] == "caller_project"
    assert state["project_config"]["account_id"] == "caller_account"
    assert state["project_config"]["project_id"] == "caller_project"
    assert state["trusted_runtime_context"] == {
        "account_id": "caller_account",
        "tenant_id": "caller_tenant",
        "workspace_id": "caller_workspace",
        "project_id": "caller_project",
        "workflow_id": "caller_workflow",
    }
    assert state["_test_mode_suppressed_outputs"] == {}


def test_initial_state_test_mode_requires_explicit_host_authority():
    state_module = _import_state_module()
    build_initial_orchestrator_state = state_module.build_initial_orchestrator_state

    untrusted = build_initial_orchestrator_state(
        task_id="task-untrusted",
        task_summary="Attempt test-mode escalation",
        task_source="manual",
        project_name="Demo",
        project_config={"account_id": "account-alpha", "project_id": "project-alpha"},
        extra_state={"test_mode": True},
    )
    authorized = build_initial_orchestrator_state(
        task_id="task-authorized",
        task_summary="Authorized test run",
        task_source="workflow_test_run",
        project_name="Demo",
        project_config={"account_id": "account-alpha", "project_id": "project-alpha"},
        test_mode=True,
        extra_state={"test_mode": False},
    )

    assert untrusted["test_mode"] is False
    assert "test_mode" not in untrusted["trusted_runtime_context"]
    assert authorized["test_mode"] is True
    assert authorized["trusted_runtime_context"]["test_mode"] is True


def test_normalize_orchestrator_state_preserves_extra_fields():
    state_module = _import_state_module()
    normalize_orchestrator_state = state_module.normalize_orchestrator_state

    state = normalize_orchestrator_state(
        {
            "task_id": "TASK-3",
            "task_summary": "extra passthrough",
            "task_source": "manual",
            "project_name": "demo",
            "project_config": {},
            "custom_runtime_hint": {"alpha": True},
        }
    )

    assert state["custom_runtime_hint"] == {"alpha": True}


def test_build_initial_orchestrator_state_retains_invoke_child_lineage_fields():
    state_module = _import_state_module()
    build_initial_orchestrator_state = state_module.build_initial_orchestrator_state

    state = build_initial_orchestrator_state(
        task_id="TASK-5",
        task_summary="invoke child workflow",
        task_source="invoke_workflow",
        project_name="demo",
        project_config={},
        extra_state={
            "invoke_child_auto_approve": True,
            "invoke_parent_job_id": 500,
            "invoke_parent_step_id": "fan_out",
            "invoke_parent_step_agent_id": "invoke_workflow",
        },
    )

    assert state["invoke_child_auto_approve"] is True
    assert state["invoke_parent_job_id"] == "500"
    assert state["invoke_parent_step_id"] == "fan_out"
    assert state["invoke_parent_step_agent_id"] == "invoke_workflow"


def test_normalize_orchestrator_state_rejects_retired_string_journal_entries():
    state_module = _import_state_module()
    normalize_orchestrator_state = state_module.normalize_orchestrator_state

    with pytest.raises(TypeError, match="structured dict objects"):
        normalize_orchestrator_state(
            {
                "task_id": "TASK-6",
                "task_summary": "journal strictness",
                "task_source": "manual",
                "project_name": "demo",
                "project_config": {},
                "orchestrator_journal": ["[iter=1] retired entry"],
            }
        )
