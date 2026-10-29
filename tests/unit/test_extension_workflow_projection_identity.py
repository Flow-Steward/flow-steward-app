from __future__ import annotations

from types import SimpleNamespace


def test_disabled_projection_anchor_does_not_resolve_as_an_empty_workflow() -> None:
    from core.application.registry.workflow_lookup import get_workflow_row

    class _Store:
        extension_runtime_store = SimpleNamespace(list_projections=lambda **_kwargs: [])

        @staticmethod
        def get_workflow(workflow_id: str):
            return {
                "workflow_id": workflow_id,
                "projection_mode": "derived",
                "source_install_id": "install-a",
                "_phases": [],
            }

    assert get_workflow_row(_Store(), "extension.install-a.deploy") is None


def test_workflow_registry_hides_inactive_durable_projection_anchor() -> None:
    from core.application.registry.workflow_registry import WorkflowRegistry

    class _Store:
        extension_runtime_store = SimpleNamespace(list_projections=lambda **_kwargs: [])

        @staticmethod
        def list_workflows():
            return [
                {
                    "workflow_id": "extension.install-a.deploy",
                    "display_name": "Disabled projection bridge",
                    "enabled": True,
                    "settings": {},
                    "source_install_id": "install-a",
                    "projection_mode": "derived",
                    "_phases": [],
                }
            ]

        @staticmethod
        def list_project_workflows(**_kwargs):
            return []

    assert WorkflowRegistry(_Store()).get_workflow("extension.install-a.deploy") is None


def test_workflow_registry_loads_projected_workflow_by_public_id() -> None:
    from core.application.registry.workflow_registry import WorkflowRegistry

    class _Store:
        extension_runtime_store = SimpleNamespace(
            list_projections=lambda **_kwargs: [
                {
                    "install_id": "install-a",
                    "release_id": "release-a",
                    "artifact_id": "deploy",
                    "artifact_kind": "workflow",
                    "projection": {
                        "workflow_id": "deploy",
                        "display_name": "Deploy",
                        "enabled": True,
                        "settings": {},
                        "_phases": [],
                    },
                }
            ]
        )

        @staticmethod
        def list_workflows():
            return []

        @staticmethod
        def list_project_workflows(**_kwargs):
            return []

    workflow = WorkflowRegistry(_Store()).get_workflow("extension.install-a.deploy")

    assert workflow is not None
    assert workflow.workflow_id == "extension.install-a.deploy"


def test_projected_registries_preserve_install_scoped_runtime_references() -> None:
    from core.application.registry.agent_registry import AgentRegistry
    from core.application.registry.skill_registry import SkillRegistry
    from core.application.registry.tool_registry import ToolRegistry
    from core.application.registry.workflow_registry import WorkflowRegistry

    projections = {
        "workflow": [
            {
                "install_id": "install-a",
                "artifact_id": "deploy",
                "artifact_kind": "workflow",
                "projection": {
                    "workflow_id": "deploy",
                    "display_name": "Deploy",
                    "enabled": True,
                    "settings": {},
                    "steps": [
                        {
                            "step_id": "write",
                            "step_kind": "connector",
                            "connector_id": "connector.draft",
                            "override": {"connector": {"function_name": "compose"}},
                            "group_id": None,
                            "lane_id": None,
                        }
                    ],
                    "groups": [],
                },
            }
        ],
        "agent": [
            {
                "install_id": "install-a",
                "artifact_id": "writer",
                "artifact_kind": "agent",
                "projection": {
                    "agent_id": "writer",
                    "display_name": "Writer",
                    "base_skill": "write",
                    "tools": ["draft"],
                },
            }
        ],
        "skill": [
            {
                "install_id": "install-a",
                "artifact_id": "write",
                "artifact_kind": "skill",
                "projection": {
                    "skill_id": "write",
                    "display_name": "Write",
                    "expected_inputs": [],
                    "expected_outputs": [],
                },
            }
        ],
        "tool": [
            {
                "install_id": "install-a",
                "artifact_id": "draft",
                "artifact_kind": "tool",
                "projection": {"tool_id": "draft", "display_name": "Draft"},
            }
        ],
    }

    class _Store:
        extension_runtime_store = SimpleNamespace(
            list_projections=lambda *, artifact_kind=None, **_kwargs: (
                projections[artifact_kind]
                if artifact_kind
                else [row for rows in projections.values() for row in rows]
            )
        )

        @staticmethod
        def list_workflows():
            return []

        @staticmethod
        def list_agents():
            return []

        @staticmethod
        def list_skills():
            return []

        @staticmethod
        def list_tools():
            return []

        @staticmethod
        def list_project_workflows(**_kwargs):
            return []

    store = _Store()
    workflow = WorkflowRegistry(store).get_workflow("extension.install-a.deploy")
    agent = AgentRegistry(store).get_agent("extension.install-a.writer")
    skill = SkillRegistry(store).get_skill("extension.install-a.write")
    tool = ToolRegistry(store).get_tool("extension.install-a.draft")

    assert workflow is not None
    assert len(workflow.steps) == 1
    assert workflow.steps[0].group_id is None
    assert workflow.steps[0].connector_id == "extension.install-a.draft"
    assert workflow.steps[0].connector_install_id == "install-a"
    assert agent is not None
    assert agent.skills == ["extension.install-a.write"]
    assert agent.tools == ["extension.install-a.draft"]
    assert skill is not None
    assert tool is not None


