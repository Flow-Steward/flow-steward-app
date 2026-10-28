"""Canonical Flow Steward image roles for official releases."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from core.application.release.catalog import ReleaseVersion


@dataclass(frozen=True, slots=True)
class ProductImageRole:
    """One published first-party image and its production environment binding.

    ``first_release`` is the first official release that publishes the role; empty
    means every release does. A release identity of an older version never names
    the role, and an identity of a later version may still lack it when an updater
    that predates the role resolved it (ADR 0030, amendment for 0.9.2).
    """

    role: str
    repository: str
    environment_variable: str
    first_release: str = ""


_ROLE_BY_NAME = {
    role.role: role
    for role in (
        ProductImageRole("compact-runtime", "flow-steward-compact", "FS_COMPACT_IMAGE"),
        ProductImageRole("fs-app", "flow-steward-app", "FS_APP_IMAGE"),
        ProductImageRole("fs-worker", "flow-steward-worker", "FS_WORKER_IMAGE"),
        ProductImageRole("control-plane", "flow-steward-control", "FS_CONTROL_IMAGE"),
        ProductImageRole("fs-cli", "flow-steward-cli", "FS_CLI_IMAGE"),
        ProductImageRole("fs-mcp", "flow-steward-mcp", "MCP_IMAGE"),
        # The canonical PostgreSQL image (infra/docker/postgres-extended). Both
        # profiles run it; official installs pull it instead of building it.
        ProductImageRole(
            "postgres", "flow-steward-postgres", "POSTGRES_IMAGE", first_release="0.9.2"
        ),
    )
}

_PROFILE_ROLE_NAMES = {
    "compact": ("compact-runtime", "fs-cli", "fs-mcp"),
    "scalable": ("fs-app", "fs-worker", "control-plane", "fs-cli", "fs-mcp"),
}
# Images every profile shares. They belong to a release's published set, but not to
# the product images a source installation builds and proves by revision.
_SHARED_ROLE_NAMES = ("postgres",)


def required_product_roles(profile: str) -> tuple[ProductImageRole, ...]:
    """Return the ordered first-party application image roles of a deployment profile."""

    normalized = str(profile or "").strip().lower()
    try:
        names = _PROFILE_ROLE_NAMES[normalized]
    except KeyError as exc:
        raise ValueError("unsupported release deployment profile") from exc
    return tuple(_ROLE_BY_NAME[name] for name in names)


def _published_by(role: ProductImageRole, version: ReleaseVersion) -> bool:
    return not role.first_release or ReleaseVersion.parse(role.first_release) <= version


def release_image_roles(profile: str, *, version: str) -> tuple[ProductImageRole, ...]:
    """Return the complete ordered role set official release ``version`` publishes.

    Resolution requires every one of them from a single registry, so a mirror that
    lacks a role the release publishes is incomplete rather than silently partial.
    """

    published = ReleaseVersion.parse(version)
    shared = tuple(
        role
        for role in (_ROLE_BY_NAME[name] for name in _SHARED_ROLE_NAMES)
        if _published_by(role, published)
    )
    return (*required_product_roles(profile), *shared)


def is_release_role_set(profile: str, *, version: str, roles: Collection[str]) -> bool:
    """Whether ``roles`` is a valid durable role set for a release identity.

    Every product role of the profile is required. A shared role is accepted only
    for a version that publishes it, and is optional there: an updater that
    predates the role resolves the newer release without it, and that identity
    must still install (the installation then provides the shared image itself).
    """

    product = {role.role for role in required_product_roles(profile)}
    optional = {role.role for role in release_image_roles(profile, version=version)} - product
    observed = set(roles)
    return len(observed) == len(roles) and product <= observed <= product | optional


def all_release_roles() -> tuple[ProductImageRole, ...]:
    """Return every independently published image role once."""

    return tuple(_ROLE_BY_NAME.values())


__all__ = [
    "ProductImageRole",
    "all_release_roles",
    "is_release_role_set",
    "release_image_roles",
    "required_product_roles",
]
