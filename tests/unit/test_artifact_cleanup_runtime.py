from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.jobs import artifact_cleanup_recovery as cleanup_recovery
from core.application.jobs import artifact_cleanup_runtime as cleanup


class _ReconciliationCheckpointStore:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state if state is not None else {}

    def load(self) -> dict:
        return dict(self.state)

    def save(self, checkpoint: dict) -> None:
        self.state.clear()
        self.state.update(checkpoint)


def _checkpoint_for_marker(marker_key: str) -> _ReconciliationCheckpointStore:
    return _ReconciliationCheckpointStore(
        {"version": 1, "shard": int(marker_key.split("/")[1], 16), "after_key": ""}
    )


def _put_legacy_cleanup_marker(storage, *, project_id: str, job_id: str) -> str:
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key

    marker_key = replay_cleanup_completion_key(project_id, job_id)
    storage.put(
        marker_key,
        json.dumps(
            {
                "version": 2,
                "state": "complete",
                "project_id": project_id,
                "job_id": job_id,
                "db_transition": cleanup.JOB_DELETED_TRANSITION,
                "authority_fingerprint": "a" * 64,
                "plan_fingerprint": cleanup._cleanup_plan_fingerprint([]),
                "objects": [],
                "prepared_at": 1.0,
                "completed_at": 2.0,
            }
        ).encode(),
        content_type="application/json",
    )
    return marker_key


class _Storage:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.objects: dict[str, bytes] = {
            "artifacts/_metadata/art-1.json": (
                b'{"artifact_id":"art-1","object_key":"artifacts/art-1/invoice.pdf",'
                b'"owner_scope":{"project_id":"project-1","job_id":"job-1"}}'
            )
        }

    def delete(self, key: str) -> bool:
        self.deleted.append(str(key))
        self.objects.pop(str(key), None)
        return True

    def get(self, key: str):
        return self.objects.get(str(key))

    def put(self, key: str, body: bytes, **_kwargs):
        payload = bytes(body)
        self.objects[str(key)] = payload
        return {"key": str(key), "size_bytes": len(payload)}

    def stat(self, key: str):
        body = self.objects.get(str(key))
        return None if body is None else {"key": str(key), "size_bytes": len(body)}

    def list_key_page(self, _prefix: str, **_kwargs):
        return [], None


class _OutboxRepositoryDouble:
    def __init__(self, bl) -> None:
        self.bl = bl

    def claim_expired_preparing(self, **kwargs):
        return self.bl.claim_expired_preparing_job_dispatches(**kwargs)

    def verify_cleanup_owner(self, *, job_id, owner_token):
        return self.bl.verify_job_dispatch_cleanup_owner(job_id, owner_token=owner_token)

    def settle_cleanup(self, *, job_id, **kwargs):
        return self.bl.settle_job_dispatch_cleanup(job_id, **kwargs)


def test_collect_storage_keys_from_job_row_includes_blob_and_sidecar() -> None:
    job_row = {
        "meta": {
            "files": [
                {
                    "artifact_id": "art_123",
                    "object_key": "artifacts/art_123/blob",
                }
            ]
        }
    }

    keys = cleanup.collect_storage_keys_from_job_row(job_row)

    assert keys == [
        "artifacts/art_123/blob",
        "_replay_owners/artifact/art_123.json",
        "artifacts/_metadata/art_123.json",
    ]


def test_purge_job_artifacts_includes_replay_snapshot_pointer() -> None:
    class _SidecarStorage(_Storage):
        def __init__(self) -> None:
            super().__init__()
            self.objects = {
                "artifacts/_metadata/art_replay.json": (
                    b'{"artifact_id":"art_replay",'
                    b'"object_key":"artifacts/art_replay/replay-snapshot.enc"}'
                ),
                "artifacts/art_replay/replay-snapshot.enc": b"ciphertext",
            }

        def get(self, key: str):
            return self.objects.get(str(key))

        def delete(self, key: str) -> bool:
            self.deleted.append(str(key))
            return self.objects.pop(str(key), None) is not None

        def stat(self, key: str):
            body = self.objects.get(str(key))
            return None if body is None else {"key": str(key), "size_bytes": len(body)}

    storage = _SidecarStorage()
    job_row = {
        "meta": {
            "replay_snapshot": {
                "kind": "job_replay_snapshot",
                "artifact_id": "art_replay",
            }
        }
    }

    stats = cleanup.purge_job_artifacts(job_row, storage=storage)

    assert stats == {"attempted": 3, "deleted": 3, "failed": 0}
    assert storage.deleted == [
        "artifacts/art_replay/replay-snapshot.enc",
        "_replay_owners/artifact/art_replay.json",
        "artifacts/_metadata/art_replay.json",
    ]
    assert storage.objects == {}


def test_collect_storage_keys_handles_json_meta_and_deduplicates() -> None:
    job_row = {
        "meta": (
            '{"files":[{"artifact_id":"art_a","object_key":"artifacts/art_a/blob"},'
            '{"artifact_id":"art_a","object_key":"artifacts/art_a/blob"}]}'
        )
    }

    keys = cleanup.collect_storage_keys_from_job_row(job_row)

    assert keys == [
        "artifacts/art_a/blob",
        "_replay_owners/artifact/art_a.json",
        "artifacts/_metadata/art_a.json",
    ]


def test_collect_storage_keys_includes_pending_attachment_journal() -> None:
    job_row = {
        "meta": {
            "files": [{"artifact_id": "art_complete"}],
            "attachment_persistence_artifacts": [
                {
                    "artifact_id": "art_pending",
                    "object_key": "artifacts/art_pending/invoice.pdf",
                }
            ],
        }
    }

    keys = cleanup.collect_storage_keys_from_job_row(job_row)

    assert keys == [
        "_replay_owners/artifact/art_complete.json",
        "artifacts/_metadata/art_complete.json",
        "artifacts/art_pending/invoice.pdf",
        "_replay_owners/artifact/art_pending.json",
        "artifacts/_metadata/art_pending.json",
    ]


def test_collect_storage_keys_includes_finalized_workflow_output_artifacts() -> None:
    job_row = {
        "meta": {
            "files": [],
            "workflow_outputs": {
                "artifacts": {
                    "catalog_artifact": {
                        "artifact_id": "art_generated",
                        "artifact_handle": "artifact:art_generated",
                    }
                }
            },
        }
    }

    keys = cleanup.collect_storage_keys_from_job_row(job_row)

    assert keys == [
        "_replay_owners/artifact/art_generated.json",
        "artifacts/_metadata/art_generated.json",
    ]


