"""n8n independent branches map to native Flow Steward parallel groups."""

from __future__ import annotations

import pytest
import yaml

from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from tests.unit.test_workflow_n8n_conversion import convert, source


def fanout_source():
    data = source()
    data["nodes"][1] = {
        "name": "Left",
        "type": "n8n-nodes-base.noOp",
        "typeVersion": 1,
        "parameters": {},
    }
    data["nodes"].append(
        {"name": "Right", "type": "n8n-nodes-base.noOp", "typeVersion": 1, "parameters": {}}
    )
    data["connections"] = {
        "Start": {
            "main": [
                [
                    {"node": "Left", "type": "main", "index": 0},
                    {"node": "Right", "type": "main", "index": 0},
                ]
            ]
        }
    }
    return data


def test_independent_fanout_is_a_native_parallel_group_with_typed_lane_exits():
    result = convert(fanout_source(), parallel_fanout=True)
    assert result["status"] == "converted", result["issues"]
    workflow = yaml.safe_load(result["bundle_yaml"])["workflows"][0]
    assert validate_workflow_definition(workflow) == []
    assert len(workflow["groups"]) == 1
    group = workflow["groups"][0]
    assert group["body"]["kind"] == "lanes" and len(group["body"]["lanes"]) == 2
    members = [s for s in workflow["steps"] if s.get("group_id") == group["group_id"]]
    assert len(members) == 2 and len({s["lane_id"] for s in members}) == 2
    exits = [
        e["target"]
        for e in workflow["execution_graph"]["edges"]
        if e["target"]["kind"] == "group_exit"
    ]
    assert {e["lane_id"] for e in exits} == {s["lane_id"] for s in members}
    assert any(i["code"] == "n8n.parallel_fanout" and not i["blocking"] for i in result["issues"])


def test_fanout_does_not_silently_change_source_sequential_branch_order():
    result = convert(fanout_source())
    assert result["status"] == "blocked" and result["bundle_yaml"] is None
    assert "Parallel" in result["issues"][0]["repair_guidance"]


def test_each_http_lane_keeps_an_object_guard_without_illegal_if_steps():
    data = fanout_source()
    for node in data["nodes"][1:]:
        node.update(
            type="n8n-nodes-base.httpRequest",
            typeVersion=4.2,
            parameters={"url": "https://example.test/", "options": {}},
        )
    result = convert(data, parallel_fanout=True, object_response_nodes=[1, 2])
    assert result["status"] == "converted", result["issues"]
    workflow = yaml.safe_load(result["bundle_yaml"])["workflows"][0]
    assert validate_workflow_definition(workflow) == []
    members = [s for s in workflow["steps"] if s.get("group_id")]
    assert {s["step_kind"] for s in members} == {"http_request", "template"}
    for step in members:
        if step["step_kind"] == "template":
            assert step["override"]["template"]["inputs"][0]["type"] == "object"
            assert step["error_policy"]["on_error"] == "fail"


@pytest.mark.parametrize("bad_index", [1, 2])
def test_shared_join_is_still_an_explicit_mapping_gap(bad_index):
    data = fanout_source()
    data["connections"][data["nodes"][bad_index]["name"]] = {
        "main": [[{"node": "Right" if bad_index == 1 else "Left", "type": "main", "index": 0}]]
    }
    result = convert(data, parallel_fanout=True)
    assert result["status"] == "blocked" and result["bundle_yaml"] is None


def test_complete_official_get_graph_keeps_all_four_http_branches():
    import json

    from tests.unit.workflow_migration_skill_helpers import SKILL_ROOT

    fixture = SKILL_ROOT / "assets/source_examples/n8n_http_fanout.json"
    data = json.loads(fixture.read_text())["workflow"]
    result = convert(data, object_response_nodes=[1, 2, 3, 4], parallel_fanout=True)
    assert result["status"] == "converted", result["issues"]
    workflow = yaml.safe_load(result["bundle_yaml"])["workflows"][0]
    assert validate_workflow_definition(workflow) == []
    assert len(workflow["groups"]) == 1
    assert len(workflow["groups"][0]["body"]["lanes"]) == 4
    assert len([s for s in workflow["steps"] if s["step_kind"] == "http_request"]) == 4
    assert len(result["node_mappings"]) == len(data["nodes"])
    assert "public-fixture-placeholder" not in result["bundle_yaml"]


def test_parallel_bundle_passes_real_authoring_preflight_without_writes(monkeypatch):
    from tests.unit.test_workflow_authoring_api import BASE, _client

    result = convert(fanout_source(), parallel_fanout=True)
    client, db, refreshed = _client(monkeypatch)
    response = client.post(BASE + "/validate", json={"bundle_yaml": result["bundle_yaml"]})
    assert response.status_code == 200
    assert response.json()["can_save"] is True, response.json()
    assert db.workflows == {} and refreshed == []
