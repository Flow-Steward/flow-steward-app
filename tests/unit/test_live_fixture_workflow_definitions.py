"""Workflows the live suites and the release runner author are definitions Core accepts.

The e2e/UI neutral fixtures and the candidate semantic checks build their
workflows through ``/api/workflows/{id}/changes`` against a live stand only, so
no gate would see a change set Core refuses. Each change set is applied here
the way the change endpoint applies it and checked by the definition validator.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.application.workflows.workflow_definition_changes import _ChangeSet, _load_definition
from core.application.workflows.workflow_definition_validation import (
    validate_workflow_definition,
)
from scripts.smoke import candidate_semantic_checks
from tests.e2e.neutral_registry import (
    NEUTRAL_APPROVAL_STEP_ID,
    NEUTRAL_WORKFLOW_IDS,
    neutral_workflow_operations,
    neutral_workflow_payloads,
)


def _built(operations: list[dict[str, Any]]) -> dict[str, Any]:
    definition = _load_definition({})
    change_set = _ChangeSet(definition)
    for index, operation in enumerate(operations):
        change_set.apply(index, operation)
    return definition.payload()


class _RecordingApi:
    """Answers the create, read, change and enable calls of one fixture workflow."""

    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.operations: list[dict[str, Any]] = []

    def request(self, method: str, path: str, payload: Any = None, **_kwargs: Any):
        if method == "POST" and path == "/api/workflows":
            self.created = dict(payload or {})
            return 200, {"status": "created"}
        if method == "POST" and path.endswith("/changes"):
            self.operations = list((payload or {})["operations"])
            return 200, {"valid": True, "issues": [], "applied": True}
        if method in {"GET", "PATCH"} and path.startswith("/api/workflows/"):
            return 200, {"revision": 1}
        if method == "GET" and path == "/api/workspaces":
            return 200, [{"id": "workspace-1", "workspace_key": "default"}]
        if method == "POST" and path == "/api/projects":
            return 201, {"project_id": "project-1"}
        raise AssertionError(f"unexpected request: {method} {path}")


@pytest.mark.parametrize("workflow_id", NEUTRAL_WORKFLOW_IDS)
def test_neutral_workflow_change_set_builds_a_valid_definition(workflow_id: str) -> None:
    definition = _built(neutral_workflow_operations(workflow_id))

    assert validate_workflow_definition(definition) == []
    assert definition["execution_graph"]["entry"] == {"kind": "step", "id": "workflow_start"}


def test_neutral_workflow_payloads_are_metadata_only_and_disabled() -> None:
    for payload in neutral_workflow_payloads():
        assert payload["enabled"] is False
        assert not {"phases", "steps", "groups"} & set(payload)


def test_only_the_gated_neutral_workflow_has_an_approval_step_routing_both_outcomes() -> None:
    gated, auto = (_built(neutral_workflow_operations(wid)) for wid in NEUTRAL_WORKFLOW_IDS)

    approval_outcomes = {
        edge["outcome"]["name"]
        for edge in gated["execution_graph"]["edges"]
        if edge["source"]["id"] == NEUTRAL_APPROVAL_STEP_ID
    }
    assert approval_outcomes == {"approved", "rejected"}
    assert "approval" not in {step["step_kind"] for step in auto["steps"]}


@pytest.mark.parametrize(
    "build",
    [
        candidate_semantic_checks._create_hitl_workflow,
        candidate_semantic_checks._create_cutover_fixture,
    ],
    ids=["a23_hitl", "e12_cutover"],
)
def test_release_runner_fixture_workflows_build_valid_definitions(build) -> None:
    api = _RecordingApi()

    build(api)

    assert api.created["enabled"] is False
    assert "phases" not in api.created
    assert validate_workflow_definition(_built(api.operations)) == []


def test_a23_rejection_is_a_bounded_rework_of_the_recorded_effect() -> None:
    api = _RecordingApi()

    candidate_semantic_checks._create_hitl_workflow(api)

    edges = _built(api.operations)["execution_graph"]["edges"]
    rejected = [edge for edge in edges if edge["outcome"]["name"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["target"]["id"] == "normalize_effect"
    assert rejected[0]["rework"] is True
    assert rejected[0]["rework_limit"] == 2
