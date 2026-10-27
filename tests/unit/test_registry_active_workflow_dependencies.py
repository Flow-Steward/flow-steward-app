from __future__ import annotations

from core.application.registry.active_workflow_dependencies import (
    enabled_workflow_dependencies,
    workflow_dependency_impacts,
    workflow_registry_tool_issues,
)


class _RegistryDb:
    def list_workflows(self, *, enabled_only: bool, include_definitions: bool):
        assert enabled_only is True
        assert include_definitions is True
        return [
            {
                "workflow_id": "wf_direct_agent",
                "enabled": True,
                "settings": {},
                "steps": [
                    {"step_kind": "agent", "agent_id": "writer"},
                ],
            },
            {
                "workflow_id": "wf_bound_agent",
                "enabled": True,
                "settings": {
                    "participants": [{"participant_key": "reviewer", "agent_id": "writer"}]
                },
                "steps": [
                    {"step_kind": "agent", "participant_key": "reviewer"},
                ],
            },
            {
                "workflow_id": "wf_direct_tool",
                "enabled": True,
                "settings": {},
                "steps": [
                    {
                        "step_kind": "connector",
                        "connector_id": "connector.search_web",
                    },
                ],
            },
            {
                "workflow_id": "wf_unrelated",
                "enabled": True,
                "settings": {},
                "steps": [{"step_kind": "agent", "agent_id": "planner"}],
            },
        ]


def test_skill_dependency_is_resolved_transitively_through_its_agents() -> None:
    assert enabled_workflow_dependencies(_RegistryDb(), agent_ids=["writer"]) == [
        "wf_bound_agent",
        "wf_direct_agent",
    ]


def test_tool_dependency_covers_agent_and_direct_connector_paths() -> None:
    assert enabled_workflow_dependencies(
        _RegistryDb(), agent_ids=["writer"], tool_id="search_web"
    ) == ["wf_bound_agent", "wf_direct_agent", "wf_direct_tool"]


def test_extension_connector_is_not_misclassified_as_registry_tool_dependency() -> None:
    workflow = {
        "workflow_id": "wf_extension_connector",
        "steps": [
            {
                "step_kind": "connector",
                "connector_id": "extension.pi_example.weather::fetch",
            }
        ],
    }

    class _Db:
        @staticmethod
        def get_tool(_tool_id: str):
            return None

    assert workflow_registry_tool_issues(_Db(), workflow) == []


def test_extension_agent_capability_is_not_misclassified_as_registry_tool_dependency() -> None:
    workflow = {
        "workflow_id": "wf_extension_agent",
        "steps": [{"step_kind": "agent", "agent_id": "extension.agent"}],
    }

    class _Db:
        @staticmethod
        def get_tool(_tool_id: str):
            return None

        @staticmethod
        def get_agent(_agent_id: str):
            return {"tools": ["extension.pi_example.content_tools"]}

    assert workflow_registry_tool_issues(_Db(), workflow) == []


def test_dependency_impact_includes_all_statuses_and_filters_account() -> None:
    db = _RegistryDb()
    workflows = db.list_workflows(enabled_only=True, include_definitions=True)
    workflows.extend(
        [
            {
                "workflow_id": "wf_disabled",
                "account_id": "acc_alpha",
                "enabled": False,
                "status": "disabled",
                "display_name": "Disabled flow",
                "steps": [{"step_kind": "connector", "connector_id": "mcp_fs"}],
            },
            {
                "workflow_id": "wf_foreign",
                "account_id": "acc_beta",
                "enabled": True,
                "steps": [{"step_kind": "agent", "agent_id": "writer"}],
            },
        ]
    )
    for workflow in workflows:
        workflow.setdefault("account_id", "acc_alpha")
    db.list_workflows = lambda **_kwargs: workflows

    assert workflow_dependency_impacts(
        db,
        account_id="acc_alpha",
        connector_id="mcp_fs",
        tool_agent_ids={"search_web": {"writer"}},
    ) == [
        {
            "display_name": "wf_bound_agent",
            "enabled": True,
            "status": "enabled",
            "tool_ids": ["search_web"],
            "via_connector": False,
            "workflow_id": "wf_bound_agent",
        },
        {
            "display_name": "wf_direct_agent",
            "enabled": True,
            "status": "enabled",
            "tool_ids": ["search_web"],
            "via_connector": False,
            "workflow_id": "wf_direct_agent",
        },
        {
            "display_name": "wf_direct_tool",
            "enabled": True,
            "status": "enabled",
            "tool_ids": ["search_web"],
            "via_connector": False,
            "workflow_id": "wf_direct_tool",
        },
        {
            "display_name": "Disabled flow",
            "enabled": False,
            "status": "disabled",
            "tool_ids": ["search_web"],
            "via_connector": True,
            "workflow_id": "wf_disabled",
        },
    ]


def test_dependency_impact_keeps_direct_connector_reference_without_projected_tools() -> None:
    db = _RegistryDb()
    db.list_workflows = lambda **_kwargs: [
        {
            "workflow_id": "wf_connector_only",
            "account_id": "acc_alpha",
            "enabled": True,
            "steps": [{"connector_id": "mcp_empty"}],
        }
    ]

    assert workflow_dependency_impacts(
        db,
        account_id="acc_alpha",
        connector_id="mcp_empty",
        tool_agent_ids={},
    ) == [
        {
            "display_name": "wf_connector_only",
            "enabled": True,
            "status": "enabled",
            "tool_ids": [],
            "via_connector": True,
            "workflow_id": "wf_connector_only",
        }
    ]
