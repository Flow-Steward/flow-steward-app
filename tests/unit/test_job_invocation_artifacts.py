from __future__ import annotations

import hashlib
import json
import time
from unittest.mock import MagicMock

import pytest

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.jobs.job_invocation_artifacts import (
    JobReplaySnapshotError,
    build_effective_input_projection,
    capture_replay_snapshot,
    load_replay_snapshot,
    mint_replay_file_handles,
)
from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage
from core.infrastructure.security import secret_codec


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "invocation-test-key"})


def _complete_snapshot(**overrides):
    inputs = dict(overrides.pop("inputs", {}))
    files = list(overrides.pop("files", []))
    snapshot = {
        "workflow_id": "wf",
        "inputs": inputs,
        "files": files,
        "test_mode": False,
        "input_contract": "direct_inputs",
        "task_envelope": {"inputs": inputs, "files": files},
        "_workflow_secret_ref_fingerprints": [],
    }
    snapshot.update(overrides)
    return snapshot


def test_projection_redacts_recursively_and_reports_canonical_identity() -> None:
    inputs = {
        "invoice_id": "INV-42",
        "credential": "declared-secret",
        "nested": {
            "api_key": "heuristic-secret",  # pragma: allowlist secret - redaction fixture
            "safe": "visible",
        },
    }

    projection = build_effective_input_projection(
        inputs=inputs,
        files=[],
        declared_secret_inputs={"credential"},
    )

    canonical = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert projection["mapped_inputs"] == {
        "credential": "[REDACTED]",
        "invoice_id": "INV-42",
        "nested": {"api_key": "[REDACTED]", "safe": "visible"},
    }
    assert projection["source_keys"] == ["credential", "invoice_id", "nested"]
    assert projection["size_bytes"] == len(canonical.encode("utf-8"))
    assert projection["sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert projection["truncated"] is False
    assert "declared-secret" not in json.dumps(projection)
    assert "heuristic-secret" not in json.dumps(projection)


def test_projection_bounds_large_nested_values_without_losing_file_handles() -> None:
    projection = build_effective_input_projection(
        inputs={
            "rows": [{"id": index, "description": "x" * 4000} for index in range(200)],
            "notes": "y" * 100_000,
        },
        files=[
            {
                "artifact_id": "art_input",
                "sha256": "a" * 64,
                "size_bytes": 123,
                "filename": "input.csv",
                "content_type": "text/csv",
            }
        ],
    )

    encoded = json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= 16 * 1024
    assert projection["truncated"] is True
    assert projection["files"] == [
        {
            "artifact_id": "art_input",
            "sha256": "a" * 64,
            "size_bytes": 123,
            "filename": "input.csv",
            "content_type": "text/csv",
        }
    ]
    assert "100000" in json.dumps(projection["mapped_inputs"])


def test_replay_snapshot_is_encrypted_and_round_trips_exact_values() -> None:
    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    snapshot = _complete_snapshot(
        workflow_id="invoice-import",
        inputs={
            "api_key": "runtime-secret",  # pragma: allowlist secret - encrypted replay fixture
            "count": 7,
        },
    )

    pointer = capture_replay_snapshot(
        artifact_plane=plane,
        project_id="project-7",
        job_id="job-9",
        snapshot=snapshot,
    )
    meta = plane.get_artifact(pointer["artifact_id"])
    assert meta is not None
    ciphertext = storage.get(meta["object_key"])
    assert ciphertext is not None
    assert b"runtime-secret" not in ciphertext
    assert "inputs" not in pointer
    assert (
        load_replay_snapshot(
            artifact_plane=plane,
            pointer=pointer,
            project_id="project-7",
            job_id="job-9",
        )
        == snapshot
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        {key: value for key, value in _complete_snapshot().items() if key != "input_contract"},
        {**_complete_snapshot(), "unknown": True},
        _complete_snapshot(input_contract="legacy_envelope"),
        _complete_snapshot(task_envelope=[]),
        _complete_snapshot(_workflow_secret_ref_fingerprints=None),
        _complete_snapshot(
            inputs={"invoice_id": "INV-42"},
            task_envelope={"inputs": {"invoice_id": "INV-OTHER"}, "files": []},
        ),
    ],
)
def test_replay_snapshot_requires_complete_supported_consistent_schema(snapshot) -> None:
    plane = ArtifactDatasetPlane()

    with pytest.raises(JobReplaySnapshotError) as exc_info:
        capture_replay_snapshot(
            artifact_plane=plane,
            project_id="project-7",
            job_id="job-9",
            snapshot=snapshot,
        )

    assert exc_info.value.code == "job.replay_snapshot_partial"
    assert plane._artifacts == {}


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("artifact_id", 17, "job.replay_snapshot_partial"),
        ("size_bytes", "not-an-int", "job.replay_snapshot_partial"),
        ("sha256", "z" * 64, "job.replay_snapshot_partial"),
        ("artifact_sha256", "A" * 64, "job.replay_snapshot_partial"),
        ("expires_at", "tomorrow", "job.replay_snapshot_partial"),
        ("version", True, "job.replay_snapshot_partial"),
        ("expires_at", 1.0, "job.replay_snapshot_expired"),
    ],
)
def test_replay_pointer_rejects_invalid_primitives_before_storage_access(
    field, value, expected_code
) -> None:
    plane = ArtifactDatasetPlane()
    pointer = capture_replay_snapshot(
        artifact_plane=plane,
        project_id="project-7",
        job_id="job-9",
        snapshot=_complete_snapshot(),
    )
    plane.get_artifact = MagicMock(wraps=plane.get_artifact)

    with pytest.raises(JobReplaySnapshotError) as exc_info:
        load_replay_snapshot(
            artifact_plane=plane,
            pointer={**pointer, field: value},
            project_id="project-7",
            job_id="job-9",
        )

    assert exc_info.value.code == expected_code
    plane.get_artifact.assert_not_called()