def test_purge_job_artifacts_deletes_all_collected_keys() -> None:
    storage = _Storage()
    job_row = {
        "meta": {
            "files": [
                {"artifact_id": "art_1", "object_key": "artifacts/art_1/blob"},
                {"artifact_id": "art_2", "object_key": "artifacts/art_2/blob"},
            ]
        }
    }

    stats = cleanup.purge_job_artifacts(job_row, storage=storage)

    assert stats == {"attempted": 6, "deleted": 6, "failed": 0}
    assert storage.deleted == [
        "artifacts/art_1/blob",
        "_replay_owners/artifact/art_1.json",
        "artifacts/art_2/blob",
        "_replay_owners/artifact/art_2.json",
        "artifacts/_metadata/art_1.json",
        "artifacts/_metadata/art_2.json",
    ]


def test_purge_job_artifacts_resolves_generated_blob_key_from_metadata_sidecar() -> None:
    class _SidecarStorage(_Storage):
        def __init__(self) -> None:
            super().__init__()
            self.objects = {
                "artifacts/_metadata/art_generated.json": (
                    b'{"artifact_id":"art_generated",'
                    b'"object_key":"artifacts/art_generated/catalog.csv"}'
                ),
                "artifacts/art_generated/catalog.csv": b"catalog-bytes",
            }

        def get(self, key: str):
            return self.objects.get(key)

        def delete(self, key: str) -> bool:
            self.deleted.append(str(key))
            return self.objects.pop(str(key), None) is not None

        def stat(self, key: str):
            body = self.objects.get(str(key))
            return None if body is None else {"key": str(key), "size_bytes": len(body)}

    storage = _SidecarStorage()
    job_row = {
        "meta": {
            "workflow_outputs": {
                "artifacts": {"catalog_artifact": {"artifact_id": "art_generated"}}
            }
        }
    }

    stats = cleanup.purge_job_artifacts(job_row, storage=storage)

    assert stats == {"attempted": 3, "deleted": 3, "failed": 0}
    assert storage.deleted == [
        "artifacts/art_generated/catalog.csv",
        "_replay_owners/artifact/art_generated.json",
        "artifacts/_metadata/art_generated.json",
    ]
    assert storage.objects == {}


def test_purge_job_artifacts_no_files_is_noop() -> None:
    storage = _Storage()
    stats = cleanup.purge_job_artifacts({"meta": {}}, storage=storage)
    assert stats == {"attempted": 0, "deleted": 0, "failed": 0}
    assert storage.deleted == []


def test_purge_job_artifacts_rejects_cross_owner_reference() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-other"},
        run_id="job-other",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"input",
    )

    stats = cleanup.purge_job_artifacts(
        {
            "id": "job-1",
            "project_id": "project-1",
            "meta": {"files": [{"artifact_id": handle.artifact_id}]},
        },
        storage=storage,
    )

    assert stats["failed"] == 1
    assert storage.stat(str(meta["object_key"])) is not None
    assert storage.stat(f"artifacts/_metadata/{handle.artifact_id}.json") is not None


def test_purge_job_artifacts_rejects_untyped_foreign_object_key() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    victim, victim_meta = plane.create_artifact(
        owner_scope={"project_id": "p-other", "job_id": "j-other"},
        run_id="j-other",
        step_id="upload",
        filename="blob.bin",
        content_type="application/octet-stream",
        body=b"victim",
    )

    stats = cleanup.purge_job_artifacts(
        {
            "id": "j-delete",
            "project_id": "p-delete",
            "meta": {"files": [{"object_key": victim_meta["object_key"]}]},
        },
        storage=storage,
    )

    assert stats["failed"] == 1
    assert storage.stat(str(victim_meta["object_key"])) is not None
    assert storage.stat(f"artifacts/_metadata/{victim.artifact_id}.json") is not None


