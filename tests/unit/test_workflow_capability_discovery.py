from __future__ import annotations

import copy
import uuid

import pytest

from core.application.management.scope import normalize_scope
from core.application.workflows.discovery import (
    capability_discovery as capability_discovery_module,
)
from core.application.workflows.discovery.capability_discovery import (
    WorkflowCapabilityDiscovery,
)
from core.application.workflows.discovery.scoped_capability_catalog import (
    ScopedCapabilityCatalog,
)
from core.application.workflows.knowledge_projection import (
    WorkflowKnowledgeDocument,
    WorkflowKnowledgeEdge,
    WorkflowKnowledgeHit,
    WorkflowKnowledgeNode,
    WorkflowKnowledgeProjection,
)


@pytest.fixture(autouse=True)
def _clear_workflow_knowledge_sync_cache():
    capability_discovery_module._WORKFLOW_KNOWLEDGE_SYNC_CACHE.clear()
    yield
    capability_discovery_module._WORKFLOW_KNOWLEDGE_SYNC_CACHE.clear()


class _FakeWorkflowKnowledgeBackend:
    def __init__(
        self,
        *,
        hits: list[WorkflowKnowledgeHit] | None = None,
        projection: WorkflowKnowledgeProjection | None = None,
        fail_upsert: bool = False,
    ) -> None:
        self._hits = list(hits or [])
        self._projection = projection or WorkflowKnowledgeProjection()
        self._fail_upsert = fail_upsert
        self.upsert_calls: list[dict[str, object]] = []
        self.search_calls: list[dict[str, object]] = []
        self.scope_digests: dict[tuple[str, str, str], str] = {}

    def get_scope_sync_digest(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> str:
        return self.scope_digests.get(
            (account_id, str(tenant_id or "").strip(), str(project_id or "").strip()), ""
        )

    def set_scope_sync_digest(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        digest: str,
    ) -> None:
        self.scope_digests[
            (account_id, str(tenant_id or "").strip(), str(project_id or "").strip())
        ] = str(digest or "")

    def upsert_projection(
        self,
        *,
        projection: WorkflowKnowledgeProjection,
        account_id: str = "",
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        if self._fail_upsert:
            raise RuntimeError("workflow knowledge unavailable")
        self.upsert_calls.append(
            {
                "account_id": account_id,
                "tenant_id": str(tenant_id or "").strip(),
                "project_id": project_id,
                "node_count": len(projection.nodes),
                "edge_count": len(projection.edges),
                "doc_count": len(projection.documents),
            }
        )

    def delete_scope(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        workflow_ids=(),
    ) -> None:
        del account_id, tenant_id, project_id, workflow_ids

    def search(
        self,
        *,
        query: str,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        entity_kinds=(),
        limit: int = 8,
    ) -> list[WorkflowKnowledgeHit]:
        self.search_calls.append(
            {
                "query": query,
                "account_id": account_id,
                "tenant_id": str(tenant_id or "").strip(),
                "project_id": project_id,
                "entity_kinds": list(entity_kinds),
                "limit": limit,
            }
        )
        return list(self._hits)

    def neighbors(
        self,
        *,
        entity_id: str,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        edge_kinds=(),
        depth: int = 1,
        limit: int = 20,
    ):
        del entity_id, account_id, tenant_id, project_id, edge_kinds, depth, limit
        return []

    def fetch_subgraph(
        self,
        *,
        entity_ids,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        edge_kinds=(),
        depth: int = 2,
        limit: int = 100,
    ) -> WorkflowKnowledgeProjection:
        del entity_ids, account_id, tenant_id, project_id, edge_kinds, depth, limit
        return self._projection


class _FakeWorkflowDB:
    def __init__(self) -> None:
        self.summary_call_kwargs: list[dict[str, object]] = []
        self.workflows = {
            "wf_current": {
                "workflow_id": "wf_current",
                "display_name": "Current Draft",
                "description": "Current editing canvas",
                "builder_metadata_jsonb": {
                    "purpose": "Edit the current workflow safely.",
                    "use_cases": ["workflow refinement"],
                    "anti_use_cases": ["greenfield authoring"],
                    "composition_role": "workflow_refinement",
                    "determinism_level": "hybrid",
                    "safe_for_auto_use": False,
                },
                "enabled": True,
                "_phases": [],
                "tags": [],
            },
            "weather_brief": {
                "workflow_id": "weather_brief",
                "display_name": "Weather Brief",
                "description": "Fetch weather forecast by city and return a summary.",
                "builder_metadata_jsonb": {
                    "purpose": "Fetch external weather data and shape it into a short brief.",
                    "use_cases": ["weather lookup", "status summary"],
                    "anti_use_cases": ["content generation"],
                    "composition_role": "external_data_fetch",
                    "determinism_level": "deterministic",
                    "safe_for_auto_use": True,
                },
                "enabled": True,
                "_phases": [
                    {
                        "phase_id": "collect",
                        "category": "collect",
                        "display_name": "Collect data",
                        "_steps": [
                            {
                                "id": "collect:1",
                                "display_name": "Fetch forecast",
                                "step_kind": "connector",
                                "connector_id": "weather_api",
                                "operation_id": "fetch_forecast",
                            }
                        ],
                    }
                ],
                "tags": ["weather", "forecast"],
            },
        }
        self.workflow_rows = [
            {
                "workflow_id": "wf_current",
                "display_name": "Current Draft",
                "description": "Current editing canvas",
                "step_count": 0,
                "builder_metadata_jsonb": {
                    "purpose": "Edit the current workflow safely.",
                    "use_cases": ["workflow refinement"],
                    "anti_use_cases": ["greenfield authoring"],
                    "composition_role": "workflow_refinement",
                    "determinism_level": "hybrid",
                    "safe_for_auto_use": False,
                },
                "enabled": True,
                "visible_in_catalog": True,
                "tags": [],
            },
            {
                "workflow_id": "weather_brief",
                "display_name": "Weather Brief",
                "description": "Fetch weather forecast by city and return a summary.",
                "step_count": 1,
                "builder_metadata_jsonb": {
                    "purpose": "Fetch external weather data and shape it into a short brief.",
                    "use_cases": ["weather lookup", "status summary"],
                    "anti_use_cases": ["content generation"],
                    "composition_role": "external_data_fetch",
                    "determinism_level": "deterministic",
                    "safe_for_auto_use": True,
                },
                "enabled": True,
                "visible_in_catalog": True,
                "tags": ["weather", "forecast"],
            },
        ]
        self.agent_rows = [
            {
                "agent_id": "research_agent",
                "display_name": "Research Agent",
                "description": "Summarize and analyze support tickets.",
                "enabled": True,
                "visible_in_catalog": True,
                "category": "ai",
                "skill_names": "ticket_summary",
                "builder_metadata_jsonb": {
                    "purpose": "Analyze or review inbound support cases before a human or system action.",
                    "use_cases": ["support triage", "risk review"],
                    "anti_use_cases": ["direct API calls"],
                    "composition_role": "reasoning_step",
                    "determinism_level": "agentic",
                    "safe_for_auto_use": False,
                },
            }
        ]
        self.skill_rows = [
            {
                "skill_id": "ticket_summary",
                "display_name": "Ticket Summary",
                "description": "Summarize ticket content into short actionable notes.",
                "enabled": True,
                "visible_in_catalog": True,
                "category": "data",
                "builder_metadata_jsonb": {
                    "purpose": "Extract concise conclusions from support messages.",
                    "use_cases": ["summarization", "triage notes"],
                    "anti_use_cases": ["external delivery"],
                    "composition_role": "analysis",
                    "determinism_level": "hybrid",
                    "safe_for_auto_use": False,
                },
            }
        ]
        self.tool_rows = [
            {
                "tool_id": "weather_api",
                "display_name": "Weather API",
                "description": "Fetch forecast data by city.",
                "enabled": True,
                "visible_in_catalog": True,
                "category": "connector",
                "impl_type": "http",
                "builder_metadata_jsonb": {
                    "purpose": "Call a deterministic external weather endpoint.",
                    "use_cases": ["forecast lookup", "current weather fetch"],
                    "anti_use_cases": ["text generation"],
                    "composition_role": "external_action",
                    "determinism_level": "deterministic",
                    "safe_for_auto_use": True,
                },
            }
        ]
        self.step_kind_profile_rows = [
            {
                "step_kind_key": "connector",
                "display_name": "Connector Step",
                "description": "Invoke a deterministic tool or connector operation.",
                "enabled": True,
                "builder_metadata_jsonb": {
                    "purpose": "Execute an external or builtin deterministic operation.",
                    "use_cases": ["API calls", "connector actions"],
                    "anti_use_cases": ["open-ended reasoning"],
                    "composition_role": "external_action",
                    "determinism_level": "deterministic",
                    "safe_for_auto_use": True,
                },
            },
            {
                "step_kind_key": "set_variable",
                "display_name": "Set Variable",
                "description": "Shape state with deterministic assignments.",
                "enabled": True,
                "builder_metadata_jsonb": {
                    "purpose": "Prepare state for later steps.",
                    "use_cases": ["payload shaping"],
                    "anti_use_cases": ["external side effects"],
                    "composition_role": "deterministic_transform",
                    "determinism_level": "deterministic",
                    "safe_for_auto_use": True,
                },
            },
            {
                "step_kind_key": "stop_success",
                "display_name": "Stop Success",
                "description": "End the workflow successfully.",
                "enabled": True,
                "builder_metadata_jsonb": {
                    "purpose": "Terminate the workflow with success.",
                    "use_cases": ["terminal completion"],
                    "anti_use_cases": ["mid-workflow processing"],
                    "composition_role": "terminal",
                    "determinism_level": "deterministic",
                    "safe_for_auto_use": True,
                },
            },
        ]

    def get_workflow(self, workflow_id: str):
        row = self.workflows.get(str(workflow_id))
        return copy.deepcopy(row) if isinstance(row, dict) else None

    def list_workflows_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ):
        self.summary_call_kwargs.append(
            {
                "getter": "workflow",
                "enabled_only": enabled_only,
                "account_id": account_id,
                "project_id": project_id,
            }
        )
        if not enabled_only:
            return copy.deepcopy(self.workflow_rows)
        return [row for row in copy.deepcopy(self.workflow_rows) if row.get("enabled", True)]

    def list_agents_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ):
        self.summary_call_kwargs.append(
            {
                "getter": "agent",
                "enabled_only": enabled_only,
                "account_id": account_id,
                "project_id": project_id,
            }
        )
        if not enabled_only:
            return copy.deepcopy(self.agent_rows)
        return [row for row in copy.deepcopy(self.agent_rows) if row.get("enabled", True)]

    def list_skills_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ):
        self.summary_call_kwargs.append(
            {
                "getter": "skill",
                "enabled_only": enabled_only,
                "account_id": account_id,
                "project_id": project_id,
            }
        )
        if not enabled_only:
            return copy.deepcopy(self.skill_rows)
        return [row for row in copy.deepcopy(self.skill_rows) if row.get("enabled", True)]

    def list_tools_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ):
        self.summary_call_kwargs.append(
            {
                "getter": "tool",
                "enabled_only": enabled_only,
                "account_id": account_id,
                "project_id": project_id,
            }
        )
        if not enabled_only:
            return copy.deepcopy(self.tool_rows)
        return [row for row in copy.deepcopy(self.tool_rows) if row.get("enabled", True)]

    def list_step_kind_profiles(self, enabled_only: bool = False):
        if not enabled_only:
            return copy.deepcopy(self.step_kind_profile_rows)
        return [
            row for row in copy.deepcopy(self.step_kind_profile_rows) if row.get("enabled", True)
        ]

    def get_agent(self, agent_id: str):
        if agent_id != "research_agent":
            return None
        return {
            "agent_id": "research_agent",
            "display_name": "Research Agent",
            "description": "Summarize and analyze support tickets.",
            "_inputs": [{"name": "ticket_text", "type": "text", "required": True}],
            "_outputs": [{"name": "summary", "type": "text", "required": True}],
        }

    def get_agent_inputs(self, agent_id: str):
        if agent_id == "research_agent":
            return [{"name": "ticket_text", "type": "text", "required": True}]
        return []

    def get_agent_outputs(self, agent_id: str):
        if agent_id == "research_agent":
            return [{"name": "summary", "type": "text", "required": True}]
        return []

    def get_agent_skills(self, agent_id: str):
        if agent_id == "research_agent":
            return [{"skill_id": "ticket_summary", "sort_order": 0}]
        return []

    def get_agent_tools(self, agent_id: str):
        return []

    def get_skill(self, skill_id: str):
        if skill_id != "ticket_summary":
            return None
        return {
            "skill_id": "ticket_summary",
            "display_name": "Ticket Summary",
            "description": "Summarize ticket content into short actionable notes.",
            "_inputs": [{"name": "ticket_text", "type": "text", "required": True}],
            "_outputs": [{"name": "summary", "type": "text", "required": True}],
        }

    def get_skill_inputs(self, skill_id: str):
        if skill_id == "ticket_summary":
            return [{"name": "ticket_text", "type": "text", "required": True}]
        return []

    def get_skill_outputs(self, skill_id: str):
        if skill_id == "ticket_summary":
            return [{"name": "summary", "type": "text", "required": True}]
        return []

    def get_tool(self, tool_id: str):
        if tool_id != "weather_api":
            return None
        return {
            "tool_id": "weather_api",
            "display_name": "Weather API",
            "description": "Fetch forecast data by city.",
            "interface": {
                "operations": [
                    {
                        "name": "fetch_forecast",
                        "display_name": "Fetch Forecast",
                        "description": "Retrieve forecast by city name.",
                        "parameters": [
                            {
                                "name": "city",
                                "type": "text",
                                "description": "City to forecast.",
                                "required": True,
                                "schema": {"type": "string", "minLength": 1},
                            }
                        ],
                        "returns": {
                            "type": "object",
                            "properties": {
                                "forecast": {
                                    "type": "text",
                                    "description": "Forecast summary",
                                }
                            },
                            "required": ["forecast"],
                        },
                    }
                ]
            },
        }


