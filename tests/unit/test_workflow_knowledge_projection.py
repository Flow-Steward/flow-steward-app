from __future__ import annotations

from typing import Any

import pytest

from core.application.workflows.knowledge_projection import (
    build_workflow_catalog_projection,
    build_workflow_knowledge_projection,
)


class _FakeKnowledgeDB:
    def __init__(self) -> None:
        self.workflows: dict[str, dict[str, Any]] = {}
        self.workflow_summaries: list[dict[str, Any]] = []
        self.agents: dict[str, dict[str, Any]] = {}
        self.agent_summaries: list[dict[str, Any]] = []
        self.agent_skills: dict[str, list[dict[str, Any]]] = {}
        self.agent_tools: dict[str, list[dict[str, Any]]] = {}
        self.skills: dict[str, dict[str, Any]] = {}
        self.skill_summaries: list[dict[str, Any]] = []
        self.tools: dict[str, dict[str, Any]] = {}
        self.tool_summaries: list[dict[str, Any]] = []
        self.step_kind_profiles: list[dict[str, Any]] = []

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return self.workflows.get(str(workflow_id))

    def list_workflows_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        del enabled_only
        del account_id
        del project_id
        return list(self.workflow_summaries)

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return self.agents.get(str(agent_id))

    def list_agents_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        del enabled_only
        del account_id
        del project_id
        return list(self.agent_summaries)

    def get_agent_skills(self, agent_id: str) -> list[dict[str, Any]]:
        return list(self.agent_skills.get(str(agent_id), []))

    def get_agent_tools(self, agent_id: str) -> list[dict[str, Any]]:
        return list(self.agent_tools.get(str(agent_id), []))

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        return self.skills.get(str(skill_id))

    def list_skills_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        del enabled_only
        del account_id
        del project_id
        return list(self.skill_summaries)

    def get_tool(self, tool_id: str) -> dict[str, Any] | None:
        return self.tools.get(str(tool_id))

    def list_tools_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        del enabled_only
        del account_id
        del project_id
        return list(self.tool_summaries)

    def list_step_kind_profiles(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        if not enabled_only:
            return list(self.step_kind_profiles)
        return [row for row in self.step_kind_profiles if row.get("enabled", True)]


def test_build_workflow_knowledge_projection_normalizes_workflow_graph(monkeypatch):
    db = _FakeKnowledgeDB()
    db.workflows["weather_brief"] = {
        "workflow_id": "weather_brief",
        "display_name": "Weather Brief",
        "description": "Summarize the weather for a city.",
        "builder_metadata_jsonb": {
            "purpose": "Fetch weather context and summarize it for downstream automation.",
            "use_cases": ["weather summaries"],
            "anti_use_cases": ["content generation"],
            "composition_role": "external_api_workflow",
            "determinism_level": "deterministic",
            "safe_for_auto_use": True,
        },
        "enabled": True,
        "tags": ["weather", "demo"],
        "groups": [
            {
                "group_id": "analysis",
                "display_name": "Analysis",
                "description": "Analyze incoming weather data.",
                "sort_order": 0,
                "body": {"kind": "sequence", "entry": {"kind": "step", "id": "inspect_weather"}},
            }
        ],
        "steps": [
            {
                "id": "analysis:1",
                "step_id": "inspect_weather",
                "display_name": "Inspect Weather",
                "step_kind": "agent",
                "agent_id": "weather_researcher",
                "skill_id": "analyze_weather",
                "group_id": "analysis",
                "lane_id": None,
                "sort_order": 0,
            },
            {
                "id": "publish:1",
                "step_id": "publish_brief",
                "display_name": "Publish Brief",
                "step_kind": "agent",
                "agent_id": "weather_researcher",
                "group_id": None,
                "lane_id": None,
                "sort_order": 1,
            },
        ],
    }
    db.agents["weather_researcher"] = {
        "agent_id": "weather_researcher",
        "display_name": "Weather Researcher",
        "description": "Analyzes weather inputs and writes a short brief.",
        "enabled": True,
        "category": "data",
        "model": "gpt-5.5",
        "_inputs": [
            {
                "name": "forecast_payload",
                "type": "json",
                "description": "Weather payload to analyze.",
                "required": True,
            }
        ],
        "_outputs": [
            {
                "name": "weather_brief",
                "type": "text",
                "description": "Rendered weather brief.",
                "required": True,
            }
        ],
    }
    db.agent_summaries = [
        {
            "agent_id": "weather_researcher",
            "display_name": "Weather Researcher",
            "description": "Analyzes weather inputs and writes a short brief.",
            "enabled": True,
            "category": "data",
            "model": "gpt-5.5",
            "builder_metadata_jsonb": {
                "purpose": "Review and summarize weather payloads.",
                "use_cases": ["analysis"],
                "anti_use_cases": ["routing"],
                "composition_role": "reasoning_step",
                "determinism_level": "agentic",
                "safe_for_auto_use": False,
            },
        }
    ]
    db.agent_skills["weather_researcher"] = [
        {
            "skill_id": "analyze_weather",
            "sort_order": 0,
            "display_name": "Analyze Weather",
            "description": "Interpret a city weather payload.",
            "category": "weather",
            "enabled": True,
        }
    ]
    db.skills["analyze_weather"] = {
        "skill_id": "analyze_weather",
        "display_name": "Analyze Weather",
        "description": "Interpret a city weather payload.",
        "category": "weather",
        "enabled": True,
        "_inputs": [
            {
                "name": "forecast_payload",
                "type": "json",
                "description": "Weather payload to analyze.",
                "required": True,
            }
        ],
        "_outputs": [
            {
                "name": "weather_brief",
                "type": "text",
                "description": "Rendered weather brief.",
                "required": True,
            }
        ],
    }
    db.skill_summaries = [
        {
            "skill_id": "analyze_weather",
            "display_name": "Analyze Weather",
            "description": "Interpret a city weather payload.",
            "category": "weather",
            "enabled": True,
            "builder_metadata_jsonb": {
                "purpose": "Interpret weather payloads into short conclusions.",
                "use_cases": ["summaries"],
                "anti_use_cases": ["API calls"],
                "composition_role": "analysis",
                "determinism_level": "hybrid",
                "safe_for_auto_use": False,
            },
        }
    ]
    db.step_kind_profiles = [
        {
            "step_kind_key": "agent",
            "display_name": "Agent Step",
            "description": "Use an agent for reasoning-heavy work.",
            "enabled": True,
            "builder_metadata_jsonb": {
                "purpose": "Delegate non-deterministic reasoning to an agent.",
                "use_cases": ["analysis", "review"],
                "anti_use_cases": ["simple transforms"],
                "composition_role": "reasoning_step",
                "determinism_level": "agentic",
                "safe_for_auto_use": False,
            },
        }
    ]

    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        lambda _db, workflow_id: {
            "workflow": {
                "id": workflow_id,
                "display_name": "Weather Brief",
                "enabled": True,
                "callable_by_parent": True,
            },
            "interface_contract": {
                "trigger_channels": ["api"],
                "expected_inputs": [{"name": "city"}],
            },
            "output_contract": {
                "expected_outputs": [{"name": "humidity"}],
            },
            "steps": [
                {
                    "id": "analysis:1",
                    "group_id": "analysis",
                    "step_id": "inspect_weather",
                    "display_name": "Inspect Weather",
                    "kind": "agent",
                    "config_summary": "agent: weather_researcher",
                    "optional": False,
                }
            ],
            "edges": [],
            "automation_rules": [
                {
                    "scope": "workflow",
                    "scope_id": "workflow",
                    "reaction_key": "on_timeout",
                    "display_name": "On Timeout",
                    "event_type": "job.timeout",
                    "action_type": "escalate",
                    "enabled": True,
                }
            ],
        },
    )

    projection = build_workflow_knowledge_projection(db, "weather_brief")
    node_ids = {node.id for node in projection.nodes}
    edge_kinds = {edge.kind for edge in projection.edges}
    edge_pairs = {(edge.kind, edge.source_id, edge.target_id) for edge in projection.edges}
    documents = {document.entity_id: document for document in projection.documents}

    assert "workflow:weather_brief" in node_ids
    assert "group:weather_brief:analysis" in node_ids
    assert "step:weather_brief:analysis:1" in node_ids
    assert "step:weather_brief:publish:1" in node_ids
    assert "agent:weather_researcher" in node_ids
    assert "skill:analyze_weather" in node_ids
    assert "reaction:weather_brief:on_timeout" in node_ids
    assert "step_kind:agent" in node_ids
    assert not any(node_id.startswith(("phase:", "phase_category:")) for node_id in node_ids)
    assert "agent_category:data" in node_ids
    assert "skill_category:weather" in node_ids
    assert "io_field:city" in node_ids
    assert "io_field:weather_brief" in node_ids

    assert "has_group" in edge_kinds
    assert "has_step" in edge_kinds
    assert "uses_agent" in edge_kinds
    assert "uses_skill" in edge_kinds
    assert "has_reaction" in edge_kinds
    assert "classifies_step_kind" in edge_kinds
    assert "classifies_agent_category" in edge_kinds
    assert "agent_has_skill" in edge_kinds
    assert "consumes_input" in edge_kinds
    assert "produces_output" in edge_kinds
    assert ("consumes_input", "workflow:weather_brief", "io_field:city") in edge_pairs
    assert ("produces_output", "skill:analyze_weather", "io_field:weather_brief") in edge_pairs
    # Every step hangs off its workflow; only the group step also hangs off its group.
    assert ("has_group", "workflow:weather_brief", "group:weather_brief:analysis") in edge_pairs
    assert ("has_step", "workflow:weather_brief", "step:weather_brief:analysis:1") in edge_pairs
    assert ("has_step", "workflow:weather_brief", "step:weather_brief:publish:1") in edge_pairs
    assert (
        "has_step",
        "group:weather_brief:analysis",
        "step:weather_brief:analysis:1",
    ) in edge_pairs
    assert not any(
        target == "step:weather_brief:publish:1" and source.startswith("group:")
        for kind, source, target in edge_pairs
        if kind == "has_step"
    )
    nodes = {node.id: node for node in projection.nodes}
    assert nodes["step:weather_brief:analysis:1"].metadata["group_id"] == "analysis"
    assert nodes["step:weather_brief:publish:1"].metadata["group_id"] is None
    assert nodes["group:weather_brief:analysis"].metadata["body_kind"] == "sequence"

    assert "workflow:weather_brief" in documents
    assert "step:weather_brief:analysis:1" in documents
    assert "Skill: analyze_weather" in documents["step:weather_brief:analysis:1"].text
    assert "Expected outputs: humidity" in documents["workflow:weather_brief"].text
    assert "Groups: Analysis" in documents["workflow:weather_brief"].text
    assert "Group ID: analysis" in documents["step:weather_brief:analysis:1"].text
    assert "Group ID" not in documents["step:weather_brief:publish:1"].text