def test_purge_job_artifacts_uses_only_selected_owner_manifest_prefix() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_owner_manifest_prefix
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class TrackingStorage(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.listed_prefixes: list[str] = []
            self.page_limits: list[int] = []
            self.read_keys: list[str] = []

        def list_keys(self, _prefix: str):
            raise AssertionError("per-job cleanup must not scan a global prefix")

        def list_key_page(self, prefix: str, **kwargs):
            self.listed_prefixes.append(prefix)
            self.page_limits.append(int(kwargs["limit"]))
            return super().list_key_page(prefix, **kwargs)

        def get(self, key: str):
            self.read_keys.append(key)
            return super().get(key)

    storage = TrackingStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    foreign_meta = None
    for index in range(200):
        _foreign, foreign_meta = plane.create_artifact(
            artifact_id=f"art_foreign_{index:04d}",
            owner_scope={"project_id": "foreign-project", "job_id": f"foreign-{index}"},
            run_id=f"foreign-{index}",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            body=b"foreign",
        )
    owned_rows = [
        plane.create_artifact(
            artifact_id=f"art_owned_{index:04d}",
            owner_scope={"project_id": "project-1", "job_id": "job-1"},
            run_id="job-1",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            body=b"owned",
        )
        for index in range(140)
    ]
    assert foreign_meta is not None
    storage.put(
        "artifacts/_metadata/art_foreign_0000.json",
        b"not-json",
        content_type="application/json",
    )
    storage.listed_prefixes.clear()
    storage.read_keys.clear()

    stats = cleanup.purge_job_artifacts(
        {"id": "job-1", "project_id": "project-1", "meta": {}},
        storage=storage,
    )

    expected_prefix = replay_owner_manifest_prefix("project-1", "job-1")
    assert stats["failed"] == 0
    assert storage.listed_prefixes[0] == expected_prefix
    assert storage.listed_prefixes.count(expected_prefix) == 2
    assert all(
        prefix == expected_prefix or prefix.startswith("artifacts/art_owned_")
        for prefix in storage.listed_prefixes
    )
    assert max(storage.page_limits) <= 128
    assert not any(key.startswith("artifacts/_metadata/art_foreign") for key in storage.read_keys)
    for owned, owned_meta in owned_rows:
        assert storage.stat(str(owned_meta["object_key"])) is None
        assert storage.stat(f"artifacts/_metadata/{owned.artifact_id}.json") is None
    assert storage.stat(str(foreign_meta["object_key"])) is not None


def test_purge_job_artifacts_fails_closed_for_corrupt_owned_manifest() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_owner_manifest_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    owned, owned_meta = plane.create_artifact(
        artifact_id="art_owned",
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    manifest_key = replay_owner_manifest_key(
        project_id="project-1",
        job_id="job-1",
        kind="artifact",
        object_id=owned.artifact_id,
    )
    storage.put(manifest_key, b"not-json", content_type="application/json")

    stats = cleanup.purge_job_artifacts(
        {"id": "job-1", "project_id": "project-1", "meta": {}},
        storage=storage,
    )

    assert stats["failed"] == 1
    assert storage.stat(str(owned_meta["object_key"])) is not None
    assert storage.stat(manifest_key) is not None


def test_purge_job_artifacts_fails_closed_for_corrupt_cleanup_completion_marker() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    storage.put(
        replay_cleanup_completion_key("project-1", "job-1"),
        b"not-json",
        content_type="application/json",
    )

    stats = cleanup.purge_job_artifacts(
        {
            "id": "job-1",
            "project_id": "project-1",
            "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
        },
        storage=storage,
    )

    assert stats == {"attempted": 0, "deleted": 0, "failed": 1}
    assert storage.stat(str(artifact_meta["object_key"])) is not None


def test_legacy_cleanup_journal_rejects_forged_cross_object_key() -> None:
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    refs = [{"artifact_id": "art-owned", "object_key": "artifacts/art-foreign/blob"}]
    fingerprint = cleanup._cleanup_completion_fingerprint(
        project_id="project-1", job_id="job-1", refs=refs
    )
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    storage.put("artifacts/art-owned/late", b"owned", content_type="application/octet-stream")
    storage.put("artifacts/art-foreign/blob", b"foreign", content_type="application/octet-stream")
    storage.put(
        marker_key,
        json.dumps(
            {
                "version": 1,
                "project_id": "project-1",
                "job_id": "job-1",
                "authority_fingerprint": fingerprint,
                "completed_at": 1.0,
                "expires_at": 2.0,
            }
        ).encode(),
        content_type="application/json",
    )

    result = cleanup.purge_job_artifacts(
        {"id": "job-1", "project_id": "project-1", "meta": {"files": refs}},
        storage=storage,
    )

    assert result == {"attempted": 0, "deleted": 0, "failed": 1}
    assert storage.get("artifacts/art-owned/late") == b"owned"
    assert storage.get("artifacts/art-foreign/blob") == b"foreign"
    assert storage.stat(marker_key) is not None


@pytest.mark.parametrize("kind", ["artifact", "dataset"])
def test_legacy_cleanup_journal_rejects_consistent_cross_owner_reference(kind: str) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import (
        replay_cleanup_completion_key,
        replay_owner_manifest_key,
    )
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    if kind == "artifact":
        handle, meta = plane.create_artifact(
            owner_scope={"project_id": "victim-project", "job_id": "victim-job"},
            run_id="victim-job",
            step_id="upload",
            filename="secret.txt",
            content_type="text/plain",
            body=b"victim-artifact",
        )
        object_id = handle.artifact_id
        metadata_key = f"artifacts/_metadata/{object_id}.json"
    else:
        handle, meta = plane.create_dataset(
            owner_scope={"project_id": "victim-project", "job_id": "victim-job"},
            run_id="victim-job",
            step_id="rows",
            rows=[{"secret": "victim-dataset"}],  # pragma: allowlist secret - ownership fixture
        )
        object_id = handle.dataset_id
        metadata_key = f"datasets/{object_id}/meta.json"
    object_key = str(meta["object_key"])
    refs = [{f"{kind}_id": object_id, "object_key": object_key}]
    fingerprint = cleanup._cleanup_completion_fingerprint(
        project_id="attacker-project", job_id="attacker-job", refs=refs
    )
    marker_key = replay_cleanup_completion_key("attacker-project", "attacker-job")
    victim_manifest_key = replay_owner_manifest_key(
        project_id="victim-project",
        job_id="victim-job",
        kind=kind,
        object_id=object_id,
    )
    storage.put(
        marker_key,
        json.dumps(
            {
                "version": 1,
                "project_id": "attacker-project",
                "job_id": "attacker-job",
                "authority_fingerprint": fingerprint,
                "completed_at": 1.0,
                "expires_at": 2.0,
            }
        ).encode(),
        content_type="application/json",
    )

    result = cleanup.purge_job_artifacts(
        {
            "id": "attacker-job",
            "project_id": "attacker-project",
            "meta": {"files": refs},
        },
        storage=storage,
    )

    assert result == {"attempted": 0, "deleted": 0, "failed": 1}
    assert storage.stat(object_key) is not None
    assert storage.stat(metadata_key) is not None
    assert storage.stat(victim_manifest_key) is not None
    assert storage.stat(marker_key) is not None


@pytest.mark.parametrize("kind", ["artifact", "dataset"])
@pytest.mark.parametrize("sidecar_body", [None, b"not-json"])
def test_legacy_cleanup_journal_fails_closed_without_valid_sidecar(
    kind: str, sidecar_body: bytes | None
) -> None:
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    object_id = "legacy-object"
    object_key = (
        f"artifacts/{object_id}/blob" if kind == "artifact" else f"datasets/{object_id}/blob"
    )
    metadata_key = (
        f"artifacts/_metadata/{object_id}.json"
        if kind == "artifact"
        else f"datasets/{object_id}/meta.json"
    )
    refs = [{f"{kind}_id": object_id, "object_key": object_key}]
    fingerprint = cleanup._cleanup_completion_fingerprint(
        project_id="project-1", job_id="job-1", refs=refs
    )
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    storage.put(object_key, b"must-survive", content_type="application/octet-stream")
    if sidecar_body is not None:
        storage.put(metadata_key, sidecar_body, content_type="application/json")
    storage.put(
        marker_key,
        json.dumps(
            {
                "version": 1,
                "project_id": "project-1",
                "job_id": "job-1",
                "authority_fingerprint": fingerprint,
                "completed_at": 1.0,
                "expires_at": 2.0,
            }
        ).encode(),
        content_type="application/json",
    )

    result = cleanup.purge_job_artifacts(
        {"id": "job-1", "project_id": "project-1", "meta": {"files": refs}},
        storage=storage,
    )

    assert result == {"attempted": 0, "deleted": 0, "failed": 1}
    assert storage.stat(object_key) is not None
    if sidecar_body is not None:
        assert storage.get(metadata_key) == sidecar_body
    assert storage.stat(marker_key) is not None


def test_valid_legacy_cleanup_journal_upgrades_to_self_contained_v2_plan() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, meta = plane.create_artifact(
        artifact_id="art-owned",
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="original.txt",
        content_type="text/plain",
        body=b"original",
    )
    refs = [{"artifact_id": artifact.artifact_id, "object_key": meta["object_key"]}]
    fingerprint = cleanup._cleanup_completion_fingerprint(
        project_id="project-1", job_id="job-1", refs=refs
    )
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    storage.put("artifacts/art-owned/late", b"owned", content_type="application/octet-stream")
    storage.put(
        marker_key,
        json.dumps(
            {
                "version": 1,
                "project_id": "project-1",
                "job_id": "job-1",
                "authority_fingerprint": fingerprint,
                "completed_at": 1.0,
                "expires_at": 2.0,
            }
        ).encode(),
        content_type="application/json",
    )

    result = cleanup.purge_job_artifacts(
        {"id": "job-1", "project_id": "project-1", "meta": {"files": refs}},
        storage=storage,
    )

    marker = json.loads(storage.get(marker_key))
    assert result["failed"] == 0
    assert storage.stat("artifacts/art-owned/late") is None
    assert marker["version"] == 2
    assert marker["state"] == "complete"
    assert marker["objects"][0]["object_prefix"] == "artifacts/art-owned/"


def test_committed_cleanup_tombstone_repurgates_late_put_after_grant_ttl() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
    }
    assert cleanup.purge_job_artifacts(job, storage=storage)["failed"] == 0
    assert cleanup.finalize_job_artifact_purge(job, storage=storage, now=100.0)
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    marker = json.loads(storage.get(marker_key))
    storage.put(str(artifact_meta["object_key"]), b"late", content_type="text/plain")
    shard = int(marker_key.split("/")[1], 16)
    bl = MagicMock()

    before_expiry = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl,
        storage=storage,
        budget=1,
        checkpoint_store=_ReconciliationCheckpointStore(
            {"version": 1, "shard": shard, "after_key": ""}
        ),
        now=float(marker["remove_after"]) - 1,
    )
    after_expiry = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl,
        storage=storage,
        budget=1,
        checkpoint_store=_ReconciliationCheckpointStore(
            {"version": 1, "shard": shard, "after_key": ""}
        ),
        now=float(marker["remove_after"]) + 1,
    )

    assert before_expiry == {"scanned": 1, "reconciled": 0, "preserved": 1, "failed": 0}
    assert after_expiry == {"scanned": 1, "reconciled": 1, "preserved": 0, "failed": 0}
    assert storage.stat(str(artifact_meta["object_key"])) is None
    assert storage.stat(marker_key) is None
    bl.get_job.assert_not_called()


