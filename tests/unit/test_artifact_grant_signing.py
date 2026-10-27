from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from inspect import signature
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.runtime_grants import build_descriptor_for_invocation
from core.infrastructure.extension_runtime.extension_runtime_common import _sandboxed_env
from core.infrastructure.extension_runtime.object_storage import S3ObjectStorageAdapter
from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import artifacts
from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.http import (
    build_pinned_opener,
    open_pinned_url,
    verified_platform_grant_signature,
)
from core.infrastructure.security.artifact_grant_signing import (
    artifact_grant_claims,
    artifact_grant_verification_key,
)


def test_host_artifact_grant_binds_scope_method_constraints_and_expiry(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "unit-artifact-grant-secret")
    plane = SimpleNamespace(
        get_artifact=MagicMock(
            return_value={
                "artifact_id": "artifact-1",
                "object_key": "artifacts/artifact-1/catalog.csv",
                "owner_scope": {
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "project_id": "project-1",
                },
                "content_type": "text/csv",
            }
        ),
        artifact_upload_stat=MagicMock(return_value=None),
        artifact_presigned_url=MagicMock(
            return_value="http://seaweedfs:8333/bucket/artifact-1?signature=provider"
        ),
    )

    descriptor = build_descriptor_for_invocation(
        plane,
        "artifact-1",
        kind="artifact",
        role="output",
        binding_key="catalog_artifact",
        audit={
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "project-1",
            "job_id": "job-1",
            "workflow_id": "sample_catalog_enrichment",
            "step_id": "store_catalog",
        },
        max_size_bytes=4096,
    )

    claims = artifact_grant_claims(descriptor)
    assert claims == {
        "version": 1,
        "artifact_id": "artifact-1",
        "artifact_kind": "artifact",
        "binding_key": "catalog_artifact",
        "content_type": "text/csv",
        "scope": {
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "project-1",
            "job_id": "job-1",
            "workflow_id": "sample_catalog_enrichment",
            "step_id": "store_catalog",
        },
        "method": "PUT",
        "upload_url": "http://seaweedfs:8333/bucket/artifact-1?signature=provider",
        "max_size_bytes": 4096,
        "required_headers": {"if-none-match": "*"},
        "expires_at": descriptor["access"]["expires_at"],
        "single_use": True,
    }
    assert datetime.fromisoformat(claims["expires_at"].replace("Z", "+00:00")) > datetime.now(UTC)
    assert descriptor["access"]["platform_grant"]["signature"]


def test_subprocess_receives_only_artifact_grant_public_key(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "unit-artifact-grant-secret")

    env = _sandboxed_env()

    assert env["FS_ARTIFACT_GRANT_VERIFY_KEY"] == artifact_grant_verification_key()
    assert "FS_SECRET_KEY" not in env


def test_host_signs_private_input_artifact_grant_for_exact_get_url(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "unit-artifact-grant-secret")
    plane = SimpleNamespace(
        get_artifact=MagicMock(
            return_value={
                "artifact_id": "artifact-1",
                "object_key": "artifacts/artifact-1/catalog.csv",
                "owner_scope": {
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "project_id": "project-1",
                },
                "content_type": "text/csv",
                "size_bytes": 7,
                "sha256": "a" * 64,
            }
        ),
        artifact_presigned_url=MagicMock(
            return_value="http://seaweedfs:8333/bucket/artifact-1?signature=provider"
        ),
    )

    descriptor = build_descriptor_for_invocation(
        plane,
        "artifact-1",
        kind="artifact",
        role="input",
        binding_key="catalog_artifact",
        audit={
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "project-1",
            "job_id": "job-1",
            "workflow_id": "sample_catalog_enrichment",
            "step_id": "load_catalog",
        },
    )

    claims = artifact_grant_claims(descriptor)
    assert claims["method"] == "GET"
    assert claims["upload_url"] == descriptor["access"]["download_url"]
    assert claims["single_use"] is False
    monkeypatch.setenv("FS_ARTIFACT_GRANT_VERIFY_KEY", artifact_grant_verification_key())
    assert verified_platform_grant_signature(descriptor, descriptor["access"])

    tampered = dict(descriptor)
    tampered["access"] = dict(descriptor["access"])
    tampered["access"]["download_url"] += "&other=1"
    assert not verified_platform_grant_signature(tampered, tampered["access"])


