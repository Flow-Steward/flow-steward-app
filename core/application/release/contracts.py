"""Canonical immutable identity contracts for official releases."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.application.release.catalog import ReleaseCoordinates
from core.application.release.images import is_release_role_set

_CANONICAL_RELEASE_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$", re.ASCII)
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_OCI_REGISTRY_HOST = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*(?::\d+)?$", re.ASCII)
_OCI_REPOSITORY_COMPONENT = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def parse_digest_pinned_reference(reference: str) -> tuple[str, str]:
    """Parse one canonical ``registry/path@sha256:digest`` reference."""
    value = str(reference or "")
    if value != value.strip() or value.count("@") != 1:
        raise ValueError("registry_ref must be exactly pinned to one sha256 digest")
    repository, digest = value.split("@", 1)
    components = repository.split("/")
    if (
        len(components) < 2
        or _OCI_REGISTRY_HOST.fullmatch(components[0]) is None
        or any(_OCI_REPOSITORY_COMPONENT.fullmatch(item) is None for item in components[1:])
        or _OCI_DIGEST.fullmatch(digest) is None
    ):
        raise ValueError("registry_ref must be exactly pinned to one sha256 digest")
    return repository, digest


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReleaseImageIdentity(_ClosedModel):
    """Immutable identity of one product image selected for a release."""

    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    registry_ref: str = Field(min_length=1, max_length=512)
    index_digest: str
    manifest_digest: str
    config_digest: str
    revision: str
    platform: Literal["linux/amd64", "linux/arm64"]

    @model_validator(mode="after")
    def validate_immutable_identity(self) -> Self:
        for name in ("index_digest", "manifest_digest", "config_digest"):
            if _OCI_DIGEST.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"{name} must be a canonical sha256 digest")
        if _COMMIT_SHA.fullmatch(self.revision) is None:
            raise ValueError("revision must be a full lowercase commit SHA")
        _repository, digest = parse_digest_pinned_reference(self.registry_ref)
        if digest != self.manifest_digest:
            raise ValueError("registry_ref must be exactly pinned to manifest_digest")
        return self


class ReleaseIdentity(_ClosedModel):
    """One complete, verified release identity published as a single record."""

    schema_version: Literal[1] = 1
    version: str
    tag: str
    commit_sha: str
    registry: str = Field(min_length=1, max_length=255)
    profile: Literal["compact", "scalable"]
    platform: Literal["linux/amd64", "linux/arm64"]
    images: tuple[ReleaseImageIdentity, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_release_identity(self) -> Self:
        if _CANONICAL_RELEASE_VERSION.fullmatch(self.version) is None:
            raise ValueError("version must be canonical X.Y.Z")
        if self.tag != f"v{self.version}":
            raise ValueError("tag must be the canonical vX.Y.Z tag for version")
        if _COMMIT_SHA.fullmatch(self.commit_sha) is None:
            raise ValueError("commit_sha must be a full lowercase commit SHA")
        roles = [image.role for image in self.images]
        if len(roles) != len(set(roles)):
            raise ValueError("release image roles must be unique")
        if not is_release_role_set(self.profile, version=self.version, roles=roles):
            raise ValueError(f"release identity requires the complete {self.profile} role set")
        for image in self.images:
            repository = image.registry_ref.partition("@")[0]
            if not repository.startswith(self.registry.rstrip("/") + "/"):
                raise ValueError("every release image must belong to the selected registry")
            if image.platform != self.platform:
                raise ValueError("every release image must match the release platform")
            if image.revision != self.commit_sha:
                raise ValueError("every release image revision must match the release commit")
        return self


def release_identity_from_resolution(
    release: ReleaseCoordinates,
    *,
    registry: str,
    profile: Literal["compact", "scalable"],
    platform: str,
    identities: tuple[Mapping[str, str] | object, ...],
) -> ReleaseIdentity:
    """Build the durable aggregate directly from verified registry identities."""
    images = tuple(
        ReleaseImageIdentity.model_validate(
            {
                "role": _identity_value(identity, "role"),
                "registry_ref": _identity_value(identity, "registry_ref"),
                "index_digest": _identity_value(identity, "index_digest"),
                "manifest_digest": _identity_value(identity, "manifest_digest"),
                "config_digest": _identity_value(identity, "config_digest"),
                "revision": _identity_value(identity, "revision"),
                "platform": _identity_value(identity, "platform"),
            }
        )
        for identity in identities
    )
    return ReleaseIdentity.model_validate(
        {
            "version": str(release.version),
            "tag": release.tag,
            "commit_sha": release.commit_sha,
            "registry": registry,
            "profile": profile,
            "platform": platform,
            "images": images,
        }
    )


def _identity_value(identity: Mapping[str, str] | object, name: str) -> object:
    return identity[name] if isinstance(identity, Mapping) else getattr(identity, name)


__all__ = [
    "ReleaseIdentity",
    "ReleaseImageIdentity",
    "parse_digest_pinned_reference",
    "release_identity_from_resolution",
]
