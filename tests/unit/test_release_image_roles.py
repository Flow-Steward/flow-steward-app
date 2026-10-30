"""The official release image set: product roles plus the shared PostgreSQL image."""

from __future__ import annotations

import pytest

from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.environment import release_runtime_environment
from core.application.release.images import (
    all_release_roles,
    is_release_role_set,
    release_image_roles,
    required_product_roles,
)

_REVISION = "a" * 40
_DIGEST = "sha256:" + "b" * 64


def _identity(profile: str, version: str, roles: tuple[str, ...]) -> ReleaseIdentity:
    repositories = {role.role: role.repository for role in all_release_roles()}
    return ReleaseIdentity(
        version=version,
        tag=f"v{version}",
        commit_sha=_REVISION,
        registry="ghcr.io/flow-steward",
        profile=profile,
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role,
                registry_ref=f"ghcr.io/flow-steward/{repositories[role]}@{_DIGEST}",
                index_digest=_DIGEST,
                manifest_digest=_DIGEST,
                config_digest=_DIGEST,
                revision=_REVISION,
                platform="linux/amd64",
            )
            for role in roles
        ),
    )


def _product(profile: str) -> tuple[str, ...]:
    return tuple(role.role for role in required_product_roles(profile))


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_both_profiles_publish_the_postgres_image_from_0_9_2(profile: str) -> None:
    roles = release_image_roles(profile, version="0.9.2")

    assert tuple(role.role for role in roles) == (*_product(profile), "postgres")
    postgres = roles[-1]
    assert postgres.repository == "flow-steward-postgres"
    assert postgres.environment_variable == "POSTGRES_IMAGE"
    assert tuple(role.role for role in release_image_roles(profile, version="1.4.0")) == (
        *_product(profile),
        "postgres",
    )


@pytest.mark.parametrize("version", ("0.9.0", "0.9.1"))
@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_releases_published_before_the_role_are_resolved_without_it(
    profile: str, version: str
) -> None:
    roles = release_image_roles(profile, version=version)

    assert tuple(role.role for role in roles) == _product(profile)


def test_source_installs_still_prove_only_product_images() -> None:
    assert "postgres" not in _product("compact")
    assert "postgres" not in _product("scalable")


def test_release_scripts_publish_seven_distinct_repositories() -> None:
    repositories = [role.repository for role in all_release_roles()]

    assert len(repositories) == len(set(repositories)) == 7
    assert "flow-steward-postgres" in repositories


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_identity_with_the_postgres_role_is_valid_from_0_9_2(profile: str) -> None:
    release = _identity(profile, "0.9.2", (*_product(profile), "postgres"))

    assert [image.role for image in release.images][-1] == "postgres"


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_identity_resolved_by_an_older_updater_without_postgres_stays_valid(
    profile: str,
) -> None:
    """A 0.9.1 `release upgrade` resolves 0.9.2 without the role and stages it."""
    release = _identity(profile, "0.9.2", _product(profile))

    assert {image.role for image in release.images} == set(_product(profile))


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_identity_of_a_release_that_predates_the_role_cannot_name_it(profile: str) -> None:
    roles = (*_product(profile), "postgres")
    with pytest.raises(ValueError, match="complete"):
        _identity(profile, "0.9.1", roles)


def test_identity_still_requires_every_product_role() -> None:
    with pytest.raises(ValueError, match="complete scalable role set"):
        _identity("scalable", "0.9.2", ("fs-app", "fs-worker", "fs-cli", "fs-mcp", "postgres"))
    assert not is_release_role_set(
        "compact", version="0.9.2", roles=("compact-runtime", "fs-cli", "fs-mcp", "fs-app")
    )
    assert not is_release_role_set(
        "compact", version="0.9.2", roles=("compact-runtime", "fs-cli", "fs-mcp", "fs-mcp")
    )


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_runtime_environment_pins_postgres_only_when_the_release_publishes_it(
    profile: str,
) -> None:
    with_postgres = release_runtime_environment(
        _identity(profile, "0.9.2", (*_product(profile), "postgres"))
    )
    without_postgres = release_runtime_environment(_identity(profile, "0.9.2", _product(profile)))

    assert (
        with_postgres["POSTGRES_IMAGE"] == f"ghcr.io/flow-steward/flow-steward-postgres@{_DIGEST}"
    )
    assert "POSTGRES_IMAGE" not in without_postgres
    assert {key: value for key, value in with_postgres.items() if key != "POSTGRES_IMAGE"} == (
        without_postgres
    )
