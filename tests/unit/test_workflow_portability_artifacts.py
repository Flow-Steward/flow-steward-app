"""A workflow export carries the agents, skills, contracts and guardrails it uses."""

from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from core.application.workflows.workflow_portability import (
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from core.application.workflows.workflow_portability_artifacts import parse_definitions
from tests._workflow_definitions import linear_workflow

ACCOUNT = "00000000-0000-0000-0000-0000000000a1"
OTHER_ACCOUNT = "00000000-0000-0000-0000-0000000000b2"
GLOBAL_RULE = "11111111-1111-4111-8111-111111111111"
PROJECT_RULE = "22222222-2222-4222-8222-222222222222"
OTHER_PROJECT_RULE = "33333333-3333-4333-8333-333333333333"
UNRELATED_RULE = "44444444-4444-4444-8444-444444444444"
SECRET = "sk-" + "x" * 30  # pragma: allowlist secret


class _Store:
    def __init__(self) -> None:
        self.workflows: dict[str, dict] = {}
        self.agents = {
            "writer": {
                "agent_id": "writer",
                "account_id": ACCOUNT,
                "version": "2.0.0",
                "display_name": "Writer",
                "description": "Writes the summary.",
                "enabled": True,
                "category": "marketing",
                "prompts": {
                    "system": "You write; the token is a word here.",
                    "context_template": "{{ inputs.topic }}",
                    "skill_prompts": {"draft": "Draft first.", "polish": "Then polish."},
                },
                "config": {
                    "provider": "openai",
                    "model": "gpt-5",
                    "model_provider_id": "prov-1",
                    "max_tokens": 4000,
                    "timeout_minutes": 10,
                    "api_key": SECRET,
                },
                "quality": {"retry_on_failure": True},
                "tags": ["content"],
                "depends_on": {"soft": [], "hard": []},
                "inputs": {"required": [{"name": "topic"}], "optional": []},
                "outputs": ["summary"],
                "_skills": [
                    {"skill_id": "draft", "skill_prompt": "Draft first.", "sort_order": 0},
                    {"skill_id": "polish", "skill_prompt": "Then polish.", "sort_order": 1},
                ],
                "_tools": [{"tool_id": "search_docs", "sort_order": 0}],
                "created_at": "2026-10-01T00:00:00Z",
                "scope_kind": "account",
            },
            "projected": {
                "agent_id": "projected",
                "account_id": ACCOUNT,
                "display_name": "Projected",
                "source_install_id": "inst-1",
            },
            "foreign": {"agent_id": "foreign", "account_id": OTHER_ACCOUNT, "display_name": "F"},
        }
        self.own_io = {
            "writer": (
                [{"name": "topic", "type": "text", "description": "Topic", "required": True}],
                [{"name": "summary", "type": "text", "description": "", "required": True}],
            )
        }
        self.skills = {
            "draft": {
                "skill_id": "draft",
                "account_id": ACCOUNT,
                "display_name": "Draft",
                "enabled": True,
                "category": "marketing",
                "skill_type": "base",
                "instructions": ["Write a draft."],
                "prompt_template": "Draft {{ topic }}",
                "_inputs": [
                    {"name": "topic", "type": "text", "required": True, "contract_id": None},
                    {"name": "brief", "type": "json", "required": False, "contract_id": "brief.v1"},
                ],
                "_outputs": [
                    {
                        "name": "draft",
                        "type": "json",
                        "required": True,
                        "contract_id": "auto.draft.v1",
                    }
                ],
            },
            "polish": {"skill_id": "polish", "account_id": ACCOUNT, "display_name": "Polish"},
            "step_only": {"skill_id": "step_only", "account_id": ACCOUNT, "display_name": "Step"},
        }
        self.contracts = {
            "brief.v1": {
                "contract_id": "brief.v1",
                "display_name": "Brief",
                "kind": "json",
                "projection_mode": "manual",
                "schema_json": {
                    "type": "object",
                    "properties": {"audience": {"contract_id": "audience.v1"}},
                },
            },
            "audience.v1": {
                "contract_id": "audience.v1",
                "projection_mode": "manual",
                "schema_json": {"type": "object"},
            },
            "auto.draft.v1": {
                "contract_id": "auto.draft.v1",
                "projection_mode": "derived",
                "schema_json": {"type": "object"},
            },
        }
        self.guardrails = [
            {
                "id": GLOBAL_RULE,
                "display_name": "No PII",
                "enabled": True,
                "mode": "deterministic",
                "rule_type": "forbidden_patterns",
                "severity": "error",
                "config": {"patterns": ["ssn"]},
                "sort_order": 2,
                "project_id": None,
                "account_id": ACCOUNT,
                "_agents": ["writer", "unrelated_agent"],
            },
            {
                "id": PROJECT_RULE,
                "display_name": "Tone",
                "mode": "semantic",
                "rule_type": "llm_judge",
                "prompt_text": "Be polite.",
                "project_id": "proj-shop",
                "account_id": ACCOUNT,
                "_agents": ["writer"],
            },
            {
                "id": OTHER_PROJECT_RULE,
                "display_name": "Length",
                "mode": "deterministic",
                "rule_type": "max_length",
                "project_id": "proj-blog",
                "account_id": ACCOUNT,
                "_agents": ["writer"],
            },
            {
                "id": UNRELATED_RULE,
                "display_name": "Other",
                "mode": "deterministic",
                "rule_type": "forbidden_patterns",
                "project_id": None,
                "account_id": ACCOUNT,
                "_agents": ["unrelated_agent"],
            },
        ]
        self.projects = {"proj-shop": {"name": "Shop"}, "proj-blog": {"name": "Blog"}}

    def get_workflow(self, workflow_id, *, include_trashed=False):
        row = self.workflows.get(workflow_id)
        return deepcopy(row) if row else None

    def get_agent(self, agent_id):
        return deepcopy(self.agents.get(agent_id))

    def get_agent_own_inputs(self, agent_id):
        return deepcopy(self.own_io.get(agent_id, ([], []))[0])

    def get_agent_own_outputs(self, agent_id):
        return deepcopy(self.own_io.get(agent_id, ([], []))[1])

    def get_agent_tools(self, agent_id):
        return deepcopy((self.agents.get(agent_id) or {}).get("_tools") or [])

    def get_skill(self, skill_id):
        return deepcopy(self.skills.get(skill_id))

    def get_tool(self, tool_id):
        return {"tool_id": tool_id}

    def get_data_contract(self, contract_id, include_trashed=False):
        return deepcopy(self.contracts.get(contract_id))

    def list_guardrails(
        self, project_id=None, enabled_only=False, include_trashed=False, *, account_id=None
    ):
        return [deepcopy(row) for row in self.guardrails if row["account_id"] == account_id]

    def get_project(self, project_id):
        return deepcopy(self.projects.get(project_id))


def _workflow(
    workflow_id: str, *, agent_id: str = "writer", skill_id: str = "draft", child: str = ""
) -> dict:
    steps = [
        {
            "step_id": "write",
            "step_kind": "agent",
            "participant_key": "author",
            "agent_id": agent_id,
            "skill_id": skill_id,
        }
    ]
    if child:
        steps.append(
            {
                "step_id": "call",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"workflow_id": child}},
            }
        )
    return linear_workflow(
        *steps,
        workflow_id=workflow_id,
        account_id=ACCOUNT,
        projection_mode="manual",
        settings={"participants": [{"participant_key": "author", "agent_id": agent_id}]},
    )


