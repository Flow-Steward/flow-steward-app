from __future__ import annotations

import json

import pytest

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.tabular_feed.context import TabularFeedExecutionContext
from core.application.tabular_feed.service import TabularFeedService
from core.infrastructure.tabular_feed import engine, engine_plane

pytestmark = [pytest.mark.integration]


def _context() -> TabularFeedExecutionContext:
    return TabularFeedExecutionContext(
        account_id="acct",
        project_id="proj",
        workflow_id="wf",
        run_id="run",
        step_id="step",
    )


def test_old_committed_pointer_is_not_a_fallback_when_database_is_missing():
    plane = ArtifactDatasetPlane()
    service = TabularFeedService(plane=plane)
    action = {"runtime_context": _context().as_runtime_context()}
    pointer_key = engine_plane._pointer_key(action, "supplier-feed")

    with engine.injected_plane(plane):
        _plane, storage = engine._plane()
        storage.put(
            pointer_key,
            json.dumps(
                {
                    "snapshot_artifact_id": "art_snapshot",
                    "metadata": {"raw_sha256": "abc123"},
                }
            ).encode("utf-8"),
            content_type="application/json",
        )

    result = service.execute(
        operation="checksum",
        payload={"input": {"profile_key": "supplier-feed", "raw_sha256": "abc123"}},
        context=_context(),
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "snapshot_storage_unavailable"
    assert pointer_key == "tabular_feed/snapshots/YWNjdA/cHJvag/d2Y/supplier-feed/committed.json"


def test_legacy_candidate_without_bound_baseline_is_not_publishable():
    plane = ArtifactDatasetPlane()
    service = TabularFeedService(plane=plane)
    artifact, _ = plane.create_artifact(
        owner_scope={"account_id": "acct", "project_id": "proj", "workflow_id": "wf"},
        run_id="run",
        step_id="diff",
        filename="supplier-feed-candidate.jsonl.gz",
        content_type="application/gzip",
        body=b"snapshot",
    )
    action = {"runtime_context": _context().as_runtime_context()}
    with engine.injected_plane(plane):
        _plane, storage = engine._plane()
        storage.put(
            engine._candidate_meta_key(action, "supplier-feed", artifact.artifact_id),
            json.dumps({"raw_sha256": "def456"}).encode("utf-8"),
            content_type="application/json",
        )

    result = service.execute(
        operation="commit_snapshot",
        payload={
            "input": {
                "profile_key": "supplier-feed",
                "snapshot_artifact_id": artifact.artifact_id,
            }
        },
        context=_context(),
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "snapshot_baseline_required"


def test_tabular_file_artifact_persists_metadata_in_reserved_namespace(tmp_path):
    plane = ArtifactDatasetPlane()
    source = tmp_path / "feed.csv"
    source.write_bytes(b"sku,qty\n00123,2\n")
    action = {"runtime_context": _context().as_runtime_context()}

    with engine.injected_plane(plane):
        artifact_id, meta = engine._create_artifact_from_file(
            action,
            filename="feed.csv",
            content_type="text/csv",
            path=str(source),
        )

    restarted = ArtifactDatasetPlane(storage=plane._storage)
    restored = restarted.get_artifact(artifact_id)

    assert restored is not None
    assert restored["size_bytes"] == meta["size_bytes"]
    assert restored["sha256"] == meta["sha256"]