class _CountingScopedToolDB(_FakeWorkflowDB):
    def __init__(self) -> None:
        super().__init__()
        self.get_tool_calls = 0
        self.tool_rows = [
            {
                "tool_id": f"tool_{index}",
                "display_name": f"Scoped Tool {index}",
                "description": "sync customer records safely",
                "enabled": True,
                "visible_in_catalog": True,
                "category": "connector",
                "impl_type": "http",
                "account_id": "acct_test" if index % 2 == 0 else "acct_other",
                "scope_kind": "account",
                "builder_metadata_jsonb": {
                    "purpose": "Run a deterministic scoped connector action.",
                    "use_cases": ["sync records"],
                    "anti_use_cases": ["freeform reasoning"],
                    "composition_role": "external_action",
                    "determinism_level": "deterministic",
                    "safe_for_auto_use": True,
                },
            }
            for index in range(250)
        ]
        self.tool_rows[18]["tool_id"] = "customer_sync"
        self.tool_rows[18]["display_name"] = "Customer Sync"
        self.tool_rows[18]["description"] = "sync customer crm records"

    def get_tool(self, tool_id: str):
        self.get_tool_calls += 1
        normalized = str(tool_id or "").strip()
        for row in self.tool_rows:
            if str(row.get("tool_id") or "") != normalized:
                continue
            return {
                **copy.deepcopy(row),
                "interface": {
                    "operations": [
                        {
                            "id": "sync_records",
                            "display_name": "Sync Records",
                            "description": "Synchronize customer records.",
                            "input_schema": {
                                "type": "object",
                                "properties": {"customer_id": {"type": "string"}},
                                "required": ["customer_id"],
                            },
                            "output_schema": {
                                "type": "object",
                                "properties": {"sync_status": {"type": "string"}},
                                "required": ["sync_status"],
                            },
                        }
                    ]
                },
            }
        return None