@pytest.mark.parametrize("has_artifact", [False, True], ids=["empty-plan", "owned-artifact"])
def test_committed_tombstone_uses_storage_ingress_grant_ceiling(has_artifact: bool) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import (
        ARTIFACT_INGRESS_GRANT_MAX_TTL_SECONDS,
        InMemoryObjectStorage,
    )

    storage = InMemoryObjectStorage()
    files = []
    if has_artifact:
        plane = ArtifactDatasetPlane(storage=storage)
        artifact, _meta = plane.create_artifact(
            owner_scope={"project_id": "project-1", "job_id": "job-1"},
            run_id="job-1",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            body=b"owned",
        )
        files.append({"artifact_id": artifact.artifact_id})
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"files": files},
    }
    assert cleanup.purge_job_artifacts(job, storage=storage)["failed"] == 0
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    completed = json.loads(storage.get(marker_key))
    assert completed["state"] == "complete"
    assert len(completed["objects"]) == int(has_artifact)
    assert cleanup.finalize_job_artifact_purge(job, storage=storage, now=100.0)

    marker = json.loads(storage.get(marker_key))
    assert marker["state"] == "committed"
    assert marker["remove_after"] == 100.0 + ARTIFACT_INGRESS_GRANT_MAX_TTL_SECONDS
    bl = MagicMock()
    for now, expected in (
        (marker["remove_after"] - 1, {"scanned": 1, "reconciled": 0, "preserved": 1, "failed": 0}),
        (marker["remove_after"] + 1, {"scanned": 1, "reconciled": 1, "preserved": 0, "failed": 0}),
    ):
        assert (
            cleanup_recovery.reconcile_job_artifact_purge_markers(
                bl,
                storage=storage,
                budget=1,
                checkpoint_store=_checkpoint_for_marker(marker_key),
                now=now,
            )
            == expected
        )
    assert storage.get(marker_key) is None
    bl.get_job.assert_not_called()


def test_reconciler_budget_one_reads_one_bounded_page_for_ten_thousand_markers() -> None:
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class CountingStorage(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.page_calls = 0
            self.keys_returned = 0

        def list_key_page_after(self, prefix, *, limit, start_after=None):
            self.page_calls += 1
            keys, next_key = super().list_key_page_after(
                prefix, limit=limit, start_after=start_after
            )
            self.keys_returned += len(keys)
            return keys, next_key

    storage = CountingStorage()
    marker_shard = None
    for index in range(10_000):
        key = _put_legacy_cleanup_marker(
            storage, project_id=f"project-{index}", job_id=f"job-{index}"
        )
        marker_shard = marker_shard if marker_shard is not None else int(key.split("/")[1], 16)
    assert marker_shard is not None
    bl = MagicMock()
    bl.get_job.return_value = {"id": "present", "project_id": "present", "status": "failed"}

    result = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl,
        storage=storage,
        budget=1,
        checkpoint_store=_ReconciliationCheckpointStore(
            {"version": 1, "shard": marker_shard, "after_key": ""}
        ),
    )

    assert result["scanned"] == 1
    assert storage.page_calls == 1
    assert storage.keys_returned == 1
    assert bl.get_job.call_count == 1


def test_purge_job_artifacts_missing_sidecar_without_completion_proof_fails_closed() -> None:
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()

    stats = cleanup.purge_job_artifacts(
        {
            "id": "job-1",
            "project_id": "project-1",
            "meta": {"files": [{"artifact_id": "art-missing"}]},
        },
        storage=storage,
    )

    assert stats == {"attempted": 0, "deleted": 0, "failed": 1}


def test_purge_job_artifacts_completion_marker_rejects_new_owned_manifest() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    first_artifact, _first_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="first.txt",
        content_type="text/plain",
        body=b"first",
    )
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"files": [{"artifact_id": first_artifact.artifact_id}]},
    }
    assert cleanup.purge_job_artifacts(job, storage=storage)["failed"] == 0
    _second_artifact, second_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="late-upload",
        filename="second.txt",
        content_type="text/plain",
        body=b"second",
    )

    retry = cleanup.purge_job_artifacts(job, storage=storage)

    assert retry == {"attempted": 0, "deleted": 0, "failed": 1}
    assert storage.stat(str(second_meta["object_key"])) is not None


