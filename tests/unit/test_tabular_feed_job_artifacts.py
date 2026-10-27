from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.jobs.job_artifact_access import resolve_job_artifact_download
from core.application.orchestration.graph.nodes import execute_step_node
from core.application.reports.ui_projection_helpers import _build_message_steps


def _run_state(steps, *, active=0, **state):
    """A run whose cursor is on ``steps[active]`` of a primed pinned definition."""
    from tests._step_run_state import step_run_state

    named = [{"step_id": f"step_{index + 1}", **step} for index, step in enumerate(steps)]
    others = [step for index, step in enumerate(named) if index != active]
    return step_run_state(named[active], *others, **state)


@pytest.mark.parametrize("mapped", [False, True])
def test_tabular_csv_node_persists_downloadable_output_without_bulk_state(mapped: bool) -> None:
    plane = ArtifactDatasetPlane()
    scope = {"account_id": "account-export", "project_id": "project-export"}
    dataset, _ = plane.create_dataset(
        owner_scope=scope,
        run_id="job-export",
        step_id="prepare",
        schema={"columns": [{"name": "sku", "type": "text"}]},
        rows=[{"sku": "00123"}],
    )
    step = {
        "step_id": "export-csv",
        "step_kind": "tabular_feed",
        "override": {
            "tabular_feed": {
                "operation": "write",
                "input": {"dataset_handle": "state.dataset"},
                "format": "csv",
                "options": {"filename": "catalog.csv"},
                "result_target": "state.export_result",
            }
        },
    }
    if mapped:
        step["output_mapping_jsonb"] = {"artifact_handle": "state.export_file"}
    state = _run_state(
        [step],
        _artifact_plane=plane,
        _trusted_runtime_context=scope,
        job_id="job-export",
        dataset=dataset.dataset_id,
        agent_messages=[],
    )
    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = execute_step_node(state)

    assert update["agent_messages"][-1]["status"] == "pass", update["agent_messages"]
    result = update["export_result"]
    artifact_id = result["artifact_handle"]
    assert not artifact_id.startswith("artifact:")
    if mapped:
        assert update["export_file"] == artifact_id
    else:
        assert "export_file" not in update
    # Replay the durable message, not mutable workflow state or a fabricated report.
    messages = json.loads(json.dumps(update["agent_messages"]))
    assert messages[-1]["status"] == "pass"
    assert "00123" not in json.dumps(messages)
    steps = _build_message_steps(
        messages,
        project_id="project-export",
        job_id="job-export",
        resolve_artifact=plane.get_artifact,
    )
    assert steps[0]["outputs"]["row_count"] == 1
    assert steps[0]["output_artifacts"] == [
        {
            "artifact_id": artifact_id,
            "role": "output",
            "filename": "catalog.csv",
            "content_type": "text/csv",
            "size_bytes": result["size_bytes"],
            "available": True,
            "download_url": f"/api/jobs/job-export/artifacts/{artifact_id}",
        }
    ]
    download = resolve_job_artifact_download(
        job={"id": "job-export", "project_id": "project-export"},
        artifact_id=artifact_id,
        resolve_artifact=plane.get_artifact,
    )
    body = plane.read_artifact_bytes(artifact_id, max_size_bytes=1024)
    assert body == b"sku\n00123\n"
    assert download.filename == "catalog.csv"
    assert download.content_type == "text/csv"
    assert download.size_bytes == len(body)
    assert hashlib.sha256(body).hexdigest() == result["sha256"]


@pytest.mark.parametrize(
    ("changes", "visible"),
    [
        ({}, True),
        ({"expired": True}, True),
        ({"persistence_status": "preparing"}, True),
        ({"sha256": ""}, True),
        ({"object_key": ""}, True),
        ({"owner_scope": {"project_id": "foreign", "job_id": "job-export"}}, False),
        ({"owner_scope": {"project_id": "project-export", "job_id": "foreign"}}, False),
        (None, False),
    ],
)
def test_tabular_report_retains_canonical_artifact_availability_guards(changes, visible) -> None:
    metadata = {
        "artifact_id": "art-export",
        "owner_scope": {"project_id": "project-export", "job_id": "job-export"},
        "filename": "catalog.csv",
        "content_type": "text/csv",
        "size_bytes": 10,
        "object_key": "artifacts/art-export/catalog.csv",
        "sha256": "a" * 64,
        "persistence_status": "available",
    }
    record = None if changes is None else {**metadata, **changes}
    message = {
        "status": "pass",
        "artifacts": {
            "step_kind": "tabular_feed",
            "tabular_feed": {"ok": True, "artifact_handle": "art-export", "row_count": 1},
            "outputs": {"file": "artifact:art-export", "mapped_count": 1},
        },
    }
    step = _build_message_steps(
        [message],
        project_id="project-export",
        job_id="job-export",
        resolve_artifact=lambda _artifact_id: record,
    )[0]
    assert step["outputs"] == {"mapped_count": 1}
    descriptors = step["output_artifacts"]
    assert len(descriptors) == int(visible)  # No duplicate from the two references.
    if visible:
        available = not changes
        assert descriptors[0]["available"] is available
        assert descriptors[0]["download_url"] == (
            "/api/jobs/job-export/artifacts/art-export" if available else None
        )


@pytest.mark.parametrize("kind", ["tabular_feed", "http_request"])
def test_failed_or_unrelated_tabular_diagnostics_do_not_invent_outputs(kind: str) -> None:
    step = _build_message_steps(
        [
            {
                "artifacts": {
                    "step_kind": kind,
                    "tabular_feed": {"ok": False, "artifact_handle": "art-x"},
                }
            }
        ]
    )[0]
    assert step["outputs"] == {}
    assert step["output_artifacts"] == []