def test_build_workflow_catalog_projection_merges_workflows_and_capabilities(monkeypatch):
    db = _FakeKnowledgeDB()
    db.workflow_summaries = [
        {"workflow_id": "weather_brief"},
        {"workflow_id": "city_summary"},
    ]
    db.workflows["weather_brief"] = {
        "workflow_id": "weather_brief",
        "display_name": "Weather Brief",
        "description": "Fetches weather and invokes a summary workflow.",
        "enabled": True,
        "groups": [],
        "steps": [
            {
                "id": "fetch:1",
                "step_id": "call_weather_api",
                "display_name": "Call Weather API",
                "step_kind": "connector",
                "group_id": None,
                "lane_id": None,
                "override": {
                    "connector": {
                        "connector_id": "connector.weather.api",
                        "operation_id": "fetch_forecast",
                    }
                },
            },
            {
                "id": "fetch:2",
                "step_id": "invoke_summary",
                "display_name": "Invoke Summary",
                "step_kind": "invoke_workflow",
                "group_id": None,
                "lane_id": None,
                "override": {"invoke_workflow": {"workflow_id": "city_summary"}},
            },
        ],
    }
    db.workflows["city_summary"] = {
        "workflow_id": "city_summary",
        "display_name": "City Summary",
        "description": "Summarizes a city report.",
        "enabled": True,
        "groups": [],
        "steps": [],
    }
    db.agent_summaries = [
        {
            "agent_id": "city_editor",
            "display_name": "City Editor",
            "description": "Edits city summaries.",
            "enabled": True,
            "category": "data",
            "model": "gpt-5.5-mini",
        }
    ]
    db.agents["city_editor"] = dict(db.agent_summaries[0])
    db.agent_tools["city_editor"] = [
        {
            "tool_id": "connector.weather.api",
            "sort_order": 0,
            "display_name": "Weather API",
            "category": "connector",
            "enabled": True,
        }
    ]
    db.tools["connector.weather.api"] = {
        "tool_id": "connector.weather.api",
        "display_name": "Weather API",
        "description": "Fetch weather payloads.",
        "category": "connector",
        "impl_type": "extension",
        "enabled": True,
        "interface": {
            "functions": [
                {
                    "name": "fetch_forecast",
                    "description": "Fetch a city forecast payload.",
                    "parameters": [
                        {
                            "name": "city",
                            "type": "string",
                            "description": "City name to query.",
                            "required": True,
                        }
                    ],
                    "returns": {
                        "type": "object",
                        "properties": {
                            "forecast_payload": {
                                "type": "object",
                                "description": "Forecast payload for downstream processing.",
                            }
                        },
                        "required": ["forecast_payload"],
                    },
                }
            ]
        },
    }
    db.tool_summaries = [
        {
            "tool_id": "connector.weather.api",
            "display_name": "Weather API",
            "description_preview": "Fetch weather payloads.",
            "category": "connector",
            "impl_type": "extension",
            "enabled": True,
        }
    ]

    def _graph_for(_db, workflow_id: str):
        if workflow_id == "city_summary":
            return {
                "workflow": {"id": workflow_id, "display_name": "City Summary", "enabled": True},
                "interface_contract": {},
                "output_contract": {},
                "steps": [],
                "edges": [],
                "automation_rules": [],
            }
        return {
            "workflow": {"id": workflow_id, "display_name": "Weather Brief", "enabled": True},
            "interface_contract": {"expected_inputs": [{"name": "city"}]},
            "output_contract": {"expected_outputs": [{"name": "forecast_payload"}]},
            "steps": [
                {
                    "id": "fetch:1",
                    "group_id": None,
                    "step_id": "call_weather_api",
                    "display_name": "Call Weather API",
                    "kind": "connector",
                    "config_summary": "connector.weather.api -> fetch_forecast",
                    "optional": False,
                },
                {
                    "id": "fetch:2",
                    "group_id": None,
                    "step_id": "invoke_summary",
                    "display_name": "Invoke Summary",
                    "kind": "invoke_workflow",
                    "config_summary": "invoke city_summary",
                    "optional": False,
                },
            ],
            "edges": [
                {
                    "id": "fetch-seq",
                    "kind": "seq",
                    "source_step_id": "fetch:1",
                    "target_step_id": "fetch:2",
                    "metadata": {},
                }
            ],
            "automation_rules": [],
        }

    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _graph_for,
    )

    projection = build_workflow_catalog_projection(db)
    node_ids = {node.id for node in projection.nodes}
    edge_pairs = {(edge.kind, edge.source_id, edge.target_id) for edge in projection.edges}

    assert "workflow:weather_brief" in node_ids
    assert "workflow:city_summary" in node_ids
    assert "tool:connector.weather.api" in node_ids
    assert "tool_action:connector.weather.api:fetch_forecast" in node_ids
    assert "agent:city_editor" in node_ids
    assert "tool_category:connector" in node_ids
    assert "io_field:city" in node_ids
    assert "io_field:forecast_payload" in node_ids

    assert (
        "uses_tool",
        "step:weather_brief:fetch:1",
        "tool:connector.weather.api",
    ) in edge_pairs
    assert (
        "uses_tool_action",
        "step:weather_brief:fetch:1",
        "tool_action:connector.weather.api:fetch_forecast",
    ) in edge_pairs
    assert (
        "tool_has_action",
        "tool:connector.weather.api",
        "tool_action:connector.weather.api:fetch_forecast",
    ) in edge_pairs
    assert (
        "consumes_input",
        "tool_action:connector.weather.api:fetch_forecast",
        "io_field:city",
    ) in edge_pairs
    assert (
        "produces_output",
        "tool_action:connector.weather.api:fetch_forecast",
        "io_field:forecast_payload",
    ) in edge_pairs
    assert (
        "invokes_workflow",
        "step:weather_brief:fetch:2",
        "workflow:city_summary",
    ) in edge_pairs
    assert (
        "agent_has_tool",
        "agent:city_editor",
        "tool:connector.weather.api",
    ) in edge_pairs
    assert (
        "flows_to",
        "step:weather_brief:fetch:1",
        "step:weather_brief:fetch:2",
    ) in edge_pairs


