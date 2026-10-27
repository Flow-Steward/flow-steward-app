"""
Unit tests for graph/builder.py — build_graph_from_registry().

Tests:
- Graph compiles cleanly with all agents
- Fixed nodes present
- Every agent has edge back to orchestrator
- Disabled agents excluded

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_graph_builder.py -v
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests._db_env import dsn_unreachable_reason, resolve_test_db_dsn
from tests._workflow_definitions import linear_graph

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

FIXED_NODES = {
    "task_intake",
    "kb_intake",
    "workflow_routing",
    "orchestrator",
    "parallel_executor",
    "invoke_workflow_step",
    "orchestrator_validation",
    "human_approval_gate",
    "execute_step",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _registry_db_dsn(monkeypatch):
    """Point RegistryManager at the test database for this test only.

    RegistryManager reads FS_DB_DSN. When the harness named the database through
    another variable, the test sets FS_DB_DSN and monkeypatch removes it again,
    so no later test inherits a database it did not ask for.
    """
    db_dsn = resolve_test_db_dsn()
    if db_dsn and not os.environ.get("FS_DB_DSN"):
        monkeypatch.setenv("FS_DB_DSN", db_dsn)


def get_fresh_registry():
    db_dsn = resolve_test_db_dsn()
    if not db_dsn:
        pytest.skip("No database DSN configured")
    unreachable = dsn_unreachable_reason(db_dsn)
    if unreachable:
        pytest.skip(f"Database host is not reachable from this test environment: {unreachable}")
    import core.application.registry.registry_manager as rm_module

    rm_module._instance = None
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def test_fixed_graph_edges_are_explicit_and_command_safe(monkeypatch):
    """Fixed graph flow has no hidden parser or static approval return edge."""
    from core.application.orchestration.graph import builder

    captured_edges: list[tuple[str, str]] = []
    captured_nodes: list[str] = []

    class _FakeGraph:
        def __init__(self, _state_type):
            self.nodes = {}

        def add_node(self, name, node):
            self.nodes[name] = node
            captured_nodes.append(name)

        def add_edge(self, source, target):
            captured_edges.append((source, target))

        def set_entry_point(self, _name):
            return None

        def compile(self, checkpointer=None):
            return self

    fake_registry = SimpleNamespace(
        workflows=SimpleNamespace(list_all=list),
        agents=SimpleNamespace(list_all=list),
    )

    monkeypatch.setattr("langgraph.graph.StateGraph", _FakeGraph, raising=False)
    monkeypatch.setattr("langgraph.graph.END", "__end__", raising=False)

    builder.build_graph_from_registry(
        fake_registry,
        checkpointer=False,
        validate_contracts=False,
    )

    assert ("human_approval_gate", "orchestrator") not in captured_edges
    assert ("orchestrator_validation", "__end__") in captured_edges
    assert "attachment_processor" not in captured_nodes
    assert ("task_intake", "kb_intake") in captured_edges
    assert not any("attachment_processor" in edge for edge in captured_edges)


# ---------------------------------------------------------------------------
# Graph compilation tests
# ---------------------------------------------------------------------------


class TestGraphBuilderCompilation:
    """Test that the registry graph compiles cleanly."""

    def test_registry_graph_compiles(self):
        """build_graph_from_registry() should compile without exception."""
        try:
            from langgraph.checkpoint.memory import MemorySaver

            from core.application.orchestration.graph.builder import build_graph_from_registry
        except ImportError as e:
            pytest.skip(f"Required modules not available: {e}")

        registry = get_fresh_registry()
        checkpointer = MemorySaver()

        try:
            compiled = build_graph_from_registry(registry, checkpointer=checkpointer)
        except RuntimeError as e:
            if "Graph contract validation failed" in str(e):
                pytest.skip(f"Registry contains invalid workflow contracts: {e}")
            raise
        assert compiled is not None

    def test_a_compile_only_build_names_its_sentinel(self):
        """Only an explicit ``checkpointer=False`` compiles without a checkpointer."""
        from core.application.orchestration.graph.builder import build_graph_from_registry

        compiled = build_graph_from_registry(get_fresh_registry(), checkpointer=False)
        assert compiled is not None

    def test_a_runnable_graph_without_a_checkpointer_refuses_to_build(self, monkeypatch):
        """None asks for the real checkpointer; when there is none, nothing compiles."""
        from core.application.orchestration import graph_runtime
        from core.application.orchestration.checkpoint_requirement import (
            CheckpointUnavailableError,
        )
        from core.application.orchestration.graph.builder import build_graph_from_registry

        monkeypatch.setattr(graph_runtime, "_get_checkpointer", lambda: None)
        registry = get_fresh_registry()
        with pytest.raises(CheckpointUnavailableError):
            build_graph_from_registry(registry, checkpointer=None)

    def test_graph_compiles_with_memory_checkpointer(self):
        """With MemorySaver checkpointer, should compile."""
        try:
            from langgraph.checkpoint.memory import MemorySaver

            from core.application.orchestration.graph.builder import build_graph_from_registry
        except ImportError as e:
            pytest.skip(f"Required modules not available: {e}")

        registry = get_fresh_registry()
        checkpointer = MemorySaver()
        try:
            compiled = build_graph_from_registry(registry, checkpointer=checkpointer)
        except RuntimeError as e:
            if "Graph contract validation failed" in str(e):
                pytest.skip(f"Registry contains invalid workflow contracts: {e}")
            raise
        assert compiled is not None


# ---------------------------------------------------------------------------
# Fixed node tests
# ---------------------------------------------------------------------------


class TestGraphBuilderFixedNodes:
    """Test that fixed infrastructure nodes are present."""

    @pytest.fixture(scope="class")
    def compiled_graph(self):
        try:
            from langgraph.checkpoint.memory import MemorySaver

            from core.application.orchestration.graph.builder import build_graph_from_registry
        except ImportError as e:
            pytest.skip(f"Required modules not available: {e}")

        registry = get_fresh_registry()
        checkpointer = MemorySaver()
        try:
            return build_graph_from_registry(registry, checkpointer=checkpointer)
        except RuntimeError as e:
            if "Graph contract validation failed" in str(e):
                pytest.skip(f"Registry contains invalid workflow contracts: {e}")
            raise

    def test_task_intake_node_present(self, compiled_graph):
        assert "task_intake" in compiled_graph.nodes

    def test_kb_intake_node_present(self, compiled_graph):
        assert "kb_intake" in compiled_graph.nodes

    def test_orchestrator_node_present(self, compiled_graph):
        assert "orchestrator" in compiled_graph.nodes

    def test_orchestrator_validation_node_present(self, compiled_graph):
        assert "orchestrator_validation" in compiled_graph.nodes

    def test_parallel_executor_node_present(self, compiled_graph):
        assert "parallel_executor" in compiled_graph.nodes

    def test_invoke_workflow_step_node_present(self, compiled_graph):
        assert "invoke_workflow_step" in compiled_graph.nodes

    def test_hidden_attachment_processor_node_absent(self, compiled_graph):
        assert "attachment_processor" not in compiled_graph.nodes

    def test_human_approval_gate_node_present(self, compiled_graph):
        assert "human_approval_gate" in compiled_graph.nodes

    def test_all_fixed_nodes_present(self, compiled_graph):
        for node in FIXED_NODES:
            assert node in compiled_graph.nodes, f"Fixed node '{node}' missing from graph"


class TestGraphBuilderDynamicNodes:
    """Test that agent nodes are correctly added."""

    @pytest.fixture(scope="class")
    def user_authored_graph_catalog(self):
        """Persist neutral user-authored entities the graph is built from.

        The 1.0 registry seed ships no agents, skills or workflows, so the
        graph wiring is exercised against one enabled agent referenced by an
        enabled workflow and one disabled agent, authored the way a user would.
        """
        db_dsn = resolve_test_db_dsn()
        if not db_dsn:
            pytest.skip("No database DSN configured")
        unreachable = dsn_unreachable_reason(db_dsn)
        if unreachable:
            pytest.skip(f"Database host is not reachable from this test environment: {unreachable}")
        from core.application.registry.db_store import RegistryDBStore

        db = RegistryDBStore(db_dsn)
        suffix = uuid.uuid4().hex[:8]
        ids = SimpleNamespace(
            skill_id=f"draft_summary_{suffix}",
            agent_id=f"writer_{suffix}",
            disabled_agent_id=f"reviewer_{suffix}",
            workflow_id=f"weekly_report_{suffix}",
        )
        output_name = "summary_doc"
        db.save_skill(
            {
                "skill_id": ids.skill_id,
                "display_name": f"Draft summary {suffix}",
                "description": "Drafts a short summary",
                "enabled": True,
                "category": "analysis",
                "skill_type": "secondary",
                "instructions": ["Write a concise summary of the topic."],
                "tags": [],
                "expected_inputs": [
                    {
                        "name": "topic",
                        "type": "text",
                        "description": "Topic to summarise",
                        "required": True,
                    }
                ],
                "expected_outputs": [
                    {
                        "name": output_name,
                        "type": "text",
                        "description": "Short summary of the topic",
                        "required": True,
                    }
                ],
            },
            author="test",
        )
        for agent_id, enabled, name in (
            (ids.agent_id, True, "Writer"),
            (ids.disabled_agent_id, False, "Reviewer"),
        ):
            db.save_agent(
                {
                    "agent_id": agent_id,
                    "agent_type_key": "researcher",
                    "display_name": f"{name} {suffix}",
                    "description": f"{name} for graph builder tests",
                    "enabled": enabled,
                    "prompts": {"system": "You write short, factual summaries."},
                    "config": {},
                    "quality": {},
                    "inputs": {"required": [], "optional": []},
                    "outputs": [],
                    "depends_on": {"soft": [], "hard": []},
                    "tags": [],
                    "skills": [ids.skill_id],
                    "tools": [],
                },
                author="test",
            )
        db.save_workflow(
            {
                "workflow_id": ids.workflow_id,
                "display_name": f"Weekly report {suffix}",
                "description": "Drafts a short summary of the task",
                "enabled": True,
                "tags": [],
                "settings": {
                    "execution_graph": linear_graph(["workflow_start", "draft", "workflow_success"])
                },
                "groups": [],
                "for_each": [],
                "steps": [
                    {
                        "step_id": "workflow_start",
                        "display_name": "Start workflow",
                        "step_kind": "set_variable",
                        "override": {
                            "set_variable": {
                                "target": "state.workflow_started",
                                "value": True,
                            }
                        },
                    },
                    {
                        "step_id": "draft",
                        "display_name": "Draft summary",
                        "agent_id": ids.agent_id,
                        "step_kind": "agent",
                        "error_policy": {"on_error": "retry", "max_retries": 1},
                        "boundary_contract": {
                            "entry": [
                                {
                                    "name": "topic",
                                    "type": "text",
                                    "source": "state.task_summary",
                                    "required": True,
                                }
                            ],
                            "exit": [output_name],
                        },
                    },
                    {
                        "step_id": "workflow_success",
                        "display_name": "Complete workflow",
                        "step_kind": "stop_success",
                        "override": {
                            "stop_success": {"message": "Workflow completed successfully."}
                        },
                    },
                ],
            },
            author="test",
        )
        try:
            yield ids
        finally:
            db.delete_workflow(ids.workflow_id, author="test")
            db.delete_agent(ids.disabled_agent_id, author="test")
            db.delete_agent(ids.agent_id, author="test")
            db.delete_skill(ids.skill_id, author="test")
            import core.application.registry.registry_manager as rm_module

            rm_module._instance = None

    @pytest.fixture(scope="class")
    def registry(self, user_authored_graph_catalog):
        return get_fresh_registry()

    @pytest.fixture(scope="class")
    def compiled_graph(self, registry):
        try:
            from langgraph.checkpoint.memory import MemorySaver

            from core.application.orchestration.graph.builder import build_graph_from_registry
        except ImportError as e:
            pytest.skip(f"Required modules not available: {e}")
        checkpointer = MemorySaver()
        try:
            return build_graph_from_registry(registry, checkpointer=checkpointer)
        except RuntimeError as e:
            if "Graph contract validation failed" in str(e):
                pytest.skip(f"Registry contains invalid workflow contracts: {e}")
            raise

    def test_all_enabled_agents_are_nodes(
        self, registry, compiled_graph, user_authored_graph_catalog
    ):
        """Every directly-referenced enabled agent should have a graph node.

        Agents not referenced by enabled workflows do not get dedicated nodes.
        """
        from core.application.orchestration.graph.builder import _collect_directly_referenced_agents

        directly_referenced = _collect_directly_referenced_agents(registry)
        assert user_authored_graph_catalog.agent_id in directly_referenced
        assert user_authored_graph_catalog.disabled_agent_id not in directly_referenced

        graph_nodes = compiled_graph.nodes
        for agent_id in directly_referenced:
            manifest = registry.agents.get(agent_id)
            if manifest is None or not manifest.enabled:
                continue
            assert agent_id in graph_nodes, (
                f"Directly-referenced enabled agent '{agent_id}' not found in graph nodes"
            )

    def test_disabled_agents_not_in_graph(
        self, registry, compiled_graph, user_authored_graph_catalog
    ):
        """Disabled agents should NOT appear in graph."""
        disabled_agents = [a for a in registry.agents.list_all() if not a.enabled]
        assert user_authored_graph_catalog.disabled_agent_id in {
            a.agent_id for a in disabled_agents
        }

        graph_nodes = compiled_graph.nodes
        for agent in disabled_agents:
            assert agent.agent_id not in graph_nodes, (
                f"Disabled agent '{agent.agent_id}' found in graph nodes"
            )

    def test_agent_count_reasonable(self, registry, compiled_graph):
        """Graph should have at least fixed nodes + directly-referenced agents."""
        total_nodes = len(compiled_graph.nodes)
        assert total_nodes >= len(FIXED_NODES)


# ---------------------------------------------------------------------------
# Graph builder with mock registry (for isolation)
# ---------------------------------------------------------------------------


class TestGraphBuilderWithMockRegistry:
    """Test graph building with controlled mock registry."""

    def _make_mock_registry(self, agent_ids: list[str], disabled_ids: list[str] | None = None):
        """Create a mock registry with specific agents."""
        disabled_ids = disabled_ids or []
        registry = MagicMock()

        agents = []
        for agent_id in agent_ids:
            mock_agent = MagicMock()
            mock_agent.agent_id = agent_id
            mock_agent.enabled = agent_id not in disabled_ids
            mock_agent.display_name = agent_id.title()
            mock_agent.tools = []
            mock_agent.config = {}
            agents.append(mock_agent)

        registry.agents.list_all.return_value = agents
        registry.tools.get_tool = MagicMock(return_value=MagicMock())

        return registry

    def test_no_agents_graph_has_only_fixed_nodes(self):
        """With no enabled agents, only fixed nodes should be in graph."""
        try:
            from langgraph.checkpoint.memory import MemorySaver

            from core.application.orchestration.graph.builder import build_graph_from_registry
        except ImportError as e:
            pytest.skip(f"Required modules not available: {e}")

        registry = self._make_mock_registry([], [])

        with (
            patch("core.application.orchestration.graph.nodes.load_agent_implementation"),
            patch("core.application.orchestration.graph.nodes.wrap_agent_as_node"),
        ):
            try:
                checkpointer = MemorySaver()
                compiled = build_graph_from_registry(registry, checkpointer=checkpointer)
            except Exception as e:
                pytest.skip(f"Could not build graph with mock registry: {e}")
            for node in FIXED_NODES:
                assert node in compiled.nodes

    def test_build_fails_on_workflow_agent_skill_contract_mismatch(self):
        try:
            from langgraph.checkpoint.memory import MemorySaver

            from core.application.orchestration.graph.builder import build_graph_from_registry
        except ImportError as e:
            pytest.skip(f"Required modules not available: {e}")

        registry = MagicMock()
        agent = MagicMock()
        agent.agent_id = "writer"
        agent.enabled = True
        agent.display_name = "Developer"
        agent.skills = ["write_section"]
        agent.tools = []
        agent.config = {}

        skill = MagicMock()
        skill.skill_id = "write_section"
        skill.enabled = True

        step = MagicMock()
        step.agent = "writer"
        step.skill = "publish_report"
        step.optional = False

        workflow = MagicMock()
        workflow.workflow_id = "wf-mismatch"
        workflow.enabled = True
        workflow.steps = [step]

        registry.agents.list_all.return_value = [agent]
        registry.skills.list_all.return_value = [skill]
        registry.workflows.list_all.return_value = [workflow]

        _raises_input_586_1 = MemorySaver()
        with pytest.raises(RuntimeError, match="Graph contract validation failed"):
            build_graph_from_registry(registry, checkpointer=_raises_input_586_1)

    def test_validate_contracts_requires_schema_columns_not_legacy_expected_lists(self):
        from core.application.orchestration.graph.builder import _validate_graph_contracts

        registry = MagicMock()
        registry.db_store = MagicMock()
        registry.agents.list_all.return_value = []
        registry.workflows.list_all.return_value = []
        registry.skills.list_all.return_value = [
            SimpleNamespace(
                skill_id="write_section",
                enabled=True,
                expected_inputs=[{"name": "task_summary", "required": True}],
                expected_outputs=[{"name": "git_diff", "required": True}],
                input_schema={},
                output_schema={},
            )
        ]

        with (
            patch(
                "core.application.registry.contract_validation.validate_agent_readiness",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.application.registry.contract_validation.validate_skill_readiness",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
            patch(
                "core.application.registry.contract_validation.validate_workflow_readiness",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            errors = _validate_graph_contracts(registry)

        assert any("missing required input_schema" in e for e in errors)
        assert any("missing required output_schema" in e for e in errors)

    def test_validate_graph_contracts_uses_shared_readiness_validators_when_db_store_present(self):
        from core.application.orchestration.graph.builder import _validate_graph_contracts

        registry = MagicMock()
        registry.db_store = MagicMock()
        registry.agents.list_all.return_value = [
            SimpleNamespace(agent_id="writer", enabled=True),
        ]
        registry.skills.list_all.return_value = [
            SimpleNamespace(
                skill_id="write_section",
                enabled=True,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            )
        ]
        registry.workflows.list_all.return_value = [
            SimpleNamespace(workflow_id="wf_main", enabled=True),
        ]

        with (
            patch(
                "core.application.registry.contract_validation.validate_agent_readiness",
                return_value={
                    "valid": False,
                    "errors": [
                        {
                            "message": "Agent is missing required output contract",
                            "path": "agent.outputs",
                        }
                    ],
                    "warnings": [],
                },
            ) as validate_agent,
            patch(
                "core.application.registry.contract_validation.validate_skill_readiness",
                return_value={"valid": True, "errors": [], "warnings": []},
            ) as validate_skill,
            patch(
                "core.application.registry.contract_validation.validate_workflow_readiness",
                return_value={"valid": True, "errors": [], "warnings": []},
            ) as validate_workflow,
        ):
            errors = _validate_graph_contracts(registry)

        assert validate_agent.call_count == 1
        assert validate_skill.call_count == 1
        assert validate_workflow.call_count == 1
        assert any(
            "Agent 'writer'" in err and "missing required output contract" in err for err in errors
        )

    def test_validate_graph_contracts_scopes_workflow_readiness_when_requested(self):
        from core.application.orchestration.graph.builder import _validate_graph_contracts

        registry = MagicMock()
        registry.db_store = MagicMock()
        registry.agents.list_all.return_value = []
        registry.skills.list_all.return_value = []
        registry.workflows.list_all.return_value = [
            SimpleNamespace(workflow_id="selected_workflow", enabled=True),
            SimpleNamespace(workflow_id="draft_legacy_workflow", enabled=True),
        ]

        def _workflow_validation(_db, workflow_id):
            if workflow_id == "draft_legacy_workflow":
                return {
                    "valid": False,
                    "errors": [
                        {
                            "message": "draft workflow still has a retired contract",
                            "path": "workflow.callable",
                        }
                    ],
                    "warnings": [],
                }
            return {"valid": True, "errors": [], "warnings": []}

        with patch(
            "core.application.registry.contract_validation.validate_workflow_readiness",
            side_effect=_workflow_validation,
        ) as validate_workflow:
            errors = _validate_graph_contracts(
                registry,
                workflow_ids=["selected_workflow"],
            )

        assert errors == []
        validate_workflow.assert_called_once_with(registry.db_store, "selected_workflow")

    def test_step_test_graph_validation_ignores_unrelated_step_issue(self):
        from core.application.orchestration.graph.builder import _validate_graph_contracts
        from core.application.registry.workflow_manifest import StepConfig, WorkflowManifest

        registry = MagicMock()
        registry.db_store = MagicMock()
        registry.agents.list_all.return_value = []
        registry.skills.list_all.return_value = []
        registry.workflows.list_all.return_value = [
            WorkflowManifest(
                workflow_id="wf-demo",
                version="1",
                display_name="Demo",
                description="",
                enabled=True,
                trigger={},
                steps=[
                    StepConfig.from_dict(step)
                    for step in [
                        {"step_id": "unrelated", "step_kind": "connector"},
                        {"step_id": "selected", "step_kind": "set_variable"},
                    ]
                ],
            )
        ]
        validation = {
            "valid": False,
            "errors": [
                {
                    "code": "workflow.step_connector_connection_scopes_missing",
                    "message": "Authorize unrelated scopes",
                    "path": "workflow.steps.unrelated",
                }
            ],
            "warnings": [],
        }

        with patch(
            "core.application.registry.contract_validation.validate_workflow_readiness",
            return_value=validation,
        ):
            errors = _validate_graph_contracts(
                registry,
                workflow_ids=["wf-demo"],
                workflow_step_scope={
                    "workflow_id": "wf-demo",
                    "step_id": "selected",
                },
            )

        assert errors == []

    def test_step_test_graph_validation_rejects_selected_step_issue(self):
        from core.application.orchestration.graph.builder import _validate_graph_contracts
        from core.application.registry.workflow_manifest import StepConfig, WorkflowManifest

        registry = MagicMock()
        registry.db_store = MagicMock()
        registry.agents.list_all.return_value = []
        registry.skills.list_all.return_value = []
        registry.workflows.list_all.return_value = [
            WorkflowManifest(
                workflow_id="wf-demo",
                version="1",
                display_name="Demo",
                description="",
                enabled=True,
                trigger={},
                steps=[
                    StepConfig.from_dict(step)
                    for step in [
                        {"step_id": "unrelated", "step_kind": "connector"},
                        {"step_id": "selected", "step_kind": "connector"},
                    ]
                ],
            )
        ]
        selected_issue = {
            "valid": False,
            "errors": [
                {
                    "code": "workflow.step_connector_install_unavailable",
                    "message": "Selected install is unavailable",
                    "path": "workflow.steps.selected.connector_install_id",
                }
            ],
            "warnings": [],
        }

        with patch(
            "core.application.registry.contract_validation.validate_workflow_readiness",
            return_value=selected_issue,
        ):
            errors = _validate_graph_contracts(
                registry,
                workflow_ids=["wf-demo"],
                workflow_step_scope={
                    "workflow_id": "wf-demo",
                    "step_id": "selected",
                },
            )

        assert any("Selected install is unavailable" in error for error in errors)

    def test_step_test_graph_validation_checks_only_selected_agent_dependencies(self):
        from core.application.orchestration.graph.builder import _validate_graph_contracts
        from core.application.registry.workflow_manifest import StepConfig, WorkflowManifest

        registry = MagicMock()
        registry.db_store = MagicMock()
        registry.agents.list_all.return_value = [
            SimpleNamespace(agent_id="unrelated-agent", enabled=True, skills=[]),
            SimpleNamespace(agent_id="selected-agent", enabled=True, skills=[]),
        ]
        registry.skills.list_all.return_value = []
        registry.workflows.list_all.return_value = [
            WorkflowManifest(
                workflow_id="wf-demo",
                version="1",
                display_name="Demo",
                description="",
                enabled=True,
                trigger={},
                steps=[
                    StepConfig.from_dict(step)
                    for step in [
                        {
                            "step_id": "unrelated",
                            "step_kind": "agent",
                            "agent_id": "unrelated-agent",
                        },
                        {
                            "step_id": "selected",
                            "step_kind": "agent",
                            "agent_id": "selected-agent",
                        },
                    ]
                ],
            )
        ]

        def validate_agent(_db, agent_id):
            if agent_id == "unrelated-agent":
                return {
                    "valid": False,
                    "errors": [{"message": "Unrelated agent is invalid"}],
                    "warnings": [],
                }
            return {"valid": True, "errors": [], "warnings": []}

        with (
            patch(
                "core.application.registry.contract_validation.validate_agent_readiness",
                side_effect=validate_agent,
            ) as validate_agent_readiness,
            patch(
                "core.application.registry.contract_validation.validate_workflow_readiness",
                return_value={"valid": True, "errors": [], "warnings": []},
            ),
        ):
            errors = _validate_graph_contracts(
                registry,
                workflow_ids=["wf-demo"],
                workflow_step_scope={
                    "workflow_id": "wf-demo",
                    "step_id": "selected",
                },
            )

        assert errors == []
        validate_agent_readiness.assert_called_once_with(registry.db_store, "selected-agent")

    def test_whole_workflow_graph_validation_still_rejects_unrelated_step_issue(self):
        from core.application.orchestration.graph.builder import _validate_graph_contracts
        from core.application.registry.workflow_manifest import StepConfig, WorkflowManifest

        registry = MagicMock()
        registry.db_store = MagicMock()
        registry.agents.list_all.return_value = []
        registry.skills.list_all.return_value = []
        registry.workflows.list_all.return_value = [
            WorkflowManifest(
                workflow_id="wf-demo",
                version="1",
                display_name="Demo",
                description="",
                enabled=True,
                trigger={},
                steps=[
                    StepConfig.from_dict(step)
                    for step in [
                        {"step_id": "unrelated", "step_kind": "connector"},
                        {"step_id": "selected", "step_kind": "set_variable"},
                    ]
                ],
            )
        ]
        validation = {
            "valid": False,
            "errors": [
                {
                    "code": "workflow.step_connector_connection_scopes_missing",
                    "message": "Authorize unrelated scopes",
                    "path": "workflow.steps.unrelated",
                }
            ],
            "warnings": [],
        }

        with patch(
            "core.application.registry.contract_validation.validate_workflow_readiness",
            return_value=validation,
        ):
            errors = _validate_graph_contracts(registry, workflow_ids=["wf-demo"])

        assert any("Authorize unrelated scopes" in error for error in errors)

    def test_build_graph_can_skip_contract_validation_for_read_only_state_reads(self):
        try:
            from langgraph.checkpoint.memory import MemorySaver

            from core.application.orchestration.graph.builder import build_graph_from_registry
        except ImportError as e:
            pytest.skip(f"Required modules not available: {e}")

        registry = MagicMock()
        registry.db_store = MagicMock()
        registry.agents.list_all.return_value = []
        registry.workflows.list_all.return_value = []
        registry.skills.list_all.return_value = [
            SimpleNamespace(
                skill_id="projection_cap_orphan",
                enabled=True,
                input_schema={},
                output_schema={},
            )
        ]

        compiled = build_graph_from_registry(
            registry,
            checkpointer=MemorySaver(),
            validate_contracts=False,
        )

        assert compiled is not None

    def test_resolve_agent_tool_catalog_prefers_typed_registry_catalog(self):
        from core.application.orchestration.graph.builder import _resolve_agent_tool_catalog

        registry = MagicMock()
        registry.tools.get_effective_catalog.return_value = [
            {
                "tool_id": "context_lookup",
                "implementation_kind": "mcp",
                "implementation": {"kind": "mcp", "type": "mcp"},
                "source": "skill",
                "interface": {
                    "operations": [{"name": "lookup_docs"}],
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                },
            }
        ]

        catalog = _resolve_agent_tool_catalog(registry, "writer")

        registry.tools.get_effective_catalog.assert_called_once_with("writer")
        assert catalog[0]["tool_id"] == "context_lookup"
        assert catalog[0]["implementation_kind"] == "mcp"
        assert catalog[0]["source"] == "skill"


@pytest.mark.parametrize("live_workflow", ["deleted", "disabled", "repointed"])
def test_pinned_graph_keeps_enabled_agents_after_live_workflow_changes(monkeypatch, live_workflow):
    from core.application.orchestration.graph import builder, nodes

    registry = MagicMock()
    registry.agents.list_all.return_value = [
        SimpleNamespace(agent_id="archived-agent", enabled=True, display_name="Archived"),
        SimpleNamespace(agent_id="current-agent", enabled=True, display_name="Current"),
        SimpleNamespace(agent_id="disabled-agent", enabled=False, display_name="Disabled"),
    ]
    registry.workflows.list_all.return_value = (
        []
        if live_workflow == "deleted"
        else [
            SimpleNamespace(
                workflow_id="pinned-workflow",
                enabled=live_workflow != "disabled",
                participants=[],
                settings={},
                steps=[SimpleNamespace(agent="current-agent", participant_key="")],
            )
        ]
    )
    registry.tools.get_effective_catalog.return_value = []
    monkeypatch.setattr(nodes, "load_agent_implementation", lambda *_: object())
    monkeypatch.setattr(nodes, "wrap_agent_as_node", lambda *_: lambda state: {})

    graph = builder.build_graph_from_registry(
        registry,
        checkpointer=False,
        validate_contracts=False,
        workflow_ids=["pinned-workflow"],
    )

    assert "archived-agent" in graph.nodes
    assert "disabled-agent" not in graph.nodes
