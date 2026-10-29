from __future__ import annotations

import json
from copy import deepcopy
from typing import ClassVar

import pytest

from core.application.extensions import runtime_grants
from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.extensions.artifact_presentation import (
    ArtifactPresentationResolver,
    artifact_presentation_view,
    finalize_confirmed_artifact_presentations,
    strip_unaccepted_presentation_claims,
)
from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
from core.application.jobs.job_artifact_access import resolve_job_artifact_download
from core.application.management.scope import ScopeContext
from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.artifacts import (
    write_artifact_bytes,
)

BODY = b"sku,quantity\nABC-1,2\n"
BINDING = "attachment_artifact_handle"


def _prepared(tmp_path, monkeypatch, *, opted=True):
    monkeypatch.setenv(runtime_grants.FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    plane = ArtifactDatasetPlane(storage=storage)
    output = {"kind": "artifact", "binding_key": BINDING}
    if opted:
        output["presentation"] = {"claim_output": "attachment_presentation"}
    extension = {"artifact_policies": [{"operation_id": "get_attachment", "outputs": [output]}]}
    prepared = runtime_grants.prepare_extension_grants(
        plane=plane,
        scope=ScopeContext(account_id="account-a", project_id="project-a"),
        audit={"account_id": "account-a", "project_id": "project-a", "job_id": "job-a"},
        extension_id="fixture.imap",
        installed_extension=extension,
        operation_id="get_attachment",
        artifact_output_specs=[
            {
                "binding_key": BINDING,
                "filename": "imap-attachment.bin",
                "content_type": "application/octet-stream",
                "max_size_bytes": 1000,
            }
        ],
    )
    receipt = write_artifact_bytes(
        {"artifacts": {"outputs": prepared.artifact_outputs}},
        BODY,
        binding_key=BINDING,
        content_type="application/octet-stream",
    )
    claim = {key: receipt[key] for key in ("artifact_id", "binding_key", "size_bytes", "sha256")}
    claim.update(filename="orders.csv", content_type="text/csv")
    invocation = {
        "status": "succeeded",
        "response": {"ok": True, "result": {"attachment_presentation": claim}},
    }
    return plane, storage, prepared, extension, receipt, invocation


def test_actual_sdk_upload_terminal_claim_changes_display_and_download_only(tmp_path, monkeypatch):
    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    before = deepcopy(plane.get_artifact(receipt["artifact_id"]))
    finalized = runtime_grants.finalize_prepared_extension_grants(
        plane, prepared, invocation=invocation
    )
    assert finalized.errors == []
    assert finalized.produced_artifacts[0]["filename"] == "orders.csv"
    assert finalized.produced_artifacts[0]["content_type"] == "text/csv"
    canonical = plane.get_artifact(receipt["artifact_id"])
    assert canonical["filename"] == "imap-attachment.bin"
    assert canonical["content_type"] == "application/octet-stream"
    for key in ("artifact_id", "object_key", "owner_scope", "expires_at", "retention_class"):
        assert canonical[key] == before[key]
    assert plane.read_artifact_bytes(receipt["artifact_id"], max_size_bytes=1000) == BODY
    fresh = ArtifactDatasetPlane(storage=storage)
    download = resolve_job_artifact_download(
        job={"id": "job-a", "project_id": "project-a"},
        artifact_id=receipt["artifact_id"],
        resolve_artifact=ArtifactPresentationResolver(fresh),
    )
    assert (download.filename, download.content_type, download.size_bytes) == (
        "orders.csv",
        "text/csv",
        len(BODY),
    )
    assert receipt["content_type"] == "application/octet-stream"
    assert download.preview_content_type == "application/octet-stream"
    descriptor = runtime_grants.build_descriptor_for_invocation(
        plane,
        receipt["artifact_id"],
        kind="artifact",
        role="input",
        binding_key="read_existing",
        audit={"account_id": "account-a"},
    )
    assert descriptor["filename"] == "imap-attachment.bin"
    assert descriptor["content_type"] == "application/octet-stream"


def test_early_finalization_and_installed_policy_snapshot_cannot_erase_claim(tmp_path, monkeypatch):
    plane, _, prepared, extension, receipt, invocation = _prepared(tmp_path, monkeypatch)
    early, errors = runtime_grants.finalize_uploads(
        plane, [receipt["artifact_id"]], kind="artifact"
    )
    assert errors == []
    extension["artifact_policies"][0]["outputs"][0].pop("presentation")
    finalized = runtime_grants.finalize_prepared_extension_grants(
        plane, prepared, already_finalized=early, invocation=invocation
    )
    assert finalized.errors == []
    assert finalized.produced_artifacts[0]["filename"] == "orders.csv"
    plane.finalize_artifact_upload(receipt["artifact_id"])
    assert (
        artifact_presentation_view(
            plane.get_artifact(receipt["artifact_id"]),
            resolve_artifact=ArtifactPresentationResolver(plane),
        )["filename"]
        == "orders.csv"
    )
    assert plane.get_artifact(receipt["artifact_id"])["content_type"] == "application/octet-stream"


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_id", "art_other"),
        ("binding_key", "other_binding"),
        ("sha256", "0" * 64),
        ("size_bytes", 1),
        ("size_bytes", True),
        ("filename", "../escape.csv"),
        ("filename", "folder\\escape.csv"),
        ("filename", "unsafe\r\nheader.csv"),
        ("filename", "unsafe\x00.csv"),
        ("content_type", "text/html"),
        ("content_type", "image/svg+xml"),
        ("content_type", "text/csv\r\nx: bad"),
    ],
)
def test_mismatched_or_unsafe_claim_cannot_publish_success(tmp_path, monkeypatch, field, value):
    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    invocation["response"]["result"]["attachment_presentation"][field] = value
    finalized = runtime_grants.finalize_prepared_extension_grants(
        plane, prepared, invocation=invocation
    )
    assert finalized.errors
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is None
    assert plane.get_artifact(receipt["artifact_id"])["filename"] == "imap-attachment.bin"