def test_build_workflow_catalog_projection_skips_explicitly_hidden_and_disabled_rows(
    monkeypatch,
):
    db = _FakeKnowledgeDB()
    db.workflow_summaries = [
        {"workflow_id": "visible_flow", "enabled": True, "step_count": 1},
        {"workflow_id": "e2e_hidden_flow", "enabled": False, "step_count": 1},
        {
            "workflow_id": "test_named_visible_flow",
            "enabled": True,
            "step_count": 1,
            "builder_metadata_jsonb": {},
        },
        {
            "workflow_id": "hidden_flow",
            "enabled": True,
            "step_count": 1,
            "visible_in_catalog": False,
        },
        {
            "workflow_id": "generic_hidden_flag_flow",
            "enabled": True,
            "step_count": 1,
            "builder_metadata_jsonb": {"hidden": True},
        },
    ]
    db.workflows["visible_flow"] = {
        "workflow_id": "visible_flow",
        "display_name": "Visible Flow",
        "description": "Visible workflow.",
        "enabled": True,
        "groups": [],
        "steps": [],
    }
    db.workflows["e2e_hidden_flow"] = {
        "workflow_id": "e2e_hidden_flow",
        "display_name": "Hidden Flow",
        "description": "Should not be projected.",
        "enabled": False,
        "groups": [],
        "steps": [],
    }
    db.workflows["test_named_visible_flow"] = {
        "workflow_id": "test_named_visible_flow",
        "display_name": "Test Flow",
        "description": "Should be projected because hidden policy is not name-based.",
        "enabled": True,
        "groups": [],
        "steps": [],
    }
    db.workflows["hidden_flow"] = {
        "workflow_id": "hidden_flow",
        "display_name": "Hidden Flow",
        "description": "Should not be projected.",
        "enabled": True,
        "groups": [],
        "steps": [],
    }
    db.workflows["generic_hidden_flag_flow"] = {
        "workflow_id": "generic_hidden_flag_flow",
        "display_name": "Generic Hidden Flag Flow",
        "description": "Should be projected because generic hidden is not a catalog policy.",
        "enabled": True,
        "groups": [],
        "steps": [],
    }
    db.agent_summaries = [
        {
            "agent_id": "visible_agent",
            "display_name": "Visible Agent",
            "enabled": True,
            "category": "data",
        },
        {
            "agent_id": "e2e_hidden_agent",
            "display_name": "Hidden Agent",
            "enabled": False,
            "category": "data",
        },
    ]
    db.agents["visible_agent"] = {
        "agent_id": "visible_agent",
        "display_name": "Visible Agent",
        "enabled": True,
        "_inputs": [],
        "_outputs": [],
    }
    db.agents["e2e_hidden_agent"] = {
        "agent_id": "e2e_hidden_agent",
        "display_name": "Hidden Agent",
        "enabled": False,
        "_inputs": [],
        "_outputs": [],
    }

    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        lambda _db, workflow_id: {
            "workflow": {"id": workflow_id, "display_name": workflow_id, "enabled": True},
            "interface_contract": {},
            "output_contract": {},
            "steps": [],
            "edges": [],
            "automation_rules": [],
        },
    )

    projection = build_workflow_catalog_projection(db)
    node_ids = {node.id for node in projection.nodes}

    assert "workflow:visible_flow" in node_ids
    assert "workflow:e2e_hidden_flow" not in node_ids
    assert "workflow:test_named_visible_flow" in node_ids
    assert "workflow:hidden_flow" not in node_ids
    assert "workflow:generic_hidden_flag_flow" in node_ids
    assert "agent:visible_agent" in node_ids
    assert "agent:e2e_hidden_agent" not in node_ids


