from __future__ import annotations

import math

import pytest

from core.application.workflows.workflow_definition_snapshot import (
    canonical_json,
    definition_digest,
    frozen_definition,
)
from tests._workflow_definitions import linear_workflow


def test_frozen_definition_moves_the_graph_out_of_settings() -> None:
    workflow = linear_workflow(
        {"step_id": "a", "step_kind": "transform"}, settings={"traits": {"x": 1}}
    )
    frozen = frozen_definition(workflow)
    assert frozen["execution_graph"] == workflow["settings"]["execution_graph"]
    assert frozen["settings"] == {"traits": {"x": 1}}
    assert [step["step_id"] for step in frozen["steps"]] == ["a", "done"]


def test_digest_ignores_key_order_but_not_array_order() -> None:
    first = {"steps": [{"step_id": "a", "step_kind": "transform"}], "groups": []}
    reordered_keys = {"groups": [], "steps": [{"step_kind": "transform", "step_id": "a"}]}
    assert definition_digest(first) == definition_digest(reordered_keys)
    swapped = {"steps": [{"step_id": "b"}, {"step_id": "a"}], "groups": []}
    unswapped = {"steps": [{"step_id": "a"}, {"step_id": "b"}], "groups": []}
    assert definition_digest(swapped) != definition_digest(unswapped)


def test_digest_covers_the_schema_version() -> None:
    definition = {"steps": [], "groups": []}
    assert definition_digest(definition, schema_version=1) != definition_digest(
        definition, schema_version=2
    )


def test_canonical_json_is_compact_and_refuses_non_finite_numbers() -> None:
    assert canonical_json({"b": 1, "a": [2, 1]}) == '{"a":[2,1],"b":1}'
    with pytest.raises(ValueError):
        canonical_json({"value": math.inf})


@pytest.mark.parametrize(
    ("metadata", "protected"),
    [
        ({"source_install_id": "extension-install"}, True),
        ({"source_install_id": "extension-install", "protected": False}, True),
        ({"projection_mode": "derived"}, True),
        ({}, False),
        (
            {"source_install_id": "extension-install", "export_policy": {"allow_export": True}},
            False,
        ),
    ],
)
def test_snapshot_pins_canonical_protection_even_after_source_is_removed(metadata, protected):
    workflow = linear_workflow({"step_id": "a", "step_kind": "transform"})
    workflow.update(metadata)
    assert frozen_definition(workflow)["protected"] is protected


def test_canvas_notes_and_their_revision_do_not_change_executable_snapshot():
    workflow = linear_workflow({"step_id": "a", "step_kind": "transform"})
    original = frozen_definition(workflow)
    workflow["settings"]["canvas_annotations"] = [
        {"id": "note", "text": "Documentation", "x": 40, "y": 50}
    ]
    workflow["settings"]["canvas_annotations_revision"] = 7
    annotated = frozen_definition(workflow)
    assert annotated == original
    assert definition_digest(annotated) == definition_digest(original)
    assert workflow["settings"]["canvas_annotations"][0]["text"] == "Documentation"


@pytest.mark.parametrize("allow_export", [False, True])
def test_protected_annotation_edits_keep_trusted_visibility_and_execution_digest(allow_export):
    workflow = linear_workflow({"step_id": "a", "step_kind": "transform"})
    workflow.update(
        source_install_id="extension-install",
        protected=False,
        export_policy={"allow_export": allow_export},
    )
    original = frozen_definition(workflow)
    workflow["settings"]["canvas_annotations"] = [
        {
            "id": "private-note",
            "text": "private-documentation",
            "image": {"asset_id": "private-image"},
        }
    ]
    workflow["settings"]["canvas_annotations_revision"] = 99
    annotated = frozen_definition(workflow)
    assert annotated["protected"] is not allow_export
    assert annotated == original
    assert definition_digest(annotated) == definition_digest(original)
    assert "private-documentation" not in canonical_json(annotated)
    assert "private-image" not in canonical_json(annotated)