def test_projected_skill_contract_is_available_to_prompt_and_output_validation(monkeypatch) -> None:
    from contextlib import contextmanager

    from core.application.registry.db_store_skills_mixin import (
        RegistryDBStoreSkillsMixin,
    )

    class _Cursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return []

    class _Store(RegistryDBStoreSkillsMixin):
        extension_runtime_store = SimpleNamespace(
            list_projections=lambda **_kwargs: [
                {
                    "install_id": "install-a",
                    "release_id": "release-a",
                    "artifact_id": "write",
                    "artifact_kind": "skill",
                    "projection": {
                        "skill_id": "write",
                        "display_name": "Write",
                        "expected_outputs": [
                            {
                                "name": "launch_copy",
                                "type": "text",
                                "description": "Completed launch copy.",
                                "required": True,
                            }
                        ],
                    },
                }
            ]
        )

        @contextmanager
        def _cursor(self):
            yield _Cursor()

    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.active_extension_ids_for_account",
        lambda *_args, **_kwargs: {"acme.example"},
    )
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.active_extension_versions_for_account",
        lambda *_args, **_kwargs: {"acme.example": "1.0.0"},
    )
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.resolve_projection_dependency_availability",
        lambda *_args, **_kwargs: {"new_runs_blocked": False},
    )

    store = _Store()
    contract = store.get_skill_contract("extension.install-a.write")
    projected_outputs = store.get_skill_outputs("extension.install-a.write")

    assert contract["required_fields"] == ["launch_copy"]
    assert contract["optional_fields"] == []
    assert contract["all_fields"] == [
        {
            "name": "launch_copy",
            "type": "text",
            "description": "Completed launch copy.",
            "required": True,
        }
    ]
    assert [row["name"] for row in projected_outputs] == ["launch_copy"]

    from core.application.orchestration.agents.prompt_agent_artifacts import (
        PromptAgentArtifactMixin,
    )
    from core.application.orchestration.agents.prompt_agent_phase_result import (
        PromptAgentPhaseResultMixin,
    )
    from core.application.orchestration.agents.prompt_builder import PromptBuilder
    from core.application.registry.registry_manager import RegistryManager

    monkeypatch.setattr(
        RegistryManager,
        "instance",
        lambda: SimpleNamespace(db_store=store),
    )
    prompt = PromptBuilder()._build_phase_result_v2_requirements("extension.install-a.write")
    final_schema = PromptAgentArtifactMixin._resolve_final_response_schema(
        PromptAgentArtifactMixin(),
        "extension.install-a.write",
    )

    assert '"launch_copy": "..."' in prompt
    assert final_schema["properties"]["artifacts"]["required"] == ["launch_copy"]
    assert PromptAgentPhaseResultMixin.validate_agent_output("extension.install-a.write", {}) == [
        "Missing required output: 'launch_copy'"
    ]


def test_extension_workflow_projection_uses_install_scoped_public_id(monkeypatch) -> None:
    from core.entrypoints.admin import workflows_projection

    runtime_store = SimpleNamespace(
        list_projections=lambda **_kwargs: [
            {
                "install_id": "install-a",
                "release_id": "release-a",
                "extension_id": "vendor.one",
                "artifact_id": "deploy",
                "artifact_kind": "workflow",
                "artifact_ref": "artifacts/deploy.yaml",
                "is_active": True,
                "projection": {"workflow_id": "deploy", "display_name": "Deploy"},
            }
        ]
    )
    monkeypatch.setattr(
        workflows_projection,
        "_get_extension_runtime_store",
        lambda _db: runtime_store,
    )
    rows = workflows_projection._extension_workflow_projection_rows(object())

    assert rows[0]["workflow_id"] == "extension.install-a.deploy"
    assert rows[0]["local_workflow_id"] == "deploy"


def test_projected_agents_tools_and_skills_read_only_shared_categories(monkeypatch) -> None:
    from core.application.registry.projected_artifact_rows import projected_artifact_rows

    def _projection(kind: str, id_key: str, local_id: str, category: object) -> dict:
        body = {id_key: local_id, "display_name": local_id}
        if category is not None:
            body["category"] = category
        return {
            "install_id": "install-a",
            "release_id": "release-a",
            "artifact_id": local_id,
            "artifact_kind": kind,
            "projection": body,
        }

    projections = [
        _projection("agent", "agent_id", "demo_agent", None),
        _projection("tool", "tool_id", "demo_tool", "demo"),
        _projection("skill", "skill_id", "demo_skill", " Marketing "),
    ]
    store = SimpleNamespace(
        extension_runtime_store=SimpleNamespace(list_projections=lambda **_kwargs: projections)
    )
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.active_extension_ids_for_account",
        lambda *_args, **_kwargs: set(),
    )
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.active_extension_versions_for_account",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.resolve_projection_dependency_availability",
        lambda *_args, **_kwargs: {"new_runs_blocked": False},
    )

    def _category(kind: str, id_key: str) -> str:
        (row,) = projected_artifact_rows(store, kind, id_key)
        return row["category"]

    assert _category("agent", "agent_id") == "other"
    assert _category("tool", "tool_id") == "other"
    assert _category("skill", "skill_id") == "marketing"
    assert projections[1]["projection"]["category"] == "demo"