class _CountingScopedWorkflowDB(_FakeWorkflowDB):
    def __init__(self) -> None:
        super().__init__()
        self.get_workflow_calls = 0
        self.workflow_rows = [
            {
                "workflow_id": "noise_weather",
                "display_name": "Weather Notes",
                "description": "Summarize the weather forecast for a city.",
                "enabled": True,
                "visible_in_catalog": True,
                "step_count": 2,
                "builder_metadata_jsonb": {},
            },
            {
                "workflow_id": "support_triage_flow",
                "display_name": "Support Triage",
                "description": "Triage inbound support requests and return suggested replies.",
                "enabled": True,
                "visible_in_catalog": True,
                "step_count": 3,
                "builder_metadata_jsonb": {
                    "purpose": "Triage support requests and prepare a suggested reply.",
                    "use_cases": ["support triage", "billing support routing"],
                    "composition_role": "workflow_pattern",
                },
            },
        ]
        self.workflows["noise_weather"] = {
            "workflow_id": "noise_weather",
            "display_name": "Weather Notes",
            "settings": {
                "callable": {
                    "expected_inputs": [{"name": "city", "type": "text"}],
                    "expected_outputs": [{"name": "forecast", "type": "text"}],
                }
            },
        }
        self.workflows["support_triage_flow"] = {
            "workflow_id": "support_triage_flow",
            "display_name": "Support Triage",
            "settings": {
                "callable": {
                    "expected_inputs": [{"name": "support_request", "type": "text"}],
                    "expected_outputs": [{"name": "suggested_reply", "type": "text"}],
                }
            },
        }

    def get_workflow(self, workflow_id: str):
        self.get_workflow_calls += 1
        return super().get_workflow(workflow_id)


def _fake_canvas_graph(db, workflow_id: str):
    workflow = db.get_workflow(workflow_id) or {}
    phases = workflow.get("_phases") or []
    steps = []
    for phase in phases:
        for index, step in enumerate(phase.get("_steps") or []):
            steps.append(
                {
                    "id": str(step.get("id") or f"{phase.get('phase_id')}:{index + 1}"),
                    "phase_id": str(phase.get("phase_id") or ""),
                    "display_name": str(step.get("display_name") or ""),
                    "kind": str(step.get("step_kind") or ""),
                    "config_summary": str(step.get("display_name") or ""),
                }
            )
    return {
        "workflow": {
            "id": workflow_id,
            "display_name": str(workflow.get("display_name") or workflow_id),
            "enabled": True,
            "callable_by_parent": True,
        },
        "interface_contract": {"expected_inputs": [{"name": "city", "type": "text"}]},
        "output_contract": {"expected_outputs": [{"name": "forecast", "type": "text"}]},
        "phases": [
            {
                "id": str(phase.get("phase_id") or ""),
                "display_name": str(phase.get("display_name") or ""),
                "sort_order": phase_index,
            }
            for phase_index, phase in enumerate(phases)
        ],
        "steps": steps,
        "edges": [],
        "automation_rules": [],
    }