@pytest.mark.parametrize("status", ["failed", "timed_out", "unknown"])
def test_non_success_cannot_commit_presentation(tmp_path, monkeypatch, status):
    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    invocation["status"] = status
    runtime_grants.finalize_prepared_extension_grants(plane, prepared, invocation=invocation)
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is None


def test_external_effect_presentation_can_be_finalized_without_publishing_claim(
    tmp_path, monkeypatch
):
    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    invocation["response"]["result"]["provider_reference"] = "provider-message-1"
    invocation["response"]["result"]["artifact_handle"] = f"artifact:{receipt['artifact_id']}"

    finalized = runtime_grants.finalize_prepared_extension_grants(
        plane,
        prepared,
        invocation=invocation,
        accept_presentations=False,
    )

    assert finalized.errors == []
    assert finalized.produced_artifacts[0]["filename"] == "imap-attachment.bin"
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is None
    assert plane.read_artifact_bytes(receipt["artifact_id"], max_size_bytes=1000) == BODY

    unresolved = deepcopy(invocation)
    unresolved["status"] = "failed"
    unresolved["response"]["external_effect_status"] = "timeout_unknown"
    strip_unaccepted_presentation_claims(prepared, unresolved)
    assert unresolved["response"]["result"] == {
        "provider_reference": "provider-message-1",
        "artifact_handle": f"artifact:{receipt['artifact_id']}",
    }
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is None

    from core.application.extensions.external_effects import apply_external_effect_confirmation

    late_unknown = apply_external_effect_confirmation(
        deepcopy(invocation), {"status": "timeout_unknown"}
    )
    strip_unaccepted_presentation_claims(prepared, late_unknown)
    assert late_unknown["status"] == "failed"
    assert late_unknown["response"]["error_code"] == "external_effect_timeout_unknown"
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is None

    confirmed = apply_external_effect_confirmation(
        deepcopy(invocation),
        {
            "status": "succeeded",
            "__confirmed_replay_invocation": deepcopy(invocation),
        },
    )
    produced_artifacts, errors = finalize_confirmed_artifact_presentations(
        plane,
        prepared,
        finalized.artifacts,
        confirmed,
        confirmed_success=True,
    )
    assert errors == []
    assert produced_artifacts[0]["filename"] == "orders.csv"
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is not None


def test_uninstalled_opt_in_cannot_be_enabled_by_output_specs_or_claim(tmp_path, monkeypatch):
    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch, opted=False)
    finalized = runtime_grants.finalize_prepared_extension_grants(
        plane, prepared, invocation=invocation
    )
    assert finalized.errors == []
    assert finalized.produced_artifacts[0]["filename"] == "imap-attachment.bin"
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is None