def test_signed_grant_binds_root_group_scope_and_execution(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "unit-artifact-grant-secret")
    plane = SimpleNamespace(
        get_artifact=MagicMock(
            return_value={
                "artifact_id": "artifact-1",
                "object_key": "artifacts/artifact-1/catalog.csv",
                "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID, "project_id": "project-1"},
                "content_type": "text/csv",
                "size_bytes": 7,
                "sha256": "a" * 64,
            }
        ),
        artifact_presigned_url=MagicMock(
            return_value="http://seaweedfs:8333/bucket/artifact-1?signature=provider"
        ),
    )
    execution_id = "77777777-7777-4777-8777-777777777777"
    descriptor = build_descriptor_for_invocation(
        plane,
        "artifact-1",
        kind="artifact",
        role="input",
        binding_key="catalog_artifact",
        audit={
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "project-1",
            "job_id": "job-1",
            "group_id": None,
            "step_id": "load_catalog",
            "step_execution_id": execution_id,
        },
    )

    claims = artifact_grant_claims(descriptor)
    # Explicit null is signed as null: the grant is bound to the root scope.
    assert claims["scope"]["group_id"] is None
    assert claims["scope"]["step_execution_id"] == execution_id
    assert "phase_id" not in claims["scope"]
    monkeypatch.setenv("FS_ARTIFACT_GRANT_VERIFY_KEY", artifact_grant_verification_key())
    assert verified_platform_grant_signature(descriptor, descriptor["access"])

    for field, value in (("group_id", "per_item"), ("step_execution_id", None)):
        tampered = dict(descriptor)
        tampered["scope"] = {**descriptor["scope"], field: value}
        assert not verified_platform_grant_signature(tampered, tampered["access"])
    unbound = dict(descriptor)
    unbound["scope"] = {k: v for k, v in descriptor["scope"].items() if k != "group_id"}
    assert not verified_platform_grant_signature(unbound, unbound["access"])


def test_signed_input_grant_uses_exact_private_artifact_transport(monkeypatch) -> None:
    body = b"one,two"
    monkeypatch.setenv("FS_SECRET_KEY", "unit-artifact-grant-secret")
    plane = SimpleNamespace(
        get_artifact=MagicMock(
            return_value={
                "artifact_id": "artifact-1",
                "object_key": "artifacts/artifact-1/catalog.csv",
                "owner_scope": {
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "project_id": "project-1",
                },
                "content_type": "text/csv",
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        ),
        artifact_presigned_url=MagicMock(
            return_value="http://seaweedfs:8333/bucket/artifact-1?signature=provider"
        ),
    )
    descriptor = build_descriptor_for_invocation(
        plane,
        "artifact-1",
        kind="artifact",
        role="input",
        binding_key="catalog_artifact",
        audit={"account_id": DEFAULT_ACCOUNT_ID, "project_id": "project-1"},
    )
    monkeypatch.setenv("FS_ARTIFACT_GRANT_VERIFY_KEY", artifact_grant_verification_key())
    private_open = MagicMock(return_value=io.BytesIO(body))
    monkeypatch.setattr(artifacts, "open_platform_artifact_url", private_open)
    monkeypatch.setattr(
        artifacts,
        "open_pinned_url",
        MagicMock(side_effect=AssertionError("signed private input must use grant authority")),
    )

    result = b"".join(
        artifacts.stream_artifact_bytes(
            {"artifacts": {"inputs": [descriptor]}},
            artifact_id="artifact:artifact-1",
            binding_key="catalog_artifact",
        )
    )

    assert result == body
    request = private_open.call_args.args[0]
    assert request.full_url == descriptor["access"]["download_url"]
    assert request.get_method() == "GET"


def test_s3_upload_grant_is_conditionally_create_only() -> None:
    adapter = S3ObjectStorageAdapter.__new__(S3ObjectStorageAdapter)
    adapter._client = MagicMock()
    adapter._bucket = "artifacts"
    adapter._prefix = "tenant"
    adapter._presign_ttl = 600
    adapter._client.generate_presigned_url.return_value = "https://storage.example/upload"

    adapter.presign("artifacts/artifact-1/catalog.csv", method="PUT")

    adapter._client.generate_presigned_url.assert_called_once_with(
        ClientMethod="put_object",
        Params={
            "Bucket": "artifacts",
            "Key": "tenant/artifacts/artifact-1/catalog.csv",
            "IfNoneMatch": "*",
        },
        ExpiresIn=600,
    )


def test_general_ssrf_helpers_expose_no_private_peer_bypass() -> None:
    assert "allow_private" not in signature(open_pinned_url).parameters
    assert "allow_private" not in signature(build_pinned_opener).parameters
