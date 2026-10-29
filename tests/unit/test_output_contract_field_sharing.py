"""Agent outputs keep the shape their own skill declares.

One registry field row exists per output name and is shared by every skill and
workflow that uses the name. A workflow callable output named like a skill's
object-list output must not erase that skill's linked data contract, or the
model is asked for strings and the strings pass validation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from core.application.orchestration.agents.common_runtime_structured_utils import (
    _draft_response_satisfies_phase_result_schema,
)
from core.application.registry.output_contract_fields import response_field_json_schema
from core.application.registry.public_contract_projection import (
    _build_desired_projection_state,
)
from core.application.registry.step_output_contract import resolve_step_output_contract
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService

ITEMS = "catalog.meeting_action_items.v1"
ITEM_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["task", "owner"],
        "properties": {"task": {"type": "string"}, "owner": {"type": "string"}},
    },
}


class ProjectionStore:
    def __init__(self, *, workflow_contract_id=""):
        self.skills = [
            {
                "skill_id": "minutes",
                "enabled": True,
                "_inputs": [],
                "_outputs": [
                    {"name": "action_items", "type": "array", "contract_id": ITEMS},
                ],
            }
        ]
        output = {"name": "action_items", "type": "array", "required": False}
        if workflow_contract_id:
            output["contract_id"] = workflow_contract_id
        self.workflows = [
            {
                "workflow_id": "meeting_actions",
                "enabled": True,
                "settings": {"callable": {"enabled": True, "expected_outputs": [output]}},
                "steps": [],
            }
        ]

    def list_skills(self, enabled_only=False):
        return self.skills

    def list_agents(self, enabled_only=False):
        return []

    def list_tools(self, enabled_only=False):
        return []

    def list_workflows(self, enabled_only=False):
        return self.workflows

    def list_data_contracts(self, enabled_only=False, ensure_projection=False):
        return [{"contract_id": ITEMS, "projection_mode": "manual", "schema_json": ITEM_SCHEMA}]


def test_a_contractless_workflow_output_keeps_the_skill_contract_on_the_shared_field():
    state = _build_desired_projection_state(ProjectionStore(), now=datetime.now(UTC))
    assert state["fields"]["action_items"]["data_contract_id"] == ITEMS


def test_a_workflow_output_with_its_own_contract_links_it():
    store = ProjectionStore(workflow_contract_id="catalog.other_items.v1")
    store.skills = []
    state = _build_desired_projection_state(store, now=datetime.now(UTC))
    assert state["fields"]["action_items"]["data_contract_id"] == "catalog.other_items.v1"


class BlockStore:
    """A derived agent+skill block whose shared field row lost the skill's contract."""

    def __init__(self):
        self.contracts = {
            ITEMS: {"projection_mode": "manual", "schema_json": ITEM_SCHEMA, "enabled": True},
            "auto.action_items.v1": {
                "projection_mode": "derived",
                "derived_from_kind": "field_shape",
                "derived_from_ref": "action_items",
                "kind": "list",
                "schema_json": {"type": "array", "items": {}},
                "enabled": True,
            },
        }
        self.block = {
            "block_key": "agent.writer.minutes",
            "source_kind": "agent",
            "source_ref": "writer::minutes",
            "derived_from_kind": "agent_skill_pair",
            "enabled": True,
            "outputs": [
                {
                    "field_key": "action_items",
                    "value_type": "list",
                    "data_contract_id": "auto.action_items.v1",
                    "required_by_default": True,
                }
            ],
        }

    def get_skill(self, _):
        return {"skill_id": "minutes", "_outputs": self.get_skill_outputs(_)}

    def get_skill_outputs(self, _):
        return [{"name": "action_items", "type": "array", "contract_id": ITEMS}]

    def get_data_contract(self, contract_id):
        return self.contracts.get(contract_id)

    def list_published_block_contracts(self, *, enabled_only):
        return [self.block]

    def get_published_block_contract(self, key, *, account_id=None):
        return self.block if key == self.block["block_key"] else None


def _block_contract(store):
    return resolve_step_output_contract(
        store, agent_id="writer", skill_id="minutes", account_id="account-a"
    )


def test_a_derived_agent_skill_block_takes_the_shape_from_the_skill_output():
    contract = _block_contract(BlockStore())
    row = contract.rows[0]
    assert row["response_schema"]["items"]["type"] == "object"
    assert contract.validate({"action_items": [{"task": "Fix", "owner": "Tom"}]}) == []
    assert contract.validate({"action_items": ["Tom: fix the bug"]}) == [
        "Invalid type or value for output 'action_items'"
    ]


def test_a_curated_block_keeps_its_own_field_contract():
    store = BlockStore()
    store.block = {**store.block, "derived_from_kind": "", "block_key": "agent.writer.minutes"}
    contract = _block_contract(store)
    assert contract.validate({"action_items": ["free text"]}) == []


def test_bare_array_and_object_outputs_are_requested_as_their_own_type():
    assert response_field_json_schema("array") == {"type": "array", "items": {"type": "string"}}
    assert response_field_json_schema("object") == {"type": "object", "additionalProperties": True}


def test_an_empty_required_text_output_is_reported_as_empty_and_an_empty_list_is_valid():
    store = BlockStore()
    store.block = {
        **store.block,
        "outputs": [
            *store.block["outputs"],
            {"field_key": "summary", "value_type": "text", "required_by_default": True},
        ],
    }
    contract = _block_contract(store)
    assert contract.validate({"action_items": [], "summary": ""}) == [
        "Required output 'summary' is empty"
    ]
    assert contract.validate({"action_items": [], "summary": "ok"}) == []


def _draft(artifacts):
    import json

    return SimpleNamespace(
        content=json.dumps({"schema_version": "phase_result_v2", "artifacts": artifacts})
    )


def test_a_draft_with_strings_where_objects_are_required_does_not_replace_the_structured_call():
    schema = {
        "properties": {
            "artifacts": {
                "type": "object",
                "properties": {"action_items": ITEM_SCHEMA},
                "required": ["action_items"],
            }
        }
    }
    strings = _draft({"action_items": ["Tom: fix the bug"]})
    objects = _draft({"action_items": [{"task": "Fix the bug", "owner": "Tom"}]})
    assert _draft_response_satisfies_phase_result_schema(strings, schema) is False
    assert _draft_response_satisfies_phase_result_schema(objects, schema) is True


def test_an_empty_required_workflow_output_says_it_is_empty():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_outputs": [{"name": "summary", "type": "string", "required": True}],
            }
        }
    )
    result = WorkflowInterfaceService().validate_outputs(interface, {"outputs": {"summary": ""}})
    assert [error["message"] for error in result.errors] == [
        "Workflow output 'summary' is required but empty"
    ]