def test_identical_replay_is_idempotent_and_conflicting_replay_refused(tmp_path, monkeypatch):
    plane, _, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    first = runtime_grants.finalize_prepared_extension_grants(
        plane, prepared, invocation=invocation
    )
    assert first.errors == []
    assert (
        runtime_grants.finalize_prepared_extension_grants(
            plane, prepared, invocation=invocation
        ).errors
        == []
    )
    invocation["response"]["result"]["attachment_presentation"]["filename"] = "different.csv"
    assert runtime_grants.finalize_prepared_extension_grants(
        plane, prepared, invocation=invocation
    ).errors
    assert (
        artifact_presentation_view(
            plane.get_artifact(receipt["artifact_id"]),
            resolve_artifact=ArtifactPresentationResolver(plane),
        )["filename"]
        == "orders.csv"
    )


def test_orphan_sidecar_is_hidden_and_original_lifecycle_removes_it(tmp_path, monkeypatch):
    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    assert (
        runtime_grants.finalize_prepared_extension_grants(
            plane, prepared, invocation=invocation
        ).errors
        == []
    )
    key = f"artifacts/{receipt['artifact_id']}/_presentation.json"
    assert storage.get(key)
    assert plane.delete_artifact(receipt["artifact_id"]) is True
    assert storage.get(key) is None
    storage.put(
        key,
        json.dumps(invocation["response"]["result"]["attachment_presentation"]).encode(),
        content_type="application/json",
    )
    assert ArtifactDatasetPlane(storage=storage).get_artifact(receipt["artifact_id"]) is None


def test_s3_atomic_presentation_replay_and_browser_mime_override():
    import hashlib
    import io
    from types import SimpleNamespace

    from core.infrastructure.extension_runtime.object_storage import S3ObjectStorageAdapter

    class Missing(Exception):
        pass

    class PreconditionFailed(Exception):
        response: ClassVar[dict[str, object]] = {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        }

    class Client:
        exceptions = SimpleNamespace(NoSuchKey=Missing)

        def __init__(self):
            self.objects = {}
            self.signed = None

        def put_object(self, **request):
            if request.get("IfNoneMatch") == "*" and request["Key"] in self.objects:
                raise PreconditionFailed()
            self.objects[request["Key"]] = request["Body"]
            return {}

        def get_object(self, **request):
            if request["Key"] not in self.objects:
                raise Missing()
            return {"Body": io.BytesIO(self.objects[request["Key"]])}

        def head_object(self, **request):
            if request["Key"] not in self.objects:
                raise Missing()
            return {"ContentLength": len(self.objects[request["Key"]])}

        def generate_presigned_url(self, **request):
            self.signed = request
            return "https://objects.example.test/signed-download"

    client = Client()
    storage = object.__new__(S3ObjectStorageAdapter)
    storage._client = client
    storage._public_presign_client = client
    storage._public_endpoint_configured = True
    storage._bucket = "artifacts"
    storage._prefix = ""
    storage._presign_ttl = 900
    plane = ArtifactDatasetPlane(storage=storage)
    handle, _original = plane.create_artifact(
        owner_scope={"account_id": "account-a"},
        run_id="run-a",
        step_id="fetch",
        filename="imap-attachment.bin",
        content_type="application/octet-stream",
        body=BODY,
    )
    claim = {
        "artifact_id": handle.artifact_id,
        "binding_key": BINDING,
        "filename": "orders.csv",
        "content_type": "text/csv",
        "size_bytes": len(BODY),
        "sha256": hashlib.sha256(BODY).hexdigest(),
    }
    plane.commit_artifact_presentation(handle.artifact_id, claim)
    plane.commit_artifact_presentation(handle.artifact_id, dict(claim))
    with pytest.raises(ValueError, match="artifact_presentation_conflict"):
        plane.commit_artifact_presentation(
            handle.artifact_id, {**claim, "filename": "conflict.csv"}
        )
    fresh = ArtifactDatasetPlane(storage=storage)
    assert (
        artifact_presentation_view(
            fresh.get_artifact(handle.artifact_id),
            resolve_artifact=ArtifactPresentationResolver(fresh),
        )["filename"]
        == "orders.csv"
    )
    assert fresh.artifact_public_download_url(handle.artifact_id, filename="imap-attachment.bin")
    assert client.signed["Params"]["ResponseContentType"] == "text/csv"
    assert (
        client.signed["Params"]["ResponseContentDisposition"]
        == "attachment; filename*=UTF-8''orders.csv"
    )
    assert (
        json.loads(client.objects[f"artifacts/_metadata/{handle.artifact_id}.json"])["content_type"]
        == "application/octet-stream"
    )
    assert fresh.read_artifact_bytes(handle.artifact_id, max_size_bytes=1000) == BODY