def test_replay_snapshot_rejects_ciphertext_larger_than_loader_limit_before_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.jobs import job_invocation_artifacts as artifacts

    plane = ArtifactDatasetPlane()
    monkeypatch.setattr(artifacts, "INVOCATION_ARTIFACT_READ_MAX_BYTES", 256)

    _raises_input_214_1 = _complete_snapshot(inputs={"message": "x" * 1000})
    with pytest.raises(JobReplaySnapshotError) as exc_info:
        capture_replay_snapshot(
            artifact_plane=plane,
            project_id="project-7",
            job_id="job-9",
            snapshot=_raises_input_214_1,
        )

    assert exc_info.value.code == "job.replay_snapshot_too_large"
    assert plane._artifacts == {}


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("absent", "job.replay_snapshot_absent"),
        ("partial", "job.replay_snapshot_partial"),
        ("expired", "job.replay_snapshot_expired"),
        ("scope", "job.replay_snapshot_scope_mismatch"),
        ("digest", "job.replay_snapshot_digest_mismatch"),
    ],
)
def test_replay_snapshot_fails_closed_with_stable_codes(
    mutation: str,
    expected_code: str,
) -> None:
    plane = ArtifactDatasetPlane()
    pointer = capture_replay_snapshot(
        artifact_plane=plane,
        project_id="project-7",
        job_id="job-9",
        snapshot=_complete_snapshot(),
    )
    project_id = "project-7"
    if mutation == "absent":
        pointer = {}
    elif mutation == "partial":
        pointer = {"kind": "job_replay_snapshot", "version": 1}
    elif mutation == "expired":
        plane._artifacts[pointer["artifact_id"]]["expires_at"] = time.time() - 1
    elif mutation == "scope":
        project_id = "project-other"
    elif mutation == "digest":
        pointer = {**pointer, "sha256": "0" * 64}

    with pytest.raises(JobReplaySnapshotError) as exc_info:
        load_replay_snapshot(
            artifact_plane=plane,
            pointer=pointer,
            project_id=project_id,
            job_id="job-9",
        )

    assert exc_info.value.code == expected_code
    assert "runtime-secret" not in str(exc_info.value)


def test_replay_snapshot_rejects_referenced_artifact_digest_mismatch() -> None:
    plane = ArtifactDatasetPlane()
    handle, file_meta = plane.create_artifact(
        owner_scope={"project_id": "project-7", "job_id": "job-9"},
        run_id="job-9",
        step_id="input",
        filename="input.csv",
        content_type="text/csv",
        retention_class="audit",
        body=b"a,b\n1,2\n",
    )
    pointer = capture_replay_snapshot(
        artifact_plane=plane,
        project_id="project-7",
        job_id="job-9",
        snapshot=_complete_snapshot(
            files=[
                {
                    "artifact_id": handle.artifact_id,
                    "sha256": file_meta["sha256"],
                    "size_bytes": file_meta["size_bytes"],
                    "owner_project_id": "project-7",
                    "owner_job_id": "job-9",
                }
            ],
        ),
    )
    plane._artifacts[handle.artifact_id]["sha256"] = "f" * 64

    with pytest.raises(JobReplaySnapshotError) as exc_info:
        load_replay_snapshot(
            artifact_plane=plane,
            pointer=pointer,
            project_id="project-7",
            job_id="job-9",
        )

    assert exc_info.value.code == "job.replay_snapshot_digest_mismatch"