def test_cleanup_completion_marker_survives_ttl_and_allows_late_retry() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, _artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
    }
    assert cleanup.purge_job_artifacts(job, storage=storage)["failed"] == 0
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    marker = json.loads(storage.get(marker_key) or b"{}")

    sweep = ArtifactDatasetPlane(storage=storage).cleanup_expired(
        now=float(marker["completed_at"]) + (8 * 24 * 60 * 60)
    )
    retry = cleanup.purge_job_artifacts(job, storage=storage)

    assert sweep["cleanup_failed"] == 0
    assert storage.stat(marker_key) is not None
    assert retry == {"attempted": 0, "deleted": 0, "failed": 0}


def test_db_reconciler_removes_stale_marker_after_committed_job_delete() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, _artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
    }
    assert cleanup.purge_job_artifacts(job, storage=storage)["failed"] == 0
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    bl = MagicMock()
    bl.get_job.return_value = None

    result = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl, storage=storage, checkpoint_store=_checkpoint_for_marker(marker_key)
    )

    assert result == {"scanned": 1, "reconciled": 1, "preserved": 0, "failed": 0}
    bl.get_job.assert_called_once_with("job-1")
    marker = json.loads(storage.get(marker_key))
    assert marker["state"] == "committed"
    assert marker["remove_after"] - marker["committed_at"] >= (
        cleanup.ARTIFACT_INGRESS_GRANT_MAX_TTL_SECONDS
    )


def test_db_reconciler_preserves_marker_on_project_mismatch_or_database_error() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, _artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
    }
    assert cleanup.purge_job_artifacts(job, storage=storage)["failed"] == 0
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-other",
        "status": "failed",
        "meta": {},
    }

    mismatch = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl, storage=storage, checkpoint_store=_checkpoint_for_marker(marker_key)
    )
    bl.get_job.side_effect = RuntimeError("database unavailable")
    unavailable = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl, storage=storage, checkpoint_store=_checkpoint_for_marker(marker_key)
    )

    assert mismatch == {"scanned": 1, "reconciled": 0, "preserved": 1, "failed": 0}
    assert unavailable == {"scanned": 1, "reconciled": 0, "preserved": 0, "failed": 1}
    assert storage.stat(marker_key) is not None


def test_db_reconciler_removes_preparing_marker_after_terminalization_commit() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, _artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"attachment_persistence_artifacts": [{"artifact_id": artifact.artifact_id}]},
    }
    assert (
        cleanup.purge_job_artifacts(
            job,
            storage=storage,
            db_transition=cleanup.PREPARING_TERMINALIZED_TRANSITION,
        )["failed"]
        == 0
    )
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        "status": "failed",
        "meta": {"task_envelope": {"payload_complete": False}},
    }

    result = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl, storage=storage, checkpoint_store=_checkpoint_for_marker(marker_key)
    )

    assert result == {"scanned": 1, "reconciled": 1, "preserved": 0, "failed": 0}
    assert json.loads(storage.get(marker_key))["state"] == "committed"


def test_db_reconciler_clamps_one_thousand_markers_to_per_run_budget() -> None:
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    for index in range(1_000):
        _put_legacy_cleanup_marker(
            storage,
            project_id=f"project-{index}",
            job_id=f"job-{index}",
        )
    bl = MagicMock()
    bl.get_job.return_value = {"id": "present", "project_id": "present", "status": "failed"}

    result = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl,
        storage=storage,
        budget=999,
        checkpoint_store=_ReconciliationCheckpointStore(),
    )

    assert result["scanned"] <= cleanup_recovery.ARTIFACT_CLEANUP_RECONCILIATION_MAX_BUDGET
    assert bl.get_job.call_count <= cleanup_recovery.ARTIFACT_CLEANUP_RECONCILIATION_MAX_BUDGET


def test_db_reconciler_repeated_scans_and_restart_eventually_cover_all_markers() -> None:
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    expected_job_ids = {f"job-{index}" for index in range(160)}
    for index, job_id in enumerate(sorted(expected_job_ids)):
        _put_legacy_cleanup_marker(
            storage,
            project_id=f"project-{index}",
            job_id=job_id,
        )
    persisted_checkpoint: dict = {}
    visited: set[str] = set()
    bl = MagicMock()

    def _present(job_id: str):
        visited.add(job_id)
        return {"id": job_id, "project_id": "still-present", "status": "failed"}

    bl.get_job.side_effect = _present
    store = _ReconciliationCheckpointStore(persisted_checkpoint)
    for scan_index in range(80):
        if scan_index == 3:
            store = _ReconciliationCheckpointStore(persisted_checkpoint)
        cleanup_recovery.reconcile_job_artifact_purge_markers(
            bl,
            storage=storage,
            budget=8,
            checkpoint_store=store,
        )
        if visited == expected_job_ids:
            break

    assert visited == expected_job_ids


def test_db_reconciler_advances_past_hot_shard_with_continuation_and_churn() -> None:
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class CountingStorage(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.page_prefixes: list[str] = []

        def list_key_page_after(self, prefix, *, limit, start_after=None):
            self.page_prefixes.append(prefix)
            return super().list_key_page_after(
                prefix,
                limit=limit,
                start_after=start_after,
            )

    def _job_ids_for_shard(shard: int, *, count: int, label: str) -> list[str]:
        matches: list[str] = []
        for index in range(100_000):
            job_id = f"{label}-{index}"
            marker_key = replay_cleanup_completion_key("project-1", job_id)
            if int(marker_key.split("/")[1], 16) == shard:
                matches.append(job_id)
                if len(matches) == count:
                    return matches
        raise AssertionError(f"could not find {count} job IDs for shard {shard:02x}")

    storage = CountingStorage()
    hot_job_ids = _job_ids_for_shard(0, count=5, label="hot")
    cold_job_id = _job_ids_for_shard(1, count=1, label="cold")[0]
    for job_id in [*hot_job_ids, cold_job_id]:
        _put_legacy_cleanup_marker(storage, project_id="project-1", job_id=job_id)
    checkpoint: dict = {"version": 2, "next_shard": 0, "after_keys": {}}
    store = _ReconciliationCheckpointStore(checkpoint)
    visited: list[str] = []
    bl = MagicMock()
    bl.get_job.side_effect = lambda job_id: (
        visited.append(job_id) or {"id": job_id, "project_id": "project-1", "status": "failed"}
    )

    first = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl,
        storage=storage,
        budget=1,
        checkpoint_store=store,
    )
    hot_cursor = str(checkpoint["after_keys"]["00"])
    churn_job_id = _job_ids_for_shard(0, count=1, label="new-hot")[0]
    _put_legacy_cleanup_marker(storage, project_id="project-1", job_id=churn_job_id)
    second = cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl,
        storage=storage,
        budget=1,
        checkpoint_store=store,
    )

    assert first["scanned"] == 1
    assert second["scanned"] == 1
    assert visited[0] in hot_job_ids
    assert visited[1] == cold_job_id
    assert storage.page_prefixes == ["_replay_job_cleanup/00/", "_replay_job_cleanup/01/"]
    assert checkpoint["next_shard"] == 2
    assert checkpoint["after_keys"]["00"] == hot_cursor