def test_discovery_skips_request_path_workflow_knowledge_sync_and_falls_back_to_visible_candidates(
    monkeypatch,
):
    monkeypatch.setenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "1")
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend(
        hits=[
            WorkflowKnowledgeHit(
                entity_id="tool_action:weather_api:fetch_forecast",
                entity_kind="tool_action",
                score=94.0,
                title="Fetch Forecast",
            ),
            WorkflowKnowledgeHit(
                entity_id="io_field:city",
                entity_kind="io_field",
                score=82.0,
                title="city",
            ),
        ],
        projection=WorkflowKnowledgeProjection(
            nodes=[
                WorkflowKnowledgeNode(
                    id="tool:weather_api",
                    kind="tool",
                    label="Weather API",
                    metadata={"tool_id": "weather_api"},
                ),
                WorkflowKnowledgeNode(
                    id="tool_action:weather_api:fetch_forecast",
                    kind="tool_action",
                    label="Fetch Forecast",
                    metadata={"tool_id": "weather_api", "operation_id": "fetch_forecast"},
                ),
                WorkflowKnowledgeNode(
                    id="workflow:weather_brief",
                    kind="workflow",
                    label="Weather Brief",
                    metadata={"workflow_id": "weather_brief"},
                ),
                WorkflowKnowledgeNode(
                    id="io_field:city",
                    kind="io_field",
                    label="city",
                    metadata={"field_name": "city"},
                ),
            ],
            edges=[
                WorkflowKnowledgeEdge(
                    id="tool_has_action:weather_api:fetch_forecast",
                    kind="tool_has_action",
                    source_id="tool:weather_api",
                    target_id="tool_action:weather_api:fetch_forecast",
                ),
                WorkflowKnowledgeEdge(
                    id="tool_action_input:fetch_forecast:city",
                    kind="consumes_input",
                    source_id="tool_action:weather_api:fetch_forecast",
                    target_id="io_field:city",
                ),
                WorkflowKnowledgeEdge(
                    id="workflow_input:weather_brief:city",
                    kind="consumes_input",
                    source_id="workflow:weather_brief",
                    target_id="io_field:city",
                ),
            ],
            documents=[
                WorkflowKnowledgeDocument(
                    id="doc:tool_action:weather_api:fetch_forecast",
                    entity_id="tool_action:weather_api:fetch_forecast",
                    entity_kind="tool_action",
                    title="Fetch Forecast",
                    text="Fetch forecast data by city.",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Build a weather forecast workflow by city",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    assert backend.upsert_calls == []
    assert backend.search_calls == []
    candidates = {(candidate.type, candidate.id): candidate for candidate in result["candidates"]}
    assert ("tool", "weather_api") in candidates
    assert ("workflow", "weather_brief") in candidates
    weather_tool = candidates[("tool", "weather_api")]
    assert weather_tool.function_name == "fetch_forecast"
    assert weather_tool.operation_id == "fetch_forecast"
    assert result["workflow_knowledge"]["lexical_fallback_used"] is True


def test_discovery_blocks_lexical_fallback_when_workflow_knowledge_stale_and_disabled(
    monkeypatch,
):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend()
    monkeypatch.setenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "false")
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Build a weather forecast workflow by city",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    candidates = {(candidate.type, candidate.id) for candidate in result["candidates"]}
    assert ("tool", "weather_api") not in candidates
    assert result["workflow_knowledge"]["freshness"] == "empty"
    assert result["workflow_knowledge"]["lexical_fallback_blocked"] is True
    assert backend.search_calls == []


def test_discovery_tool_candidates_include_declared_contract_names():
    db = _FakeWorkflowDB()
    discovery = WorkflowCapabilityDiscovery(db)

    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Build a weather forecast workflow by city",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    weather_tool = next(
        candidate
        for candidate in result["candidates"]
        if candidate.type == "tool" and candidate.id == "weather_api"
    )
    assert weather_tool.function_name == "fetch_forecast"
    assert weather_tool.operation_id == "fetch_forecast"
    assert weather_tool.operation_display_name == "Fetch Forecast"
    assert weather_tool.input_names == ["city"]
    assert weather_tool.output_names == ["forecast"]
    assert weather_tool.input_contracts == [
        {
            "name": "city",
            "type": "text",
            "required": True,
            "description": "City to forecast.",
            "schema": {"type": "string", "minLength": 1},
        }
    ]


def test_discovery_workflow_candidates_include_hydrated_callable_contract_names():
    db = _FakeWorkflowDB()
    db.workflows["weather_brief"]["settings"] = {
        "callable": {
            "expected_inputs": [{"name": "city", "type": "text"}],
            "expected_outputs": [{"name": "forecast", "type": "text"}],
        }
    }

    discovery = WorkflowCapabilityDiscovery(db)
    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Build a weather forecast workflow by city",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    workflow_candidate = next(
        candidate
        for candidate in result["candidates"]
        if candidate.type == "workflow" and candidate.id == "weather_brief"
    )
    assert workflow_candidate.input_names == ["city"]
    assert workflow_candidate.output_names == ["forecast"]


def test_discovery_filters_workflow_knowledge_nodes_with_shared_visibility_policy(monkeypatch):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend(
        hits=[
            WorkflowKnowledgeHit(
                entity_id="tool:blocked_graph_tool",
                entity_kind="tool",
                score=97.0,
                title="Blocked Graph Tool",
            ),
            WorkflowKnowledgeHit(
                entity_id="tool:visible_graph_tool",
                entity_kind="tool",
                score=96.0,
                title="Visible Graph Tool",
            ),
        ],
        projection=WorkflowKnowledgeProjection(
            nodes=[
                WorkflowKnowledgeNode(
                    id="tool:blocked_graph_tool",
                    kind="tool",
                    label="Blocked Graph Tool",
                    metadata={
                        "tool_id": "blocked_graph_tool",
                        "availability_status": "unavailable",
                        "new_runs_blocked": True,
                    },
                ),
                WorkflowKnowledgeNode(
                    id="tool:visible_graph_tool",
                    kind="tool",
                    label="Visible Graph Tool",
                    metadata={
                        "tool_id": "visible_graph_tool",
                        "availability_status": "available",
                        "visible_in_catalog": True,
                    },
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    backend.scope_digests[("acct_test", "", "proj_test")] = discovery._catalog_sync_digest(
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Use graph tool candidates for external data lookup",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    candidates = {(candidate.type, candidate.id): candidate for candidate in result["candidates"]}
    assert ("tool", "blocked_graph_tool") not in candidates
    assert ("tool", "visible_graph_tool") in candidates


def test_discovery_workflow_knowledge_scope_includes_tenant_id(monkeypatch):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend()
    monkeypatch.setenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "false")
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    tenant_scope = {"account_id": "acct_test", "tenant_id": "tenant_a"}
    backend.scope_digests[("acct_test", "tenant_a", "proj_test")] = discovery._catalog_sync_digest(
        scope=tenant_scope, project_id="proj_test"
    )

    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Use graph tool candidates for external data lookup",
        candidate_limit=8,
        scope=tenant_scope,
        project_id="proj_test",
    )

    assert backend.search_calls
    assert backend.search_calls[0]["tenant_id"] == "tenant_a"
    assert result["workflow_knowledge"]["scope"]["tenant_id"] == "tenant_a"


def test_discovery_does_not_hide_workflows_by_name_prefix():
    db = _FakeWorkflowDB()
    db.workflow_rows.append(
        {
            "workflow_id": "audit_lower_text_20260531",
            "display_name": "Audit Lower Text",
            "description": "Lowercase text and return a normalized text output.",
            "step_count": 1,
            "builder_metadata_jsonb": {
                "purpose": "Lowercase text during an audit run.",
                "use_cases": ["lower text", "text normalization"],
                "composition_role": "child_workflow",
                "determinism_level": "deterministic",
                "safe_for_auto_use": True,
            },
            "enabled": True,
            "visible_in_catalog": True,
            "tags": ["lower", "text"],
        }
    )
    db.workflows["audit_lower_text_20260531"] = {
        "workflow_id": "audit_lower_text_20260531",
        "display_name": "Audit Lower Text",
        "description": "Lower text and return a normalized text output.",
        "enabled": True,
        "_phases": [{"phase_id": "run", "_steps": []}],
        "settings": {
            "callable": {
                "enabled": True,
                "expected_inputs": [{"name": "text"}],
                "expected_outputs": [{"name": "normalized_text"}],
            }
        },
    }
    discovery = WorkflowCapabilityDiscovery(db, workflow_knowledge_backend=None)

    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Create a workflow that lowers text and returns normalized_text.",
        candidate_limit=12,
        scope=normalize_scope(account_id="acct_demo", project_id="proj_demo"),
        project_id="proj_demo",
    )

    surfaced_workflow_ids = {
        str(candidate.id or "")
        for candidate in result["candidates"]
        if str(candidate.type or "") == "workflow"
    }
    assert "audit_lower_text_20260531" in surfaced_workflow_ids


def test_discovery_excludes_explicitly_hidden_workflows_from_lexical_candidates():
    db = _FakeWorkflowDB()
    db.workflow_rows.append(
        {
            "workflow_id": "audit_lower_text_20260531",
            "display_name": "Audit Lower Text",
            "description": "Lower text and return a normalized text output.",
            "step_count": 1,
            "builder_metadata_jsonb": {
                "purpose": "Lower text during an audit run.",
                "use_cases": ["lower text", "text normalization"],
                "composition_role": "child_workflow",
                "determinism_level": "deterministic",
                "safe_for_auto_use": True,
            },
            "enabled": True,
            "visible_in_catalog": False,
            "tags": ["lower", "text"],
        }
    )
    db.workflows["audit_lower_text_20260531"] = {
        "workflow_id": "audit_lower_text_20260531",
        "display_name": "Audit Lower Text",
        "description": "Lower text and return a normalized text output.",
        "enabled": True,
        "_phases": [{"phase_id": "run", "_steps": []}],
        "settings": {
            "callable": {
                "enabled": True,
                "expected_inputs": [{"name": "text"}],
                "expected_outputs": [{"name": "normalized_text"}],
            }
        },
    }
    discovery = WorkflowCapabilityDiscovery(db, workflow_knowledge_backend=None)

    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Create a workflow that lowers text and returns normalized_text.",
        candidate_limit=12,
        scope=normalize_scope(account_id="acct_demo", project_id="proj_demo"),
        project_id="proj_demo",
    )

    surfaced_workflow_ids = {
        str(candidate.id or "")
        for candidate in result["candidates"]
        if str(candidate.type or "") == "workflow"
    }
    assert "audit_lower_text_20260531" not in surfaced_workflow_ids


@pytest.mark.parametrize(
    "obsolete_key",
    [
        "hidden_from_ai_builder",
        "ai_builder_hidden",
        "exclude_from_ai_builder",
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_current_visibility_policy_ignores_obsolete_hidden_aliases(obsolete_key, nested):
    from core.application.workflows.capability_visibility import capability_visible_by_policy

    row = {"enabled": True, "visible_in_catalog": True}
    if nested:
        row["builder_metadata_jsonb"] = {obsolete_key: True}
    else:
        row[obsolete_key] = True

    assert capability_visible_by_policy("visible", capability=row) is True


def test_scoped_catalog_keeps_audit_named_non_workflow_capabilities_visible():
    db = _FakeWorkflowDB()
    db.tool_rows.append(
        {
            "tool_id": "audit_log_exporter",
            "display_name": "Audit Log Exporter",
            "description": "Export production audit log records for compliance review.",
            "enabled": True,
            "visible_in_catalog": True,
            "category": "connector",
            "impl_type": "http",
        }
    )
    catalog = ScopedCapabilityCatalog(db)

    tool_ids = {
        str(row.get("tool_id") or "")
        for row in catalog.tool_summaries(
            scope=normalize_scope(account_id="acct_demo", project_id="proj_demo"),
            project_id="proj_demo",
        )
    }

    assert "audit_log_exporter" in tool_ids


def test_discovery_lexical_agent_and_skill_candidates_include_contract_names(monkeypatch):
    db = _FakeWorkflowDB()
    db.agent_rows.append(
        {
            "agent_id": "fixture_launch_copywriter",
            "display_name": "Fixture Launch Copywriter",
            "description": "Catalog agent that produces launch copy.",
            "enabled": True,
            "visible_in_catalog": True,
            "category": "ai",
            "skill_names": "fixture_write_launch_copy",
            "builder_metadata_jsonb": {
                "purpose": "Use the launch-copy skill to draft a short marketing message.",
                "use_cases": ["copy drafting", "launch messaging"],
                "anti_use_cases": ["risk analysis"],
                "composition_role": "content_generation",
                "determinism_level": "agentic",
                "safe_for_auto_use": False,
            },
        }
    )
    db.skill_rows.append(
        {
            "skill_id": "fixture_write_launch_copy",
            "display_name": "Fixture Write Launch Copy",
            "description": "Draft a short launch email from a request.",
            "enabled": True,
            "visible_in_catalog": True,
            "category": "content",
            "builder_metadata_jsonb": {
                "purpose": "Draft short launch messaging from a request.",
                "use_cases": ["copy drafting", "launch email"],
                "anti_use_cases": ["risk analysis"],
                "composition_role": "content_generation",
                "determinism_level": "hybrid",
                "safe_for_auto_use": False,
            },
        }
    )

    original_get_agent = db.get_agent
    original_get_agent_skills = db.get_agent_skills
    original_get_skill = db.get_skill

    def _get_agent(agent_id: str):
        if agent_id == "fixture_launch_copywriter":
            return {
                "agent_id": "fixture_launch_copywriter",
                "display_name": "Fixture Launch Copywriter",
                "_inputs": [],
                "_outputs": [],
            }
        return original_get_agent(agent_id)

    def _get_agent_skills(agent_id: str):
        if agent_id == "fixture_launch_copywriter":
            return [{"skill_id": "fixture_write_launch_copy", "sort_order": 0}]
        return original_get_agent_skills(agent_id)

    def _get_skill(skill_id: str):
        if skill_id == "fixture_write_launch_copy":
            return {
                "skill_id": "fixture_write_launch_copy",
                "display_name": "Fixture Write Launch Copy",
                "_inputs": [{"name": "request", "type": "paragraph", "required": True}],
                "_outputs": [{"name": "launch_copy", "type": "text", "required": True}],
            }
        return original_get_skill(skill_id)

    monkeypatch.setattr(db, "get_agent", _get_agent)
    monkeypatch.setattr(db, "get_agent_skills", _get_agent_skills)
    monkeypatch.setattr(db, "get_skill", _get_skill)

    discovery = WorkflowCapabilityDiscovery(db)
    request_tokens = capability_discovery_module._tokens(
        "Draft launch messaging and launch email copy for human approval."
    )
    agent_candidate = next(
        candidate
        for candidate in discovery._agent_candidates(request_tokens)
        if candidate.id == "fixture_launch_copywriter"
    )
    skill_candidate = next(
        candidate
        for candidate in discovery._skill_candidates(request_tokens)
        if candidate.id == "fixture_write_launch_copy"
    )

    assert agent_candidate.input_names == ["request"]
    assert agent_candidate.output_names == ["launch_copy"]
    assert agent_candidate.skill_names == ["fixture_write_launch_copy"]
    assert skill_candidate.input_names == ["request"]
    assert skill_candidate.output_names == ["launch_copy"]


def test_discovery_surfaces_multi_operation_tools_as_operation_candidates():
    db = _FakeWorkflowDB()
    db.tool_rows.append(
        {
            "tool_id": "crm_sync_tool",
            "display_name": "CRM Sync Tool",
            "description": "Sync or delete CRM records.",
            "enabled": True,
            "visible_in_catalog": True,
            "category": "connector",
            "impl_type": "http",
        }
    )
    original_get_tool = db.get_tool

    def _get_tool(tool_id: str):
        if tool_id == "crm_sync_tool":
            return {
                "tool_id": "crm_sync_tool",
                "display_name": "CRM Sync Tool",
                "description": "Sync or delete CRM records.",
                "interface": {
                    "functions": [
                        {
                            "name": "sync_record",
                            "display_name": "Sync Record",
                            "description": "Create or update a CRM record.",
                            "parameters": [
                                {"name": "record_id", "type": "text", "required": True},
                                {"name": "payload", "type": "json", "required": False},
                            ],
                            "returns": {
                                "type": "object",
                                "properties": {
                                    "status": {"type": "text"},
                                    "sync_id": {"type": "text"},
                                },
                            },
                        },
                        {
                            "name": "delete_record",
                            "display_name": "Delete Record",
                            "description": "Delete a CRM record by identifier.",
                            "parameters": [
                                {"name": "record_id", "type": "text", "required": True},
                            ],
                            "returns": {
                                "type": "object",
                                "properties": {
                                    "deleted": {"type": "text"},
                                },
                            },
                        },
                    ]
                },
            }
        return original_get_tool(tool_id)

    db.get_tool = _get_tool  # type: ignore[method-assign]
    discovery = WorkflowCapabilityDiscovery(db)

    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Use CRM Sync Tool to sync records or delete stale records",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    crm_candidates = [
        candidate
        for candidate in result["candidates"]
        if candidate.type == "tool" and candidate.id == "crm_sync_tool"
    ]
    assert {candidate.function_name for candidate in crm_candidates} == {
        "sync_record",
        "delete_record",
    }
    assert {candidate.display_name for candidate in crm_candidates} == {
        "CRM Sync Tool / Sync Record",
        "CRM Sync Tool / Delete Record",
    }
    sync_candidate = next(
        candidate for candidate in crm_candidates if candidate.function_name == "sync_record"
    )
    delete_candidate = next(
        candidate for candidate in crm_candidates if candidate.function_name == "delete_record"
    )
    assert sync_candidate.input_names == ["record_id", "payload"]
    assert sync_candidate.output_names == ["status", "sync_id"]
    assert delete_candidate.input_names == ["record_id"]
    assert delete_candidate.output_names == ["deleted"]


def test_discovery_falls_back_to_lexical_candidates_when_backend_fails(monkeypatch):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend(fail_upsert=True)
    monkeypatch.delenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Need an agent to summarize support tickets",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    candidates = {(candidate.type, candidate.id): candidate for candidate in result["candidates"]}
    assert ("agent", "research_agent") in candidates


def test_discovery_can_fallback_to_lexical_candidates_when_enabled(monkeypatch):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend(fail_upsert=True)
    monkeypatch.setenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Need an agent to summarize support tickets",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    candidates = {(candidate.type, candidate.id): candidate for candidate in result["candidates"]}
    assert ("agent", "research_agent") in candidates


def test_discovery_can_explicitly_skip_workflow_knowledge_search(monkeypatch):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend(fail_upsert=True)
    monkeypatch.delenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    discovery.discover(
        workflow_id="wf_current",
        request_text="Find a reusable workflow in the local catalog",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
        enable_workflow_knowledge_search=False,
    )

    assert backend.upsert_calls == []
    assert backend.search_calls == []


def test_catalog_pruning_does_not_keep_workflow_candidates_from_invoke_wording_alone():
    candidates = [
        capability_discovery_module.CapabilityCandidate(
            id="generic_child_flow",
            type="workflow",
            display_name="Generic Child Flow",
            confidence=0.41,
        ),
        capability_discovery_module.CapabilityCandidate(
            id="support_orchestrator",
            type="agent",
            display_name="Support Orchestrator",
            confidence=0.83,
        ),
    ]

    pruned = capability_discovery_module._prune_catalog_workflow_candidates(
        request_tokens=capability_discovery_module._tokens(
            "Call an existing workflow to help with this task."
        ),
        candidates=candidates,
    )

    candidate_pairs = {(candidate.type, candidate.id) for candidate in pruned}
    assert ("agent", "support_orchestrator") in candidate_pairs
    assert ("workflow", "generic_child_flow") not in candidate_pairs


def test_discovery_does_not_resync_workflow_knowledge_when_catalog_changes(monkeypatch):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend()
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    discovery.discover(
        workflow_id="wf_current",
        request_text="Create a workflow that gets weather forecast by city and returns warnings",
        candidate_limit=4,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    assert backend.search_calls == []
    assert backend.upsert_calls == []

    db.tool_rows.append(
        {
            "tool_id": "weather_alerts",
            "display_name": "Weather Alerts",
            "description": "Fetch severe weather warnings by city.",
            "enabled": True,
            "visible_in_catalog": True,
            "category": "connector",
            "impl_type": "http",
        }
    )
    discovery.discover(
        workflow_id="wf_current",
        request_text="Create a workflow that gets weather forecast by city and returns warnings",
        candidate_limit=4,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    assert len(backend.upsert_calls) == 0


def test_discovery_drops_low_confidence_noise_candidates(monkeypatch):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend(
        hits=[
            WorkflowKnowledgeHit(
                entity_id="workflow:weather_brief",
                entity_kind="workflow",
                score=12.0,
                title="Weather Brief",
            )
        ],
        projection=WorkflowKnowledgeProjection(
            nodes=[
                WorkflowKnowledgeNode(
                    id="workflow:weather_brief",
                    kind="workflow",
                    label="Weather Brief",
                    metadata={"workflow_id": "weather_brief", "enabled": True},
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Need something for payroll vacation accrual carryover",
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    assert result["candidates"] == []


def test_discovery_lexical_candidates_match_builder_metadata_terms():
    db = _FakeWorkflowDB()
    db.agent_rows = [
        {
            "agent_id": "review_agent",
            "display_name": "Review Agent",
            "description": "General assistance.",
            "enabled": True,
            "visible_in_catalog": True,
            "category": "ai",
            "skill_names": "",
            "builder_metadata_jsonb": {
                "purpose": "Review risky billing escalations before a human approval handoff.",
                "use_cases": ["billing escalation review", "human approval precheck"],
                "anti_use_cases": ["deterministic math"],
                "composition_role": "reasoning_step",
                "determinism_level": "agentic",
                "safe_for_auto_use": False,
            },
        }
    ]
    discovery = WorkflowCapabilityDiscovery(db)

    candidates = discovery._lexical_candidates(
        workflow_id="wf_current",
        request_tokens=capability_discovery_module._tokens(
            "Need a billing escalation review before human approval"
        ),
    )

    assert candidates
    candidate = next(item for item in candidates if item.type == "agent")
    assert candidate.id == "review_agent"
    assert "billing escalation review" in candidate.capability_summary.lower()


def test_discovery_prunes_weak_blank_canvas_workflow_patterns_when_agent_exists(monkeypatch):
    monkeypatch.setenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "1")
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend(
        hits=[
            WorkflowKnowledgeHit(
                entity_id="workflow:alert_triage",
                entity_kind="workflow",
                score=35.0,
                title="Incident Response",
            ),
            WorkflowKnowledgeHit(
                entity_id="step:coordinator:step_1",
                entity_kind="step",
                score=28.0,
                title="Step 1",
            ),
        ],
        projection=WorkflowKnowledgeProjection(
            nodes=[
                WorkflowKnowledgeNode(
                    id="workflow:alert_triage",
                    kind="workflow",
                    label="Incident Response",
                    metadata={"workflow_id": "alert_triage", "enabled": True},
                ),
                WorkflowKnowledgeNode(
                    id="agent:coordinator",
                    kind="agent",
                    label="Coordinator",
                    metadata={"agent_id": "coordinator", "enabled": True},
                ),
                WorkflowKnowledgeNode(
                    id="step:coordinator:step_1",
                    kind="step",
                    label="Step 1",
                    metadata={
                        "workflow_id": "alert_triage",
                        "agent_id": "coordinator",
                        "step_id": "step_1",
                    },
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    result = discovery.discover(
        workflow_id="wf_current",
        request_text=(
            "Build a workflow that triages inbound support emails, classifies urgency, "
            "drafts a suggested reply, and routes billing cases to human approval."
        ),
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    candidate_pairs = {(candidate.type, candidate.id) for candidate in result["candidates"]}
    assert ("agent", "research_agent") in candidate_pairs
    assert ("workflow", "alert_triage") not in candidate_pairs


def test_discovery_prunes_weak_blank_canvas_workflow_patterns_even_without_agent_fallback(
    monkeypatch,
):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend(
        hits=[
            WorkflowKnowledgeHit(
                entity_id="workflow:sample_incoming_webhook_router",
                entity_kind="workflow",
                score=33.0,
                title="Demo: Incoming Webhook Router",
            )
        ],
        projection=WorkflowKnowledgeProjection(
            nodes=[
                WorkflowKnowledgeNode(
                    id="workflow:sample_incoming_webhook_router",
                    kind="workflow",
                    label="Demo: Incoming Webhook Router",
                    metadata={
                        "workflow_id": "sample_incoming_webhook_router",
                        "enabled": True,
                    },
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    result = discovery.discover(
        workflow_id="wf_current",
        request_text=(
            "Build a workflow that triages inbound support emails, classifies urgency, "
            "drafts a suggested reply, and routes billing cases to human approval."
        ),
        candidate_limit=8,
        scope={"account_id": "acct_test"},
        project_id="proj_test",
    )

    candidate_pairs = {(candidate.type, candidate.id) for candidate in result["candidates"]}
    assert ("workflow", "sample_incoming_webhook_router") not in candidate_pairs


def test_catalog_sync_digest_ignores_blank_workflow_rows():
    db = _FakeWorkflowDB()
    discovery = WorkflowCapabilityDiscovery(db)
    digest_without_blank = discovery._catalog_sync_digest(
        scope=normalize_scope(account_id="acct_test", project_id="proj_test"),
        project_id="proj_test",
    )

    db.workflow_rows.append(
        {
            "workflow_id": "fresh_blank_canvas",
            "display_name": "Fresh Blank Canvas",
            "description": "Unsaved starter canvas.",
            "step_count": 0,
            "enabled": True,
            "visible_in_catalog": True,
            "builder_metadata_jsonb": {},
            "tags": [],
        }
    )
    digest_with_blank = discovery._catalog_sync_digest(
        scope=normalize_scope(account_id="acct_test", project_id="proj_test"),
        project_id="proj_test",
    )

    assert digest_with_blank == digest_without_blank


def test_catalog_sync_digest_accepts_uuid_typed_scope_metadata():
    db = _FakeWorkflowDB()
    db.workflow_rows[1]["account_id"] = uuid.uuid4()
    db.workflow_rows[1]["project_id"] = uuid.uuid4()
    db.workflow_rows[1]["builder_metadata_jsonb"] = {
        "owner_account_id": uuid.uuid4(),
        "allowed_projects": [uuid.uuid4()],
    }
    discovery = WorkflowCapabilityDiscovery(db)

    digest = discovery._catalog_sync_digest(
        scope=normalize_scope(account_id="acct_test", project_id="proj_test"),
        project_id="proj_test",
    )

    assert isinstance(digest, str)
    assert digest


def test_discovery_accepts_scope_context_without_request_path_workflow_knowledge_sync(monkeypatch):
    monkeypatch.setenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "1")
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend(
        hits=[
            WorkflowKnowledgeHit(
                entity_id="tool:weather_api",
                entity_kind="tool",
                score=48.0,
                title="Weather API",
            )
        ],
        projection=WorkflowKnowledgeProjection(
            nodes=[
                WorkflowKnowledgeNode(
                    id="tool:weather_api",
                    kind="tool",
                    label="Weather API",
                    metadata={"tool_id": "weather_api", "enabled": True},
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "core.application.workflows.knowledge_projection.build_workflow_canvas_graph",
        _fake_canvas_graph,
    )

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Build a weather forecast workflow by city",
        candidate_limit=8,
        scope=normalize_scope(account_id="acct_test", project_id="proj_test"),
        project_id="proj_test",
    )

    assert backend.upsert_calls == []
    assert backend.search_calls == []
    candidates = {(candidate.type, candidate.id): candidate for candidate in result["candidates"]}
    assert ("tool", "weather_api") in candidates


def test_discovery_keeps_request_path_workflow_knowledge_sync_disabled_across_instances():
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend()

    discovery_a = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    discovery_b = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )

    discovery_a.discover(
        workflow_id="wf_current",
        request_text="Build a weather forecast workflow by city",
        candidate_limit=8,
        scope=normalize_scope(account_id="acct_test", project_id="proj_test"),
        project_id="proj_test",
    )
    discovery_b.discover(
        workflow_id="wf_current",
        request_text="Build a weather forecast workflow by city",
        candidate_limit=8,
        scope=normalize_scope(account_id="acct_test", project_id="proj_test"),
        project_id="proj_test",
    )

    assert backend.upsert_calls == []
    assert backend.search_calls == []


def test_discovery_never_builds_scoped_workflow_projection_inside_live_request_path(
    monkeypatch,
):
    db = _FakeWorkflowDB()
    backend = _FakeWorkflowKnowledgeBackend()
    calls = {"count": 0}

    def _unexpected_sync(*_args, **_kwargs):
        calls["count"] += 1
        raise AssertionError("live discovery should not build workflow knowledge projections")

    monkeypatch.setattr(backend, "upsert_projection", _unexpected_sync)

    discovery = WorkflowCapabilityDiscovery(
        db,
        workflow_knowledge_backend=backend,
    )
    discovery.discover(
        workflow_id="wf_current",
        request_text="Build a weather forecast workflow by city",
        candidate_limit=8,
        scope=normalize_scope(account_id="acct_test", project_id="proj_test"),
        project_id="proj_test",
    )

    assert calls["count"] == 0


def test_discovery_hides_foreign_private_tools_from_account_scope():
    db = _FakeWorkflowDB()
    db.tool_rows.extend(
        [
            {
                "tool_id": "a_private_tool",
                "display_name": "Account A Tool",
                "description": "sync account a records",
                "enabled": True,
                "visible_in_catalog": True,
                "category": "connector",
                "impl_type": "http",
                "account_id": "acct_a",
                "scope_kind": "account",
                "builder_metadata_jsonb": {},
            },
            {
                "tool_id": "b_private_tool",
                "display_name": "Account B Tool",
                "description": "sync account b records",
                "enabled": True,
                "visible_in_catalog": True,
                "category": "connector",
                "impl_type": "http",
                "account_id": "acct_b",
                "scope_kind": "account",
                "builder_metadata_jsonb": {},
            },
        ]
    )
    discovery = WorkflowCapabilityDiscovery(db)

    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Use account b tool to sync records",
        candidate_limit=12,
        scope=normalize_scope(account_id="acct_a", project_id="proj_a"),
        project_id="proj_a",
    )

    surfaced_tool_ids = {
        str(candidate.id or "")
        for candidate in result["candidates"]
        if str(candidate.type or "") == "tool"
    }
    assert "b_private_tool" not in surfaced_tool_ids


def test_discovery_hydrates_only_bounded_scoped_tool_candidates_at_scale():
    db = _CountingScopedToolDB()
    discovery = WorkflowCapabilityDiscovery(db)

    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Create a workflow to sync customer records",
        candidate_limit=12,
        scope=normalize_scope(account_id="acct_test", project_id="proj_test"),
        project_id="proj_test",
    )

    assert db.get_tool_calls <= 12
    tool_candidates = [candidate for candidate in result["candidates"] if candidate.type == "tool"]
    assert len(tool_candidates) <= 12
    assert any(candidate.id == "customer_sync" for candidate in tool_candidates)
    assert all(candidate.id != "tool_1" for candidate in tool_candidates)


def test_discovery_sorts_workflow_lexical_candidates_before_hydration():
    db = _CountingScopedWorkflowDB()
    discovery = WorkflowCapabilityDiscovery(db)

    result = discovery.discover(
        workflow_id="wf_current",
        request_text="Create a workflow for support triage with suggested reply",
        candidate_limit=1,
        scope=normalize_scope(account_id="acct_test", project_id="proj_test"),
        project_id="proj_test",
    )

    workflow_candidates = [
        candidate for candidate in result["candidates"] if candidate.type == "workflow"
    ]
    assert workflow_candidates
    assert workflow_candidates[0].id == "support_triage_flow"
    assert db.get_workflow_calls <= 8


def test_scoped_capability_catalog_passes_scope_filters_to_summary_getters():
    db = _FakeWorkflowDB()
    catalog = ScopedCapabilityCatalog(db)

    rows = catalog.list_visible_capability_summaries(
        scope=normalize_scope(account_id="acct_demo", project_id="proj_demo"),
        project_id="proj_demo",
        request_text="support triage",
        limit=20,
    )

    assert rows
    assert db.summary_call_kwargs
    assert {entry["getter"] for entry in db.summary_call_kwargs} == {
        "workflow",
        "agent",
        "skill",
        "tool",
    }
    assert all(entry["enabled_only"] is True for entry in db.summary_call_kwargs)
    assert all(entry["account_id"] == "acct_demo" for entry in db.summary_call_kwargs)
    assert all(entry["project_id"] == "proj_demo" for entry in db.summary_call_kwargs)


def test_scoped_capability_catalog_keeps_blank_scoped_system_tool_rows_visible():
    db = _FakeWorkflowDB()
    db.tool_rows = [
        {
            "tool_id": "std.text",
            "display_name": "Text Tool",
            "description": "Builtin text operations.",
            "enabled": True,
            "visible_in_catalog": True,
            "category": "connector",
            "impl_type": "builtin",
            "account_id": "",
            "project_id": "",
            "scope_kind": "",
            "builder_metadata_jsonb": {},
        }
    ]
    catalog = ScopedCapabilityCatalog(db)

    rows = catalog.list_visible_capability_summaries(
        scope=normalize_scope(account_id="acct_demo", project_id="proj_demo"),
        project_id="proj_demo",
        request_text="text operations",
        limit=20,
    )

    surfaced_tool_ids = {str(row.get("tool_id") or "") for row in rows if row.get("type") == "tool"}
    assert "std.text" in surfaced_tool_ids


def test_scoped_capability_catalog_ranks_request_aligned_tool_rows_first():
    db = _FakeWorkflowDB()
    db.tool_rows = [
        {
            "tool_id": "weather_api",
            "display_name": "Weather API",
            "description": "Fetch forecast data by city.",
            "enabled": True,
            "visible_in_catalog": True,
            "category": "connector",
            "impl_type": "http",
            "builder_metadata_jsonb": {},
        },
        {
            "tool_id": "std.text",
            "display_name": "Text Tool",
            "description": "Convert text to upper case, lower case, or title case.",
            "enabled": True,
            "visible_in_catalog": True,
            "category": "connector",
            "impl_type": "builtin",
            "builder_metadata_jsonb": {},
        },
    ]
    catalog = ScopedCapabilityCatalog(db)

    rows = catalog.list_visible_capability_summaries(
        scope=normalize_scope(account_id="acct_demo", project_id="proj_demo"),
        project_id="proj_demo",
        request_text="upper text",
        limit=1,
    )

    assert rows
    assert rows[0].get("type") == "tool"
    assert rows[0].get("tool_id") == "std.text"


def test_discovery_tokens_do_not_expand_text_case_aliases():
    from core.application.workflows.discovery.capability_discovery import _score_match, _tokens

    request_tokens = _tokens("uppercase")

    assert "uppercase" in request_tokens
    assert "upper" not in request_tokens
    assert _score_match(request_tokens, "std.text", "upper", "Convert text to upper case") == 0


def test_dataset_candidates_surface_with_columns_and_dataset_query_step():
    """Stage 3.3: datasets surface as candidates (type=dataset) with columns as outputs."""

    class _DatasetDB:
        def list_datasets(self, enabled_only=False, *, project_id=None, include_trashed=False):
            return [
                {
                    "dataset_id": "sample_sales_open_orders",
                    "display_name": "Demo Sales Open Orders",
                    "description": "Open sales orders",
                    "query_language": "sql",
                    "schema_snapshot_jsonb": {
                        "resources": [
                            {
                                "name": "orders",
                                "columns": [
                                    {"name": "order_id"},
                                    {"name": "status"},
                                    {"name": "total_amount"},
                                ],
                            }
                        ]
                    },
                }
            ]

    disc = WorkflowCapabilityDiscovery(_DatasetDB())
    cands = disc._dataset_candidates({"sales", "orders", "open", "status"})
    assert len(cands) == 1
    candidate = cands[0]
    assert candidate.id == "sample_sales_open_orders"
    assert candidate.type == "dataset"
    assert candidate.step_kinds == ["dataset_query"]
    assert candidate.output_names == ["order_id", "status", "total_amount"]
    assert candidate.confidence > 0


def test_dataset_column_names_handles_missing_or_malformed_snapshot():
    fn = capability_discovery_module._dataset_column_names
    assert fn(None) == []
    assert fn({}) == []
    assert fn({"resources": [{"columns": [{"name": "a"}, {"name": "a"}, {"name": ""}]}]}) == ["a"]


def test_scoped_catalog_workflow_token_restriction_applies_before_summary_limit():
    catalog = ScopedCapabilityCatalog(_FakeWorkflowDB())
    scope = normalize_scope(account_id="acct_demo", allowed_workflow_ids=frozenset({"permitted"}))
    common = {"enabled": True, "account_id": "acct_demo", "scope_kind": "account"}
    assert catalog._row_visible(
        {**common, "workflow_id": "permitted"},
        capability_type="workflow",
        scope=scope,
        project_id=None,
    )
    assert not catalog._row_visible(
        {**common, "workflow_id": "denied"},
        capability_type="workflow",
        scope=scope,
        project_id=None,
    )
    assert catalog._row_visible(
        {**common, "workflow_id": "denied"},
        capability_type="workflow",
        scope=normalize_scope(account_id="acct_demo"),
        project_id=None,
    )


def test_projected_agent_uses_qualified_skill_contract_with_empty_physical_registry():
    from tests.unit.test_workflow_authoring_catalog import (
        PROJECTED_AGENT,
        PROJECTED_SKILL,
        CatalogDB,
        install_projection_store,
    )

    db = CatalogDB()
    install_projection_store(db, account_id="account-projected")
    discovery = WorkflowCapabilityDiscovery(db)
    scope = {"account_id": " account-projected "}
    agents = discovery._agent_candidates({"launch"}, scope=scope, hydrate_limit=1)
    skills = discovery._skill_candidates({"launch"}, scope=scope, hydrate_limit=1)
    assert [
        (item.id, item.skill_names, item.input_names, item.output_names) for item in agents
    ] == [(PROJECTED_AGENT, [PROJECTED_SKILL], ["request"], ["launch_copy"])]
    assert [(item.id, item.input_names, item.output_names) for item in skills] == [
        (PROJECTED_SKILL, ["request"], ["launch_copy"])
    ]


def test_agent_denied_during_hydration_does_not_read_private_skill_contract(monkeypatch):
    from tests.unit.test_workflow_authoring_catalog import CatalogDB, install_projection_store

    db = CatalogDB()
    store = install_projection_store(db)
    discovery = WorkflowCapabilityDiscovery(db)
    original = discovery._scoped_catalog.hydrate_visible_capabilities

    def deny(**kwargs):
        store.active = False
        return original(**kwargs)

    def forbidden(*args):
        pytest.fail("Denied agent must not hydrate its skill contract")

    monkeypatch.setattr(discovery._scoped_catalog, "hydrate_visible_capabilities", deny)
    db.get_agent_skills = forbidden
    db.get_skill = forbidden
    assert discovery._agent_candidates({"launch"}, scope={"account_id": store.account_id}) == []


def test_native_summary_candidate_survives_without_private_contract_hydration():
    from tests.unit.test_workflow_authoring_catalog import SCOPE, CatalogDB

    db = CatalogDB()
    db.agents["native"] = {"agent_id": "native", "display_name": "Launch native", "enabled": True}
    db.get_agent = lambda _: None

    def forbidden(*args):
        pytest.fail("Failed agent hydration must not read private assignments or skill contracts")

    db.get_agent_skills = forbidden
    db.get_skill = forbidden
    candidates = WorkflowCapabilityDiscovery(db)._agent_candidates({"launch"}, scope=SCOPE)
    assert [(row.id, row.input_names, row.output_names, row.skill_names) for row in candidates] == [
        ("native", [], [], [])
    ]
