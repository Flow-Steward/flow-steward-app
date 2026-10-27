"""AC17-R123: how an extension is told where to fetch an artifact it cannot presign."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.extensions import runtime_grants
from core.application.management.errors import ServiceUnavailableError
from core.infrastructure.security.artifact_access_tokens import (
    HEADER_NAME,
    verify_artifact_access_token,
)

_READY_META = {
    "object_key": "artifacts/art_1/payload.bin",
    "sha256": "a" * 64,
    "size_bytes": 11,
    "filename": "payload.bin",
    "retention_class": "run_window",
}


class _Plane:
    """The narrow surface runtime_grants asks of the artifact plane.

    It offers `artifact_presigned_url` and nothing else, because that is the only
    source the real plane provides. `runtime_grants` also probes a public `storage`
    attribute, and `ArtifactDatasetPlane` keeps its adapter as `_storage` -- so that
    branch has never run outside a test double. A fake that exposed `storage` would
    test a path production does not take, which is the shape that has fooled this
    repository before.
    """

    def __init__(self, presigned_url: str = "") -> None:
        self._presigned_url = presigned_url

    def artifact_presigned_url(
        self, artifact_id: str, *, method: str, ttl_seconds: int = 600
    ) -> str:
        return self._presigned_url

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        return {"id": artifact_id, **_READY_META}


@pytest.fixture(autouse=True)
def _deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "a-deployment-secret-for-tests")
    monkeypatch.setenv("FS_APP_INTERNAL_URL", "http://fs-app:8099")
    monkeypatch.delenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", raising=False)
    monkeypatch.setattr(runtime_grants, "meta_for_grant", lambda *a, **k: dict(_READY_META))


def _target(presigned_url: str, method: str = "GET"):
    return runtime_grants.grant_target(
        _Plane(presigned_url), "art_1", kind="artifact", method=method
    )


def test_storage_that_can_hand_out_a_url_is_left_alone() -> None:
    """The question is whether this caller got a usable URL, never which backend it is.

    An extension subprocess runs inside the deployment, so an internal service address
    is perfectly usable to it. Routing that through the platform would replace a
    working direct path with a slower one on every deployment that has object storage.
    """
    target = _target("http://seaweedfs:8333/bucket/art_1?X-Amz-Signature=abc")

    assert target.url.startswith("http://seaweedfs:8333/")
    assert target.headers == {}


def test_a_path_is_not_a_url_so_the_platform_serves_the_object_itself() -> None:
    """The defect: local_fs presigns `file://`, which an extension cannot fetch."""
    target = _target("file:///data/artifacts/art_1/payload.bin")

    assert target.url == "http://fs-app:8099/api/extension-artifacts/artifact/art_1"
    claims = verify_artifact_access_token(
        target.headers[HEADER_NAME], object_id="art_1", kind="artifact", method="GET"
    )
    assert claims["method"] == "GET"


def test_the_deliberate_local_path_escape_hatch_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who has trusted `file://` keeps it; nothing here withdraws that."""
    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")

    target = _target("file:///data/artifacts/art_1/payload.bin")

    assert target.url.startswith("file://")
    assert target.headers == {}


def test_a_write_grant_carries_a_write_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # An output target must still be empty and unfinalized; that rule is upstream of
    # this one and is left exactly where it was.
    empty = {key: value for key, value in _READY_META.items() if key not in {"sha256", "size_bytes"}}
    monkeypatch.setattr(runtime_grants, "meta_for_grant", lambda *a, **k: dict(empty))

    target = _target("file:///data/artifacts/art_1/payload.bin", method="PUT")

    claims = verify_artifact_access_token(
        target.headers[HEADER_NAME], object_id="art_1", kind="artifact", method="PUT"
    )
    assert claims["method"] == "PUT"


def test_a_deployment_that_was_never_told_its_address_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant pointing nowhere fails as a network error and reads as an outage.

    There is no default worth guessing here: the one compiled in for the MCP role is a
    Scalable service name, wrong on every other profile, and a silent hop to a host
    that does not exist is the least diagnosable failure available.
    """
    monkeypatch.delenv("FS_APP_INTERNAL_URL", raising=False)

    with pytest.raises(ServiceUnavailableError, match="FS_APP_INTERNAL_URL"):
        _target("file:///data/artifacts/art_1/payload.bin")


def test_a_deployment_without_a_signing_secret_says_that_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FS_SECRET_KEY", raising=False)

    with pytest.raises(ServiceUnavailableError, match="FS_SECRET_KEY"):
        _target("file:///data/artifacts/art_1/payload.bin")


def test_the_token_does_not_authorize_the_other_direction() -> None:
    """A read grant that could also write would be a wider capability than the grant."""
    from core.infrastructure.security.artifact_access_tokens import ArtifactAccessTokenError

    read = _target("file:///data/artifacts/art_1/payload.bin")

    with pytest.raises(ArtifactAccessTokenError, match="not for this request"):
        verify_artifact_access_token(
            read.headers[HEADER_NAME], object_id="art_1", kind="artifact", method="PUT"
        )


def test_presigned_url_keeps_its_shape_for_its_existing_callers() -> None:
    url = runtime_grants.presigned_url(
        _Plane("http://seaweedfs:8333/bucket/art_1"), "art_1", kind="artifact", method="GET"
    )

    assert url == "http://seaweedfs:8333/bucket/art_1"


def test_the_plane_offers_no_public_storage_attribute() -> None:
    """Pinned because a test double that offers one tests a path production never takes.

    `runtime_grants` probes `getattr(plane, "storage", None)` as a second source of
    URLs, and `ArtifactDatasetPlane` keeps its adapter as `_storage`. The branch is
    therefore unreachable with the real plane and reachable only from a fake -- the
    same shape as a `getattr(store, "method", None)` that fails open in production
    while a mock keeps the test green.
    """
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

    assert not hasattr(ArtifactDatasetPlane, "storage")
    assert "storage" not in getattr(ArtifactDatasetPlane, "__annotations__", {})