def test_db_reconciler_localfs_restart_uses_durable_checkpoint_not_adapter_token(
    tmp_path,
) -> None:
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter

    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    for index in range(20):
        _put_legacy_cleanup_marker(
            storage,
            project_id=f"project-{index}",
            job_id=f"job-{index}",
        )
    persisted_checkpoint: dict = {}
    visited: set[str] = set()
    bl = MagicMock()
    bl.get_job.side_effect = lambda job_id: (
        visited.add(job_id)
        or {
            "id": job_id,
            "project_id": "still-present",
            "status": "failed",
        }
    )

    cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl,
        storage=storage,
        budget=3,
        checkpoint_store=_ReconciliationCheckpointStore(persisted_checkpoint),
    )
    first_visited = set(visited)
    storage.close()
    restarted = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    for _ in range(20):
        cleanup_recovery.reconcile_job_artifact_purge_markers(
            bl,
            storage=restarted,
            budget=3,
            checkpoint_store=_ReconciliationCheckpointStore(persisted_checkpoint),
        )
        if len(visited) >= 6:
            break

    assert first_visited
    assert len(visited) >= 6
    assert first_visited < visited
    restarted.close()


def test_db_reconciler_new_earlier_key_and_malformed_marker_do_not_starve() -> None:
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    project_id = "same-project"
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key

    shard = replay_cleanup_completion_key(project_id, "job-0").split("/")[1]
    candidate_ids = [
        job_id
        for index in range(10_000)
        if (
            (job_id := f"job-{index}")
            and replay_cleanup_completion_key(project_id, job_id).split("/")[1] == shard
        )
    ][:8]
    candidates = sorted(
        (
            _put_legacy_cleanup_marker(storage, project_id=project_id, job_id=job_id),
            job_id,
        )
        for job_id in candidate_ids
    )
    malformed_key = _put_legacy_cleanup_marker(
        storage,
        project_id=project_id,
        job_id="malformed",
    )
    storage.put(malformed_key, b"not-json", content_type="application/json")
    checkpoint: dict = {"version": 1, "shard": int(shard, 16), "after_key": ""}
    store = _ReconciliationCheckpointStore(checkpoint)
    visited: set[str] = set()
    bl = MagicMock()
    bl.get_job.side_effect = lambda job_id: (
        visited.add(job_id)
        or {
            "id": job_id,
            "project_id": project_id,
            "status": "failed",
        }
    )

    cleanup_recovery.reconcile_job_artifact_purge_markers(
        bl,
        storage=storage,
        budget=2,
        checkpoint_store=store,
    )
    after_key = str((checkpoint.get("after_keys") or {}).get(shard) or "")
    earlier = next(
        ((key, job_id) for key, job_id in candidates if key < after_key and job_id not in visited),
        None,
    )
    if earlier is None:
        earlier_job_id = next(
            job_id
            for index in range(10_000)
            if (
                (job_id := f"new-earlier-{index}")
                and replay_cleanup_completion_key(project_id, job_id) < after_key
            )
        )
        earlier_key = _put_legacy_cleanup_marker(
            storage, project_id=project_id, job_id=earlier_job_id
        )
        assert earlier_key < after_key
    else:
        earlier_key, earlier_job_id = earlier

    for _ in range(300):
        cleanup_recovery.reconcile_job_artifact_purge_markers(
            bl,
            storage=storage,
            budget=2,
            checkpoint_store=store,
        )
        if earlier_job_id in visited and {job_id for _key, job_id in candidates}.issubset(visited):
            break

    assert earlier_job_id in visited
    assert {job_id for _key, job_id in candidates}.issubset(visited)


def test_cleanup_journal_resumes_after_manifest_delete_failure() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import (
        replay_cleanup_completion_key,
        replay_owner_manifest_key,
    )
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class _ManifestDeleteFailsOnce(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False

        def delete(self, key: str) -> bool:
            if "_replay_job_manifests/" in key and not self.failed_once:
                self.failed_once = True
                return False
            return super().delete(key)

    storage = _ManifestDeleteFailsOnce()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
    }
    marker_key = replay_cleanup_completion_key("project-1", "job-1")
    manifest_key = replay_owner_manifest_key(
        project_id="project-1",
        job_id="job-1",
        kind="artifact",
        object_id=artifact.artifact_id,
    )

    first = cleanup.purge_job_artifacts(job, storage=storage)
    journal_after_failure = json.loads(storage.get(marker_key) or b"{}")
    second = cleanup.purge_job_artifacts(job, storage=storage)
    journal_after_retry = json.loads(storage.get(marker_key) or b"{}")

    assert first["failed"] == 1
    assert journal_after_failure["state"] == "preparing"
    assert storage.stat(str(meta["object_key"])) is None
    assert storage.stat(f"artifacts/_metadata/{artifact.artifact_id}.json") is None
    assert storage.stat(manifest_key) is None
    assert second["failed"] == 0
    assert journal_after_retry["state"] == "complete"


def test_cleanup_journal_resumes_when_complete_update_fails() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_plane_values import replay_cleanup_completion_key
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class _CompleteWriteFailsOnce(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False

        def put(self, key: str, body: bytes, *, content_type: str):
            payload = json.loads(body) if key.startswith("_replay_job_cleanup/") else {}
            if payload.get("state") == "complete" and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("completion journal unavailable")
            return super().put(key, body, content_type=content_type)

    storage = _CompleteWriteFailsOnce()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, _meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
    }
    marker_key = replay_cleanup_completion_key("project-1", "job-1")

    first = cleanup.purge_job_artifacts(job, storage=storage)
    journal_after_failure = json.loads(storage.get(marker_key) or b"{}")
    second = cleanup.purge_job_artifacts(job, storage=storage)
    journal_after_retry = json.loads(storage.get(marker_key) or b"{}")

    assert first["failed"] == 1
    assert journal_after_failure["state"] == "preparing"
    assert second["failed"] == 0
    assert journal_after_retry["state"] == "complete"