def _export(store: _Store, workflow_id: str = "root", **kwargs) -> tuple[dict, str]:
    text = WorkflowPortabilityService(store, account_id=ACCOUNT).export_yaml(workflow_id, **kwargs)
    return yaml.safe_load(text), text


@pytest.fixture
def store() -> _Store:
    store = _Store()
    store.workflows["root"] = _workflow("root", child="child")
    store.workflows["child"] = _workflow("child", skill_id="step_only")
    return store


def test_export_carries_agent_definition_without_model_route_or_secrets(store: _Store) -> None:
    bundle, text = _export(store)
    agents = bundle["definitions"]["agents"]

    assert [agent["agent_id"] for agent in agents] == ["writer"]
    writer = agents[0]
    assert writer["category"] == "marketing"
    assert writer["config"] == {"max_tokens": 4000, "timeout_minutes": 10, "api_key": "[redacted]"}
    assert "skill_prompts" not in writer["prompts"]
    assert writer["prompts"]["system"] == "You write; the token is a word here."
    assert writer["skills"] == [
        {"skill_id": "draft", "skill_prompt": "Draft first."},
        {"skill_id": "polish", "skill_prompt": "Then polish."},
    ]
    assert writer["tools"] == ["search_docs"]
    assert writer["structured_inputs"] == [
        {"name": "topic", "type": "text", "description": "Topic", "required": True}
    ]
    assert writer["structured_outputs"][0]["name"] == "summary"
    for leaked in ("account_id", "created_at", "scope_kind", "_skills", "_tools"):
        assert leaked not in writer
    assert SECRET not in text
    assert "gpt-5" not in text
    assert "prov-1" not in text