def test_authorized_job_purge_removes_presentation_with_original(tmp_path, monkeypatch):
    from core.application.jobs.artifact_cleanup_runtime import purge_job_artifacts

    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    assert (
        runtime_grants.finalize_prepared_extension_grants(
            plane, prepared, invocation=invocation
        ).errors
        == []
    )
    result = purge_job_artifacts(
        {
            "id": "job-a",
            "project_id": "project-a",
            "meta": {"files": [{"artifact_id": receipt["artifact_id"]}]},
        },
        storage=storage,
    )
    assert result["failed"] == 0
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is None


def test_multiple_presentation_bindings_are_rejected_as_a_declaration():
    from core.application.extensions.artifact_presentation import installed_presentation_policy

    extension = {
        "artifact_policies": [
            {
                "operation_id": "fetch",
                "outputs": [
                    {
                        "kind": "artifact",
                        "binding_key": "one",
                        "presentation": {"claim_output": "first"},
                    },
                    {
                        "kind": "artifact",
                        "binding_key": "two",
                        "presentation": {"claim_output": "second"},
                    },
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="artifact_presentation_policy_invalid"):
        installed_presentation_policy(extension, "fetch")


def test_by_reference_and_ambiguous_presentation_are_refused_before_reservation():
    from core.application.extensions.artifact_presentation import validate_presentation_requests

    with pytest.raises(ValueError, match="artifact_presentation_output_not_fresh"):
        validate_presentation_requests(
            ((BINDING, "claim"),), [{"binding_key": BINDING, "id": "art_existing"}], []
        )
    with pytest.raises(ValueError, match="artifact_presentation_output_not_fresh"):
        validate_presentation_requests(
            ((BINDING, "claim"),), [], [{"binding_key": BINDING}, {"binding_key": BINDING}]
        )


def test_local_conditional_publication_never_exposes_partial_bytes(tmp_path, monkeypatch):
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor

    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    key = "artifacts/art_concurrent/_presentation.json"
    body = json.dumps({"filename": "report.csv", "padding": "x" * 100_000}).encode()
    ready = threading.Event()
    release = threading.Event()
    original_fdopen = os.fdopen

    class PausedFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, payload):
            split = len(payload) // 2
            self.stream.write(payload[:split])
            self.stream.flush()
            ready.set()
            if not release.wait(5):
                raise RuntimeError("publication writer timed out")
            self.stream.write(payload[split:])
            return len(payload)

        def flush(self):
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    monkeypatch.setattr(
        os, "fdopen", lambda fd, *args, **kwargs: PausedFile(original_fdopen(fd, *args, **kwargs))
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(storage.put_if_absent, key, body, content_type="application/json")
        try:
            assert ready.wait(5)
            # A writer has flushed half its JSON, but the final key is absent.
            assert storage.get(key) is None
            second = pool.submit(storage.put_if_absent, key, body, content_type="application/json")
        finally:
            release.set()
        assert sorted([first.result(timeout=5), second.result(timeout=5)]) == [False, True]
    assert storage.get(key) == body
    assert storage.put_if_absent(key, body, content_type="application/json") is False
    assert list(tmp_path.rglob(".conditional-*")) == []


def test_local_failed_publication_leaves_no_final_key_or_temporary_file(tmp_path, monkeypatch):
    import os

    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path))
    key = "artifacts/art_failed/_presentation.json"
    body = b'{"filename":"complete.csv"}'
    original_fsync = os.fsync
    monkeypatch.setattr(
        os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("injected before publication"))
    )
    with pytest.raises(OSError, match="injected before publication"):
        storage.put_if_absent(key, body, content_type="application/json")
    assert storage.get(key) is None
    assert list(tmp_path.rglob(".conditional-*")) == []
    monkeypatch.setattr(os, "fsync", original_fsync)
    assert storage.put_if_absent(key, body, content_type="application/json") is True
    assert storage.get(key) == body