@pytest.mark.parametrize("kind", ["artifact", "dataset"])
def test_complete_cleanup_journal_removes_late_object_body(kind: str) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    if kind == "artifact":
        handle, _meta = plane.create_artifact(
            owner_scope={"project_id": "project-1", "job_id": "job-1"},
            run_id="job-1",
            step_id="upload",
            filename="input.txt",
            content_type="text/plain",
            body=b"owned",
        )
        object_id = handle.artifact_id
        refs = [{"artifact_id": object_id}]
        late_key = f"artifacts/{object_id}/late-presigned-put"
    else:
        handle, _meta = plane.create_dataset(
            owner_scope={"project_id": "project-1", "job_id": "job-1"},
            run_id="job-1",
            step_id="rows",
            rows=[{"id": 1}],
        )
        object_id = handle.dataset_id
        refs = [{"dataset_id": object_id}]
        late_key = f"datasets/{object_id}/late-presigned-put"
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "meta": {"files": refs},
    }
    assert cleanup.purge_job_artifacts(job, storage=storage)["failed"] == 0
    storage.put(late_key, b"late", content_type="application/octet-stream")

    retry = cleanup.purge_job_artifacts(job, storage=storage)

    assert retry["attempted"] >= 1
    assert retry["deleted"] >= 1
    assert retry["failed"] == 0
    assert storage.stat(late_key) is None


def test_purge_storage_keys_counts_delete_false_as_failure_when_object_remains() -> None:
    class _RefusingStorage:
        def delete(self, _key: str) -> bool:
            return False

        def stat(self, key: str):
            return {"key": key, "size_bytes": 1}

    assert cleanup.purge_storage_keys(_RefusingStorage(), ["artifacts/art_1/blob"]) == {
        "attempted": 1,
        "deleted": 0,
        "failed": 1,
    }


def test_purge_job_artifacts_retains_cleanup_sidecar_when_body_delete_fails() -> None:
    class _RefusingBodyStorage:
        def __init__(self) -> None:
            self.objects = {
                "artifacts/art_1/blob": b"body",
                "artifacts/_metadata/art_1.json": b"metadata",
            }

        def delete(self, key: str) -> bool:
            if key == "artifacts/art_1/blob":
                return False
            return self.objects.pop(key, None) is not None

        def stat(self, key: str):
            return {"key": key} if key in self.objects else None

    storage = _RefusingBodyStorage()

    stats = cleanup.purge_job_artifacts(
        {"meta": {"files": [{"artifact_id": "art_1", "object_key": "artifacts/art_1/blob"}]}},
        storage=storage,
    )

    assert stats["failed"] == 1
    assert "artifacts/art_1/blob" in storage.objects
    assert "artifacts/_metadata/art_1.json" in storage.objects


def test_scoped_partial_storage_failure_preserves_preparing_journal() -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class _RefusingBodyStorage(InMemoryObjectStorage):
        def __init__(self) -> None:
            super().__init__()
            self.refused_key = ""

        def delete(self, key: str) -> bool:
            if str(key) == self.refused_key:
                return False
            return super().delete(key)

    storage = _RefusingBodyStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="input.txt",
        content_type="text/plain",
        body=b"owned",
    )
    storage.refused_key = str(artifact_meta["object_key"])

    stats = cleanup.purge_job_artifacts(
        {
            "id": "job-1",
            "project_id": "project-1",
            "meta": {"files": [{"artifact_id": artifact.artifact_id}]},
        },
        storage=storage,
    )

    assert stats["failed"] == 1
    marker_page, marker_token = storage.list_key_page(
        "_replay_job_cleanup/", limit=10, continuation_token=None
    )
    assert len(marker_page) == 1
    assert marker_token is None
    assert json.loads(storage.get(marker_page[0]) or b"{}")["state"] == "preparing"


def test_orphan_scan_uses_bounded_real_adapter_pages() -> None:
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    for index in range(3):
        storage.put(
            f"artifacts/orphan-{index}/body",
            b"orphan",
            content_type="application/octet-stream",
        )

    first = cleanup_recovery.orphan_scan(storage, known_keys=set(), limit=2)
    second = cleanup_recovery.orphan_scan(
        storage,
        known_keys=set(),
        limit=2,
        continuation_token=first["continuation_token"],
    )

    assert len(first["orphan_keys"]) == 2
    assert first["continuation_token"]
    assert len(second["orphan_keys"]) == 1
    assert second["continuation_token"] is None


def test_orphan_scan_pages_local_filesystem_adapter(tmp_path) -> None:
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter

    storage = LocalFilesystemStorageAdapter(
        root_dir=str(tmp_path / "objects"),
        key_prefix="tenant-a",
    )
    for index in range(3):
        storage.put(
            f"artifacts/orphan-{index}/body",
            b"orphan",
            content_type="application/octet-stream",
        )

    first = cleanup_recovery.orphan_scan(storage, known_keys=set(), prefix="artifacts/", limit=2)
    second = cleanup_recovery.orphan_scan(
        storage,
        known_keys=set(),
        prefix="artifacts/",
        limit=2,
        continuation_token=first["continuation_token"],
    )

    assert first["orphan_count"] == 2
    assert second["orphan_count"] == 1
    assert second["continuation_token"] is None


def test_recover_expired_preparing_submission_cleans_and_terminalizes() -> None:
    bl = MagicMock()
    meta = {
        "idempotency": {"scope_key": "scope", "key_digest": "digest"},
        "attachment_persistence_artifacts": [
            {
                "artifact_id": "art-1",
                "object_key": "artifacts/art-1/invoice.pdf",
            }
        ],
        "task_envelope": {"payload_complete": False},
    }
    bl.claim_expired_preparing_job_dispatches.return_value = [
        {"job_id": "job-1", "meta": meta, "owner_token": "cleanup-owner"}
    ]
    bl.verify_job_dispatch_cleanup_owner.return_value = True
    bl.settle_job_dispatch_cleanup.return_value = True
    bl.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        "status": "pending",
        "worker_id": None,
        "meta": meta,
    }
    bl.job_lifecycle_unit_of_work.return_value = SimpleNamespace(
        transaction=lambda: nullcontext(MagicMock())
    )
    storage = _Storage()

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure"
    ) as complete_failure:
        result = cleanup_recovery.recover_expired_preparing_submissions(
            bl,
            storage=storage,
            limit=25,
            outbox_repository=_OutboxRepositoryDouble(bl),
        )

    assert result == {"claimed": 1, "recovered": 1, "cleanup_failed": 0, "superseded": 0}
    complete_failure.assert_called_once()
    assert complete_failure.call_args.kwargs["error_code"] == "submission_preparation_timeout"
    finalized_meta = bl.update_job.call_args.kwargs["meta"]
    assert "attachment_persistence_artifacts" not in finalized_meta
    assert "idempotency" not in finalized_meta
    assert finalized_meta["abandoned_idempotency"]["reason"] == "submission_preparation_timeout"
    bl.settle_job_dispatch_cleanup.assert_called_once_with(
        "job-1",
        owner_token="cleanup-owner",
        status="failed",
        last_error="submission_preparation_timeout",
    )