def test_shared_agent_skill_contract_and_rule_travel_once(store: _Store) -> None:
    store.workflows["child"] = _workflow("child", skill_id="draft")
    bundle, _ = _export(store, include_children=True)
    definitions = bundle["definitions"]

    assert [agent["agent_id"] for agent in definitions["agents"]] == ["writer"]
    assert [skill["skill_id"] for skill in definitions["skills"]] == ["draft", "polish"]
    assert [rule["id"] for rule in definitions["guardrails"]].count(GLOBAL_RULE) == 1


def test_step_only_skill_of_a_child_is_carried(store: _Store) -> None:
    bundle, _ = _export(store, include_children=True)

    assert [skill["skill_id"] for skill in bundle["definitions"]["skills"]] == [
        "draft",
        "polish",
        "step_only",
    ]


def test_skill_io_keeps_order_types_and_only_carried_contract_ids(store: _Store) -> None:
    bundle, _ = _export(store)
    draft = next(s for s in bundle["definitions"]["skills"] if s["skill_id"] == "draft")

    assert draft["expected_inputs"] == [
        {"name": "topic", "type": "text", "required": True},
        {"name": "brief", "type": "json", "required": False, "contract_id": "brief.v1"},
    ]
    # A derived field-shape contract is rebuilt on the target; its id does not travel.
    assert draft["expected_outputs"] == [{"name": "draft", "type": "json", "required": True}]
    assert draft["instructions"] == ["Write a draft."]


def test_manual_contracts_travel_after_the_contracts_they_name(store: _Store) -> None:
    bundle, _ = _export(store)
    contracts = bundle["definitions"]["data_contracts"]

    assert [item["contract_id"] for item in contracts] == ["audience.v1", "brief.v1"]
    assert contracts[1]["schema"]["properties"]["audience"] == {"contract_id": "audience.v1"}
    assert "projection_mode" not in contracts[1]


def test_guardrails_keep_scope_without_project_ids(store: _Store) -> None:
    bundle, text = _export(store)
    rules = {rule["id"]: rule for rule in bundle["definitions"]["guardrails"]}

    assert set(rules) == {GLOBAL_RULE, PROJECT_RULE, OTHER_PROJECT_RULE}
    assert rules[GLOBAL_RULE]["scope"] == "global"
    assert rules[GLOBAL_RULE]["agents"] == ["writer"]
    assert rules[GLOBAL_RULE]["config"] == {"patterns": ["ssn"]}
    assert rules[GLOBAL_RULE]["sort_order"] == 2
    assert rules[PROJECT_RULE]["scope"] == "project"
    assert rules[PROJECT_RULE]["source_project_name"] == "Shop"
    assert rules[OTHER_PROJECT_RULE]["source_project_name"] == "Blog"
    assert "proj-shop" not in text
    assert "proj-blog" not in text
    assert ACCOUNT not in text


@pytest.mark.parametrize("agent_id", ["projected", "foreign"])
def test_projection_and_foreign_agents_stay_references(store: _Store, agent_id: str) -> None:
    store.workflows["root"] = _workflow("root", agent_id=agent_id)
    bundle, _ = _export(store)

    assert "definitions" not in bundle or not bundle["definitions"]["agents"]
    assert agent_id in {row["agent_id"] for row in bundle["dependencies"]["agents"]}