@pytest.mark.parametrize(
    "current_metadata, expected_purpose",
    [(None, ""), ({}, ""), ({"purpose": "Current guidance"}, "Current guidance")],
)
def test_catalog_projection_ignores_alternate_serialized_metadata(
    current_metadata, expected_purpose
):
    db = _FakeKnowledgeDB()
    for kind in ("workflow", "agent", "skill", "tool"):
        row = {
            f"{kind}_id": "current",
            "enabled": True,
            "step_count": 1,
            "builder_metadata": {"purpose": "Alternate guidance"},
        }
        if current_metadata is not None:
            row["builder_metadata_jsonb"] = current_metadata
        getattr(db, f"{kind}s")["current"] = row
        getattr(db, f"{kind}_summaries").append(row)

    projection = build_workflow_catalog_projection(db)
    nodes = {node.id: node for node in projection.nodes}

    for kind in ("workflow", "agent", "skill", "tool"):
        node = nodes[f"{kind}:current"]
        assert node.metadata["builder_metadata_jsonb"].get("purpose", "") == expected_purpose
        assert "Alternate guidance" not in node.text
        if expected_purpose:
            assert "Current guidance" in node.text


def test_build_workflow_catalog_projection_skips_blank_workflow_rows(monkeypatch):
    db = _FakeKnowledgeDB()
    db.workflow_summaries = [
        {"workflow_id": "blank_flow", "enabled": True, "step_count": 0},
        {"workflow_id": "usable_flow", "enabled": True, "step_count": 1},
    ]
    db.workflows["blank_flow"] = {
        "workflow_id": "blank_flow",
        "display_name": "Blank Flow",
        "description": "Starter canvas only.",
        "enabled": True,
        "groups": [],
        "steps": [],
    }
    db.workflows["usable_flow"] = {
        "workflow_id": "usable_flow",
        "display_name": "Usable Flow",
        "description": "Projected reusable workflow.",
        "enabled": True,
        "groups": [],
        "steps": [
            {
                "id": "work:1",
                "step_id": "do_work",
                "display_name": "Do work",
                "step_kind": "agent",
                "group_id": None,
                "lane_id": None,
            }
        ],
    }

    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        lambda _db, workflow_id: {
            "workflow": {"id": workflow_id, "display_name": workflow_id, "enabled": True},
            "interface_contract": {},
            "output_contract": {},
            "steps": [],
            "edges": [],
            "automation_rules": [],
        },
    )

    projection = build_workflow_catalog_projection(db)
    node_ids = {node.id for node in projection.nodes}

    assert "workflow:blank_flow" not in node_ids
    assert "workflow:usable_flow" in node_ids


