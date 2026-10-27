"""Canonical installed origin for source and official release workflows."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.application.release.catalog import ReleaseError, validated_release_repository
from core.application.release.contracts import ReleaseIdentity


class _ClosedFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfficialInstallationOrigin(_ClosedFrozenModel):
    """A completed installation authenticated by one official release identity."""

    kind: Literal["official"] = "official"
    release: ReleaseIdentity


class SourceInstallationOrigin(_ClosedFrozenModel):
    """Installed source provenance without fabricated release metadata."""

    kind: Literal["source"] = "source"
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    deployment_profile: Literal["compact", "scalable"]
    platform: Literal["linux/amd64", "linux/arm64"]
    installation_id: str = Field(min_length=1, max_length=255)
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_repository: str

    @model_validator(mode="after")
    def validate_repository(self) -> SourceInstallationOrigin:
        try:
            validated_release_repository(self.release_repository)
        except ReleaseError as exc:
            raise ValueError(str(exc)) from exc
        return self


InstalledReleaseOrigin: TypeAlias = Annotated[
    OfficialInstallationOrigin | SourceInstallationOrigin,
    Field(discriminator="kind"),
]


class InstalledReleaseOriginPort(Protocol):
    def load(self) -> InstalledReleaseOrigin: ...


__all__ = [
    "InstalledReleaseOrigin",
    "InstalledReleaseOriginPort",
    "OfficialInstallationOrigin",
    "SourceInstallationOrigin",
]