def test_unauthorized_production_readers_never_read_presentation_sidecar(tmp_path, monkeypatch):
    from core.application.jobs.workflow_run_projection import _safe_artifact
    from core.application.management.errors import ServiceNotFoundError
    from core.application.reports.ui_projection_helpers import _artifact_descriptors

    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    assert (
        runtime_grants.finalize_prepared_extension_grants(
            plane, prepared, invocation=invocation
        ).errors
        == []
    )
    key = f"artifacts/{receipt['artifact_id']}/_presentation.json"
    original_get = storage.get
    reads = []

    def spy_get(candidate):
        if candidate.endswith("/_presentation.json"):
            reads.append(candidate)
        return original_get(candidate)

    monkeypatch.setattr(storage, "get", spy_get)
    resolver = ArtifactPresentationResolver(plane)
    with pytest.raises(ServiceNotFoundError):
        resolve_job_artifact_download(
            job={"id": "other-job", "project_id": "project-a"},
            artifact_id=receipt["artifact_id"],
            resolve_artifact=resolver,
        )
    assert reads == []
    assert (
        _safe_artifact(
            {"artifact_id": receipt["artifact_id"]},
            job={"id": "other-job", "project_id": "project-a"},
            resolve_artifact=resolver,
        )
        is None
    )
    assert reads == []
    assert (
        _artifact_descriptors(
            {"artifact_id": receipt["artifact_id"]},
            role="output",
            project_id="project-other",
            job_id="job-a",
            resolve_artifact=resolver,
        )
        == []
    )
    assert reads == []
    download = resolve_job_artifact_download(
        job={"id": "job-a", "project_id": "project-a"},
        artifact_id=receipt["artifact_id"],
        resolve_artifact=resolver,
    )
    assert download.filename == "orders.csv"
    assert reads == [key]


@pytest.mark.parametrize(
    "field,value",
    [
        (
            "owner_scope",
            {"account_id": "account-a", "project_id": "project-a", "job_id": "other-job"},
        ),
        ("object_key", "artifacts/other-artifact/replaced.bin"),
    ],
)
def test_terminal_commit_rechecks_fresh_durable_mint_identity(tmp_path, monkeypatch, field, value):
    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    early, errors = runtime_grants.finalize_uploads(
        plane, [receipt["artifact_id"]], kind="artifact"
    )
    assert errors == []
    metadata_key = f"artifacts/_metadata/{receipt['artifact_id']}.json"
    changed = json.loads(storage.get(metadata_key))
    changed[field] = value
    storage.put(metadata_key, json.dumps(changed).encode(), content_type="application/json")
    finalized = runtime_grants.finalize_prepared_extension_grants(
        plane, prepared, already_finalized=early, invocation=invocation
    )
    assert finalized.errors[0]["code"] == "artifact_presentation_original_unavailable"
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is None
    assert json.loads(storage.get(metadata_key))[field] == value


def test_owner_change_during_publication_removes_only_its_new_claim(tmp_path, monkeypatch):
    plane, storage, prepared, _, receipt, invocation = _prepared(tmp_path, monkeypatch)
    metadata_key = f"artifacts/_metadata/{receipt['artifact_id']}.json"
    original_put = storage.put_if_absent

    def transfer_before_publication(key, body, *, content_type):
        if key.endswith("/_presentation.json"):
            changed = json.loads(storage.get(metadata_key))
            changed["owner_scope"]["job_id"] = "new-owner"
            storage.put(metadata_key, json.dumps(changed).encode(), content_type="application/json")
        return original_put(key, body, content_type=content_type)

    monkeypatch.setattr(storage, "put_if_absent", transfer_before_publication)
    finalized = runtime_grants.finalize_prepared_extension_grants(
        plane, prepared, invocation=invocation
    )
    assert finalized.errors[0]["code"] == "artifact_presentation_original_unavailable"
    assert storage.get(f"artifacts/{receipt['artifact_id']}/_presentation.json") is None
    assert json.loads(storage.get(metadata_key))["owner_scope"]["job_id"] == "new-owner"
