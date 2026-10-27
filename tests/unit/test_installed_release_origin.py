from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.application.release.installed_origin import (
    OfficialInstallationOrigin,
    SourceInstallationOrigin,
)


def _official_release() -> ReleaseIdentity:
    digest = "sha256:" + "a" * 64
    return ReleaseIdentity(
        version="0.3.0",
        tag="v0.3.0",
        commit_sha="b" * 40,
        registry="ghcr.io/flow-steward",
        profile="compact",
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/flow-steward/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision="b" * 40,
                platform="linux/amd64",
            )
            for role in required_product_roles("compact")
        ),
    )


def test_official_origin_preserves_only_the_verified_release_identity() -> None:
    release = _official_release()

    origin = OfficialInstallationOrigin(release=release)

    assert origin.kind == "official"
    assert origin.release == release
    assert set(origin.model_dump()) == {"kind", "release"}


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_source_origin_records_installed_truth_without_inventing_release_metadata(
    profile: str,
) -> None:
    origin = SourceInstallationOrigin(
        commit_sha="c" * 40,
        deployment_profile=profile,
        platform="linux/amd64",
        installation_id=f"source-{profile}",
        configuration_sha256="d" * 64,
        release_repository="Flow-Steward/flow-steward-app",
    )

    assert origin.kind == "source"
    assert origin.commit_sha == "c" * 40
    assert set(origin.model_dump()) == {
        "kind",
        "commit_sha",
        "deployment_profile",
        "platform",
        "installation_id",
        "configuration_sha256",
        "release_repository",
    }
    assert not {"version", "tag", "registry", "images"}.intersection(origin.model_dump())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("commit_sha", "short"),
        ("commit_sha", "A" * 40),
        ("configuration_sha256", "d" * 63),
        ("installation_id", ""),
        ("release_repository", "not-a-repository"),
    ),
)
def test_source_origin_rejects_noncanonical_installed_identity(field: str, value: str) -> None:
    values = {
        "commit_sha": "c" * 40,
        "deployment_profile": "compact",
        "platform": "linux/amd64",
        "installation_id": "source-compact",
        "configuration_sha256": "d" * 64,
        "release_repository": "Flow-Steward/flow-steward-app",
    }
    values[field] = value

    with pytest.raises((ValidationError, ValueError)):
        SourceInstallationOrigin.model_validate(values)
