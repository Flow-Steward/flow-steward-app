"""`jobs show --include-details` projects a completed run's result files (#34).

The CLI built its job query service without an artifact resolver, so a completed
workflow whose result carries files reached the public-result projection with
nothing to look the files up in, and the whole detail answered
`dependency_unavailable` / "Public workflow result is unavailable". The API was
fine: it hands the same service an ArtifactPresentationResolver. These tests go
through the product CLI runtime and the `jobs show` binding, the path the
operator's command takes in fs-app (Scalable) and compact-runtime (Compact).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management import management_runtime
from core.application.management.errors import ServiceUnavailableError
from core.entrypoints.cli.platform.service_bindings.jobs import jobs_show
from core.entrypoints.cli.runtime import CLIRuntime

JOB_ID = "job-files-1"
PROJECT_ID = "7"


class _JobStore:
    """The job row of a completed run with two result files, and empty history."""

    def __init__(self) -> None:
        self.job = {
            "id": JOB_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": PROJECT_ID,
            "selected_workflow": "export-orders",
            "status": "completed",
            "task_summary": "Export orders",
            "started_at": None,
            "finished_at": None,
            "meta": {
                "workflow_outputs": {
                    "outputs": {"rows": 2},
                    "artifacts": {
                        "orders_jsonl": "artifact:art-orders-jsonl",
                        "orders_csv": "artifact:art-orders-csv",
                    },
                },
            },
        }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return dict(self.job) if job_id == JOB_ID else None

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        if str(project_id) != PROJECT_ID:
            return None
        return {"id": PROJECT_ID, "account_id": DEFAULT_ACCOUNT_ID, "name": "Orders"}

    def get_job_usage_summary(self, _job_id: str) -> dict[str, Any]:
        return {}

    def __getattr__(self, _name: str):
        # Logs, messages, events, errors and journals: a run with no history.
        return lambda *_args, **_kwargs: []


class _ArtifactPlane:
    """The stored metadata of the run's files, as the artifact plane returns it."""

    def __init__(self) -> None:
        self.lookups: list[str] = []
        self.rows = {
            "art-orders-jsonl": ("orders.jsonl", "application/json", 120),
            "art-orders-csv": ("orders.csv", "text/csv", 64),
        }

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        self.lookups.append(artifact_id)
        row = self.rows.get(artifact_id)
        if row is None:
            return None
        filename, content_type, size = row
        return {
            "artifact_id": artifact_id,
            "project_id": PROJECT_ID,
            "run_id": f"job:{JOB_ID}",
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size,
            "object_key": f"private/{artifact_id}",
            "persistence_status": "available",
        }

    def read_artifact_presentation(self, _metadata: dict[str, Any]) -> None:
        return None


def _runtime(monkeypatch: pytest.MonkeyPatch, plane_factory) -> CLIRuntime:
    monkeypatch.setattr(management_runtime, "job_invocation_artifact_plane", plane_factory)
    runtime = CLIRuntime()
    runtime.__dict__["job_store"] = _JobStore()
    return runtime


def _show(runtime: CLIRuntime) -> dict[str, Any]:
    view = asyncio.run(jobs_show(runtime, {"job_id": JOB_ID, "include_details": True}))
    return view.model_dump(mode="json")


def test_jobs_show_with_details_projects_the_completed_runs_result_files(monkeypatch):
    plane = _ArtifactPlane()
    runtime = _runtime(monkeypatch, lambda: plane)

    detail = _show(runtime)

    result = detail["workflow_result"]
    assert result["outputs"] == {"rows": 2}
    assert set(result["artifacts"]) == {"orders_jsonl", "orders_csv"}
    csv = result["artifacts"]["orders_csv"]
    assert csv["artifact_id"] == "art-orders-csv"
    assert csv["filename"] == "orders.csv"
    assert csv["content_type"] == "text/csv"
    assert csv["size_bytes"] == 64
    assert csv["download_url"] == f"/api/jobs/{JOB_ID}/artifacts/art-orders-csv"
    assert result["artifacts"]["orders_jsonl"]["filename"] == "orders.jsonl"
    assert "private/art-orders" not in str(detail)
    assert set(plane.lookups) >= {"art-orders-jsonl", "art-orders-csv"}


def test_jobs_show_without_details_does_not_build_artifact_storage(monkeypatch):
    def storage_must_not_be_built():
        raise AssertionError("the status summary must not touch artifact storage")

    runtime = _runtime(monkeypatch, storage_must_not_be_built)

    summary = asyncio.run(jobs_show(runtime, {"job_id": JOB_ID, "include_details": False}))

    assert summary["status"] == "completed"


def test_unreachable_artifact_storage_still_fails_closed(monkeypatch):
    def storage_unavailable():
        raise OSError("artifact storage is not reachable")

    runtime = _runtime(monkeypatch, storage_unavailable)

    with pytest.raises(ServiceUnavailableError, match="Public workflow result is unavailable"):
        _show(runtime)