def test_build_workflow_catalog_projection_ignores_null_like_references(monkeypatch):
    db = _FakeKnowledgeDB()
    db.workflow_summaries = [{"workflow_id": "visible_flow", "enabled": True, "step_count": 1}]
    db.workflows["visible_flow"] = {
        "workflow_id": "visible_flow",
        "display_name": "Visible Flow",
        "description": "Visible workflow.",
        "enabled": True,
        "groups": [],
        "steps": [
            {
                "id": "build:1",
                "step_id": "call_unknown_tool",
                "display_name": "Call Unknown Tool",
                "step_kind": "connector",
                "group_id": None,
                "lane_id": None,
                "override": {"connector": {"connector_id": "None", "operation_id": "None"}},
            },
            {
                "id": "build:2",
                "step_id": "invoke_unknown_workflow",
                "display_name": "Invoke Unknown Workflow",
                "step_kind": "invoke_workflow",
                "group_id": None,
                "lane_id": None,
                "override": {"invoke_workflow": {"workflow_id": "None"}},
            },
        ],
    }

    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        lambda _db, workflow_id: {
            "workflow": {"id": workflow_id, "display_name": workflow_id, "enabled": True},
            "interface_contract": {},
            "output_contract": {},
            "steps": [
                {
                    "id": "build:1",
                    "group_id": None,
                    "step_id": "call_unknown_tool",
                    "display_name": "Call Unknown Tool",
                    "kind": "connector",
                    "config_summary": "connector none",
                    "optional": False,
                },
                {
                    "id": "build:2",
                    "group_id": None,
                    "step_id": "invoke_unknown_workflow",
                    "display_name": "Invoke Unknown Workflow",
                    "kind": "invoke_workflow",
                    "config_summary": "invoke none",
                    "optional": False,
                },
            ],
            "edges": [],
            "automation_rules": [],
        },
    )

    projection = build_workflow_catalog_projection(db)
    node_ids = {node.id for node in projection.nodes}

    assert "tool:None" not in node_ids
    assert "tool_action:None:None" not in node_ids