def test_external_tools_stay_dependencies(store: _Store) -> None:
    bundle, _ = _export(store)

    assert {row["tool_id"] for row in bundle["dependencies"]["tools"]} == {"search_docs"}
    assert {row["agent_id"] for row in bundle["dependencies"]["agents"]} == {"writer"}


def test_reference_only_keeps_the_earlier_bundle(store: _Store) -> None:
    bundle, _ = _export(store, reference_only=True)

    assert "definitions" not in bundle
    assert {row["agent_id"] for row in bundle["dependencies"]["agents"]} == {"writer"}


def test_export_is_stable_apart_from_its_timestamp(store: _Store) -> None:
    first, _ = _export(store, include_children=True)
    second, _ = _export(store, include_children=True)
    first["metadata"].pop("exported_at")
    second["metadata"].pop("exported_at")

    assert first == second


def test_exported_definitions_pass_strict_parsing(store: _Store) -> None:
    bundle, _ = _export(store, include_children=True)

    assert parse_definitions(bundle["definitions"]) == bundle["definitions"]


@pytest.mark.parametrize(
    "raw,path",
    [
        ({"tools": []}, "definitions.tools"),
        ({"agents": {}}, "definitions.agents"),
        (
            {"agents": [{"agent_id": "a", "display_name": "A", "owner": "x"}]},
            "definitions.agents[0].owner",
        ),
        (
            {
                "agents": [
                    {"agent_id": "a", "display_name": "A"},
                    {"agent_id": "a", "display_name": "B"},
                ]
            },
            "definitions.agents[1].agent_id",
        ),
        (
            {"agents": [{"agent_id": "a", "display_name": "A", "config": {"model": "m"}}]},
            "definitions.agents[0].config",
        ),
        (
            {
                "guardrails": [
                    {
                        "id": "not-a-uuid",
                        "mode": "m",
                        "rule_type": "r",
                        "agents": ["a"],
                        "scope": "global",
                    }
                ]
            },
            "definitions.guardrails[0].id",
        ),
        (
            {
                "guardrails": [
                    {
                        "id": GLOBAL_RULE,
                        "mode": "m",
                        "rule_type": "r",
                        "agents": ["a"],
                        "scope": "team",
                    }
                ]
            },
            "definitions.guardrails[0].scope",
        ),
        (
            {
                "skills": [
                    {"skill_id": "s", "display_name": "S", "expected_inputs": [{"type": "text"}]}
                ]
            },
            "definitions.skills[0].expected_inputs[0]",
        ),
    ],
)
def test_parser_refuses_malformed_definitions_with_their_path(raw: dict, path: str) -> None:
    with pytest.raises(WorkflowPortabilityError) as excinfo:
        parse_definitions(raw)

    assert excinfo.value.code == "bundle_definitions_invalid"
    assert excinfo.value.errors[0]["path"] == path


def test_a_contract_schema_keeps_credential_named_fields(store: _Store) -> None:
    """A field called password is part of the schema; only a literal value under it is masked."""
    store.contracts["brief.v1"]["schema_json"]["properties"]["password"] = {
        "type": "string",
        "minLength": 8,
        "default": "hunter2",  # pragma: allowlist secret
    }
    bundle, text = _export(store)
    brief = next(
        item
        for item in bundle["definitions"]["data_contracts"]
        if item["contract_id"] == "brief.v1"
    )

    assert brief["schema"]["properties"]["password"] == {
        "type": "string",
        "minLength": 8,
        "default": "[redacted]",
    }
    assert "hunter2" not in text


def test_extension_guardrails_are_named_as_the_extension_they_come_with(store: _Store) -> None:
    store.guardrails.append(
        {
            "id": "66666666-6666-4666-8666-666666666666",
            "display_name": "From an extension",
            "project_id": None,
            "account_id": ACCOUNT,
            "source_install_id": "install-guard",
            "_agents": ["writer"],
        }
    )
    bundle, _ = _export(store)

    assert "66666666-6666-4666-8666-666666666666" not in {
        rule["id"] for rule in bundle["definitions"]["guardrails"]
    }
    assert "install-guard" in {
        item["install_id"] for item in bundle["dependencies"].get("extensions", [])
    }