def test_recover_expired_preparing_submission_retries_when_cleanup_fails() -> None:
    class _RefusingStorage:
        def delete(self, _key: str) -> bool:
            return False

        def stat(self, key: str):
            return {"key": key, "size_bytes": 1}

    bl = MagicMock()
    bl.claim_expired_preparing_job_dispatches.return_value = [
        {
            "job_id": "job-1",
            "owner_token": "cleanup-owner",
            "meta": {
                "attachment_persistence_artifacts": [
                    {"artifact_id": "art-1", "object_key": "artifacts/art-1/invoice.pdf"}
                ]
            },
        }
    ]
    bl.verify_job_dispatch_cleanup_owner.return_value = True

    result = cleanup_recovery.recover_expired_preparing_submissions(
        bl,
        storage=_RefusingStorage(),
        outbox_repository=_OutboxRepositoryDouble(bl),
    )

    assert result == {"claimed": 1, "recovered": 0, "cleanup_failed": 1, "superseded": 0}
    bl.mark_job_dispatch_outbox.assert_not_called()


def test_recover_expired_preparing_submission_retries_when_terminalization_fails() -> None:
    bl = MagicMock()
    bl.claim_expired_preparing_job_dispatches.return_value = [
        {
            "job_id": "job-1",
            "owner_token": "cleanup-owner",
            "meta": {"task_envelope": {"payload_complete": False}},
        }
    ]
    bl.verify_job_dispatch_cleanup_owner.return_value = True
    bl.get_job.return_value = {
        "id": "job-1",
        "status": "pending",
        "worker_id": None,
        "meta": {"task_envelope": {"payload_complete": False}},
    }
    bl.job_lifecycle_unit_of_work.return_value = SimpleNamespace(
        transaction=lambda: nullcontext(MagicMock())
    )

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure",
        side_effect=RuntimeError("database unavailable"),
    ):
        result = cleanup_recovery.recover_expired_preparing_submissions(
            bl,
            storage=_Storage(),
            outbox_repository=_OutboxRepositoryDouble(bl),
        )

    assert result == {"claimed": 1, "recovered": 0, "cleanup_failed": 1, "superseded": 0}
    bl.mark_job_dispatch_outbox.assert_not_called()


def test_recover_expired_preparing_submission_reuses_completed_storage_cleanup_after_db_outage(
    monkeypatch,
) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    storage = InMemoryObjectStorage()
    plane = ArtifactDatasetPlane(storage=storage)
    artifact, _artifact_meta = plane.create_artifact(
        owner_scope={"project_id": "project-1", "job_id": "job-1"},
        run_id="job-1",
        step_id="upload",
        filename="invoice.pdf",
        content_type="application/pdf",
        body=b"invoice",
    )
    meta = {
        "attachment_persistence_artifacts": [{"artifact_id": artifact.artifact_id}],
        "task_envelope": {"payload_complete": False},
    }
    claimed = {"job_id": "job-1", "owner_token": "cleanup-owner", "meta": meta}
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "status": "pending",
        "worker_id": None,
        "meta": meta,
    }
    bl = MagicMock()
    bl.claim_expired_preparing_job_dispatches.return_value = [claimed]
    bl.verify_job_dispatch_cleanup_owner.return_value = True
    bl.settle_job_dispatch_cleanup.return_value = True
    bl.get_job.return_value = job
    bl.job_lifecycle_unit_of_work.return_value = SimpleNamespace(
        transaction=lambda: nullcontext(MagicMock())
    )

    complete_failure = MagicMock(side_effect=[RuntimeError("database unavailable"), None])
    monkeypatch.setattr(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure",
        complete_failure,
    )

    first = cleanup_recovery.recover_expired_preparing_submissions(
        bl,
        storage=storage,
        outbox_repository=_OutboxRepositoryDouble(bl),
    )
    marker_page, _marker_token = storage.list_key_page(
        "_replay_job_cleanup/", limit=10, continuation_token=None
    )
    second = cleanup_recovery.recover_expired_preparing_submissions(
        bl,
        storage=storage,
        outbox_repository=_OutboxRepositoryDouble(bl),
    )

    assert first == {"claimed": 1, "recovered": 0, "cleanup_failed": 1, "superseded": 0}
    assert len(marker_page) == 1
    assert second == {"claimed": 1, "recovered": 1, "cleanup_failed": 0, "superseded": 0}
    assert complete_failure.call_count == 2
    marker_keys, _ = storage.list_key_page(
        "_replay_job_cleanup/", limit=10, continuation_token=None
    )
    assert json.loads(storage.get(marker_keys[0]))["state"] == "committed"


def test_recover_expired_preparing_submission_settles_terminal_race() -> None:
    from core.application.jobs.terminal_persistence import TerminalPersistenceConflict

    bl = MagicMock()
    bl.claim_expired_preparing_job_dispatches.return_value = [
        {
            "job_id": "job-1",
            "owner_token": "cleanup-owner",
            "meta": {"task_envelope": {"payload_complete": False}},
        }
    ]
    bl.verify_job_dispatch_cleanup_owner.return_value = True
    bl.settle_job_dispatch_cleanup.return_value = True
    bl.get_job.return_value = {
        "id": "job-1",
        "status": "pending",
        "worker_id": None,
        "meta": {"task_envelope": {"payload_complete": False}},
    }
    bl.job_lifecycle_unit_of_work.return_value = SimpleNamespace(
        transaction=lambda: nullcontext(MagicMock())
    )

    with patch(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure",
        side_effect=TerminalPersistenceConflict("cancel won"),
    ):
        result = cleanup_recovery.recover_expired_preparing_submissions(
            bl,
            storage=_Storage(),
            outbox_repository=_OutboxRepositoryDouble(bl),
        )

    assert result == {"claimed": 1, "recovered": 0, "cleanup_failed": 0, "superseded": 1}
    bl.settle_job_dispatch_cleanup.assert_called_once_with(
        "job-1",
        owner_token="cleanup-owner",
        status="superseded",
        last_error="terminal_state_advanced",
    )
