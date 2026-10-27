"""The run inspector reads the definition a job pinned, never the live workflow (§9)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.application.management.query_services_job_details_mixin import JobQueryServiceDetailsMixin


class _Service(JobQueryServiceDetailsMixin):
    def __init__(self, conn) -> None:
        self._conn = conn

    def _job_context(self, job_id, *, scope=None):
        return SimpleNamespace(conn=self._conn), {"id": job_id}, None


def _view(row):
    with patch(
        "core.application.jobs.workflow_definition_snapshot_repository."
        "WorkflowDefinitionSnapshotRepository.load_for_job",
        return_value=row,
    ):
        return _Service(conn=object()).get_job_definition("job-1")


def test_a_pinned_job_shows_the_snapshot_it_ran():
    definition = {
        "protected": False,
        "steps": [{"step_id": "a", "step_kind": "transform"}],
        "groups": [],
        "execution_graph": {"schema_version": 1, "entry": {"kind": "step", "id": "a"}, "edges": []},
    }
    view = _view(
        {
            "id": "snap-1",
            "workflow_id": "wf",
            "revision": 4,
            "digest": "d" * 64,
            "schema_version": 2,
            "definition_jsonb": json.dumps(definition),
        }
    )

    assert view.available is True
    assert (view.snapshot_id, view.revision) == ("snap-1", 4)
    assert view.steps == definition["steps"]
    assert view.execution_graph["entry"] == {"kind": "step", "id": "a"}


def test_a_job_without_a_snapshot_says_so_instead_of_showing_the_current_definition():
    view = _view(None)

    assert view.available is False
    assert view.unavailable_reason == "definition_snapshot_unavailable"
    assert view.steps == []
    assert view.execution_graph == {}


@pytest.mark.parametrize("as_json", [False, True])
def test_protected_archived_definition_is_read_only_and_redacted(as_json):
    definition = {
        "workflow_id": "protected-extension",
        "protected": True,
        "steps": [{"step_id": "author-step", "config": {"api_key": "proprietary"}}],
        "groups": [{"group_id": "author-group"}],
        "for_each": [{"for_each_id": "each_order", "over": "state.orders"}],
        "execution_graph": {"entry": {"kind": "step", "id": "author-step"}},
    }
    view = _view({"definition_jsonb": json.dumps(definition) if as_json else definition})
    # The run's real structure is shown read-only; sensitive values never are.
    assert view.available is True
    assert view.protected is True
    assert [step["step_id"] for step in view.steps] == ["author-step"]
    assert [item["for_each_id"] for item in view.for_each] == ["each_order"]
    assert "proprietary" not in str(view.model_dump())


def test_old_archive_without_protection_metadata_is_not_assumed_public():
    view = _view({"definition_jsonb": {"steps": [{"step_id": "private"}]}})
    assert view.available is False
    assert view.unavailable_reason == "definition_visibility_unknown"


def test_public_archive_projection_strips_nested_secret_authority():
    view = _view(
        {
            "definition_jsonb": {
                "protected": False,
                "steps": [
                    {
                        "step_id": "a",
                        "config": {
                            "field": {
                                "type": "secret",
                                "default": "never-show",
                                "default_secret_ref": "private-ref",
                            }
                        },
                    }
                ],
            }
        }
    )
    assert view.available is True
    field = view.steps[0]["config"]["field"]
    assert "default" not in field
    assert "default_secret_ref" not in field
    assert field["default_configured"] is True