def test_build_workflow_knowledge_projection_includes_builder_guidance_and_step_kind_profiles(
    monkeypatch,
):
    db = _FakeKnowledgeDB()
    db.workflows["review_flow"] = {
        "workflow_id": "review_flow",
        "display_name": "Review Flow",
        "description": "Routes generated content into review.",
        "builder_metadata_jsonb": {
            "purpose": "Route generated content into a review checkpoint before completion.",
            "use_cases": ["review routing"],
            "anti_use_cases": ["dataset queries"],
            "composition_role": "review_workflow",
            "determinism_level": "hybrid",
            "safe_for_auto_use": False,
        },
        "enabled": True,
        "groups": [],
        "steps": [
            {
                "id": "review:1",
                "step_id": "approve_copy",
                "display_name": "Approve copy",
                "step_kind": "switch",
                "group_id": None,
                "lane_id": None,
            }
        ],
    }
    db.step_kind_profiles = [
        {
            "step_kind_key": "switch",
            "display_name": "Switch Branch",
            "description": "Route across named cases.",
            "enabled": True,
            "builder_metadata_jsonb": {
                "purpose": "Branch across multiple explicit outcomes.",
                "use_cases": ["routing"],
                "anti_use_cases": ["list iteration"],
                "composition_role": "branching",
                "determinism_level": "deterministic",
                "safe_for_auto_use": True,
            },
        }
    ]

    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        lambda _db, workflow_id: {
            "workflow": {"id": workflow_id, "display_name": "Review Flow", "enabled": True},
            "interface_contract": {"expected_inputs": []},
            "output_contract": {"expected_outputs": []},
            "steps": [
                {
                    "id": "review:1",
                    "group_id": None,
                    "step_id": "approve_copy",
                    "display_name": "Approve copy",
                    "kind": "switch",
                    "config_summary": "selector=state.review_result",
                    "optional": False,
                }
            ],
            "edges": [],
            "automation_rules": [],
        },
    )

    projection = build_workflow_knowledge_projection(db, "review_flow")
    documents = {document.id: document.text for document in projection.documents}

    assert (
        "Builder guidance: Route generated content into a review checkpoint before completion."
        in documents["doc:workflow:review_flow"]
    )
    assert (
        "Builder guidance: Branch across multiple explicit outcomes."
        in documents["doc:step_kind:switch"]
    )