def test_mint_replay_file_handles_binds_pre_staged_artifact_to_created_job() -> None:
    plane = ArtifactDatasetPlane()
    handle, meta = plane.create_artifact(
        owner_scope={"entry_path": "webhook", "entry_key": "invoice-created"},
        run_id="request-1",
        step_id="workflow_entrypoint",
        filename="invoice.csv",
        content_type="text/csv",
        body=b"id,total\n1,42\n",
    )

    rows = mint_replay_file_handles(
        artifact_plane=plane,
        project_id="project-7",
        job_id="job-9",
        files=[
            {
                "artifact_id": handle.artifact_id,
                "filename": "invoice.csv",
                "content_type": "text/csv",
            }
        ],
    )

    assert rows == [
        {
            "artifact_id": handle.artifact_id,
            "sha256": meta["sha256"],
            "size_bytes": meta["size_bytes"],
            "owner_project_id": "project-7",
            "owner_job_id": "job-9",
            "filename": "invoice.csv",
            "content_type": "text/csv",
        }
    ]
    plane._artifacts.clear()
    rebound = plane.get_artifact(handle.artifact_id)
    assert rebound is not None
    assert rebound["owner_scope"] == {
        "entry_path": "webhook",
        "entry_key": "invoice-created",
        "project_id": "project-7",
        "job_id": "job-9",
    }


def test_replay_snapshot_round_trips_bounded_host_secret_fingerprints() -> None:
    plane = ArtifactDatasetPlane()
    fingerprints = ["a" * 64, "b" * 64]
    pointer = capture_replay_snapshot(
        artifact_plane=plane,
        project_id="project-7",
        job_id="job-9",
        snapshot=_complete_snapshot(_workflow_secret_ref_fingerprints=fingerprints),
    )

    loaded = load_replay_snapshot(
        artifact_plane=plane,
        pointer=pointer,
        project_id="project-7",
        job_id="job-9",
    )

    assert loaded["_workflow_secret_ref_fingerprints"] == fingerprints


@pytest.mark.parametrize(
    "files",
    [
        [{"path": "/tmp/mutable.csv"}],
        [
            {
                "artifact_id": "art-1",
                "dataset_id": "dst-1",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "owner_project_id": "project-7",
                "owner_job_id": "job-9",
            }
        ],
        [
            {
                "artifact_id": "art-1",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "owner_project_id": "project-7",
                "owner_job_id": "job-9",
                "object_key": "artifacts/art-1/input.csv",
            }
        ],
    ],
)
def test_replay_snapshot_rejects_mutable_mixed_or_unknown_file_rows(files) -> None:
    _raises_input_404_1 = ArtifactDatasetPlane()
    _raises_input_404_2 = _complete_snapshot(files=files)
    with pytest.raises(JobReplaySnapshotError) as exc_info:
        capture_replay_snapshot(
            artifact_plane=_raises_input_404_1,
            project_id="project-7",
            job_id="job-9",
            snapshot=_raises_input_404_2,
        )

    assert exc_info.value.code == "job.replay_snapshot_partial"


def test_replay_snapshot_validates_dataset_digest_size_and_owner() -> None:
    plane = ArtifactDatasetPlane()
    handle, meta = plane.create_dataset(
        owner_scope={"project_id": "project-7", "job_id": "job-9"},
        run_id="job-9",
        step_id="input",
        rows=[{"id": 1}],
    )
    pointer = capture_replay_snapshot(
        artifact_plane=plane,
        project_id="project-7",
        job_id="job-9",
        snapshot=_complete_snapshot(
            files=[
                {
                    "dataset_id": handle.dataset_id,
                    "sha256": meta["sha256"],
                    "size_bytes": meta["size_bytes"],
                    "owner_project_id": "project-7",
                    "owner_job_id": "job-9",
                }
            ],
        ),
    )

    plane._datasets[handle.dataset_id]["size_bytes"] += 1
    with pytest.raises(JobReplaySnapshotError) as exc_info:
        load_replay_snapshot(
            artifact_plane=plane,
            pointer=pointer,
            project_id="project-7",
            job_id="job-9",
        )

    assert exc_info.value.code == "job.replay_snapshot_digest_mismatch"


@pytest.mark.parametrize("fingerprints", [["forged"], ["a" * 64] * 65])
def test_replay_snapshot_rejects_invalid_or_unbounded_secret_fingerprints(fingerprints) -> None:
    _raises_input_454_1 = ArtifactDatasetPlane()
    _raises_input_454_2 = _complete_snapshot(_workflow_secret_ref_fingerprints=fingerprints)
    with pytest.raises(JobReplaySnapshotError) as exc_info:
        capture_replay_snapshot(
            artifact_plane=_raises_input_454_1,
            project_id="project-7",
            job_id="job-9",
            snapshot=_raises_input_454_2,
        )

    assert exc_info.value.code == "job.replay_snapshot_partial"
