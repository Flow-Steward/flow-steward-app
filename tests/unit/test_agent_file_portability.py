"""Portable workflow definitions preserve file selectors without runtime artifacts."""

from copy import deepcopy

import yaml

from core.application.workflows.workflow_portability import WorkflowPortabilityService
from tests.unit.test_workflow_portability import _manual_workflow, _resolved_db, _reuse_definitions


def test_agent_file_bindings_survive_portable_workflow_round_trip():
    workflow = _manual_workflow("file_source")
    step = workflow["steps"][0]
    for key in ("block_key", "workflow_id", "connection_binding_jsonb"):
        step.pop(key, None)
    bindings = [
        {"name": "notes", "source": "state.outputs.markdown"},
        {"name": "image", "source": "state.outputs.image"},
        {"name": "audio", "source": "state.outputs.audio"},
        {"name": "video", "source": "state.outputs.video"},
    ]
    step["override"] = {
        "add_tools": ["search_docs"],
        "agent": {"file_bindings": bindings, "input_mapping": {"request": "state.inputs.request"}},
    }
    expected_override = deepcopy(step["override"])
    source_db = _resolved_db(workflows={"file_source": workflow})
    bundle = WorkflowPortabilityService(source_db).export_yaml("file_source")
    exported_step = yaml.safe_load(bundle)["workflows"][0]["steps"][0]
    assert exported_step["override"] == expected_override
    assert "artifact_id" not in bundle
    assert "object_key" not in bundle
    assert "artifacts/" not in bundle

    target_db = _resolved_db()
    preflight = WorkflowPortabilityService(target_db).preflight_import(
        bundle, target_workflow_id="file_copy", resolutions=_reuse_definitions(bundle)
    )
    assert [item for item in preflight["dependencies"] if item.get("blocking")] == []
    result = WorkflowPortabilityService(target_db).apply_import(
        bundle,
        target_workflow_id="file_copy",
        resolutions=_reuse_definitions(bundle),
    )
    assert result["workflow_id"] == "file_copy"
    imported = target_db.workflows["file_copy"]["steps"][0]
    assert imported["override"] == expected_override


def test_file_binding_contract_rejects_wrong_shapes_and_unknown_keys():
    from core.application.workflows.workflow_step_config_contracts import validate_step_config_keys

    def issues(value):
        return validate_step_config_keys(
            step_kind="agent", override={"agent": {"file_bindings": value}}, step_path="steps.s1"
        )

    assert not issues([{"name": "notes", "source": "state.outputs.notes"}])
    for value in (
        None,
        {},
        "state.file",
        [None],
        [{"name": "notes"}],
        [{"name": "notes", "source": 7}],
        [{"name": "notes", "source": "state.file", "artifact_id": "art_x"}],
    ):
        assert issues(value), value
    unknown = validate_step_config_keys(
        step_kind="agent", override={"agent": {"unknown": True}}, step_path="steps.s1"
    )
    assert unknown[0]["code"] == "workflow.step_config_key_unknown"
