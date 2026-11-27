"""Closed request, result and port models of the release service.

:mod:`core.application.release.service` re-exports every public name here.
"""

from __future__ import annotations

from typing import Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.catalog import ReleaseVersion
from core.application.release.contracts import ReleaseIdentity
from core.application.release.installed_origin import SourceInstallationOrigin
from core.application.release.platform_modules import ModuleSet, PreparedModuleRef

_LINUX_AMD64 = "linux/amd64"
_LINUX_ARM64 = "linux/arm64"
_SUPPORTED_RELEASE_PLATFORMS = frozenset({_LINUX_AMD64, _LINUX_ARM64})
DeploymentProfile = Literal["compact", "scalable"]
ReleasePlatform = Literal["linux/amd64", "linux/arm64"]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReleaseInfoRequest(_ClosedModel):
    """Read-only installed-release request."""


class ReleaseCheckRequest(_ClosedModel):
    """Read-only stable GitHub Release check request."""


class ReleaseInstallRequest(_ClosedModel):
    """Select the exact official identity required by an installer invocation."""

    discover_latest: bool


def release_platform_for_machine(machine: str) -> ReleasePlatform:
    """Normalize the supported host architectures once for all release consumers."""
    architecture = str(machine or "").strip().lower()
    if architecture in {"x86_64", "amd64"}:
        return cast(ReleasePlatform, _LINUX_AMD64)
    if architecture in {"aarch64", "arm64"}:
        return cast(ReleasePlatform, _LINUX_ARM64)
    raise LifecycleError.stable_failure(
        code="unsupported_release_architecture",
        cause=f"Official releases do not support host architecture {architecture or 'unknown'}.",
        remediation="Run Flow Steward on a Linux amd64 or arm64 host.",
        step="release_platform",
    )


class ReleaseUpgradeRequest(_ClosedModel):
    """Closed public inputs for a forward-only official release activation."""

    target_version: str = ""
    dry_run: bool = False
    skip_db_backup: bool = False
    timeout_seconds: int = Field(default=1800, ge=1, le=3600)
    # ADR 0082 §5: the deployment manager's prepared platform-module set.
    # None keeps the installed set; an empty tuple removes every module.
    target_modules: ModuleSet | None = None
    # ADR 0082 §5: end an unfinished module-only change by activating the
    # journal's previous module set again and restarting the roles, without
    # touching the database. Valid only after the operator restored the
    # pre-upgrade backup or confirmed that no migration of the new set was
    # applied. `flow-steward release modules-apply --restore-previous` sets it.
    restore_previous_modules: bool = False

    @model_validator(mode="after")
    def validate_target_version(self) -> Self:
        if self.target_version:
            ReleaseVersion.parse(self.target_version)
        return self


class ReleaseModulesListRequest(_ClosedModel):
    """Read-only request for the active and staged platform modules (ADR 0082 §5)."""


class ReleaseModulesApplyRequest(_ClosedModel):
    """Make staged platform modules the active set through the journaled upgrade.

    ``modules`` names ``id@version`` items separated by commas; empty selects the
    highest staged version of every staged module. ``restore_previous`` instead ends an
    unfinished module-only change by activating its previous set again.
    """

    modules: str = Field(default="", max_length=4096)
    restore_previous: bool = False

    @model_validator(mode="after")
    def validate_choice(self) -> Self:
        if self.restore_previous and self.modules.strip():
            raise ValueError("restore_previous restores the journal's set; name no modules")
        return self


class ReleaseModulesResult(_ClosedModel):
    """The active platform-module set next to what the deployment manager staged."""

    status: Literal["listed"] = "listed"
    deployment_profile: DeploymentProfile | None = None
    activation_supported: bool = False
    detail: str = ""
    active_modules: tuple[PreparedModuleRef, ...] = ()
    staged_modules: tuple[PreparedModuleRef, ...] = ()
    next_steps: tuple[str, ...] = ()


class ReleaseTransitionBinding(_ClosedModel):
    """Immutable source origin and exact official target approved by review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    installed_origin: SourceInstallationOrigin
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_repository: str
    deployment_profile: DeploymentProfile
    platform: ReleasePlatform
    target_release: ReleaseIdentity

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        origin = self.installed_origin
        if (
            self.configuration_sha256 != origin.configuration_sha256
            or self.release_repository != origin.release_repository
            or self.deployment_profile != origin.deployment_profile
            or self.platform != origin.platform
            or self.target_release.profile != self.deployment_profile
            or self.target_release.platform != self.platform
        ):
            raise ValueError("source transition binding fields must describe one exact context")
        return self


class PreparedReleaseUpgrade(_ClosedModel):
    """Exact, immutable release selection approved by the operator review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    installed_release: ReleaseIdentity | None
    target_release: ReleaseIdentity
    transition_binding: ReleaseTransitionBinding | None = None
    release_source: str
    deployment_profile: DeploymentProfile
    platform: ReleasePlatform
    dry_run: bool = False
    skip_db_backup: bool = False
    timeout_seconds: int = Field(
        default=ReleaseUpgradeRequest.model_fields["timeout_seconds"].default, ge=1, le=3600
    )
    target_modules: ModuleSet | None = None
    # Carried from ReleaseUpgradeRequest for one run; never a reason to resume.
    restore_previous_modules: bool = False

    @model_validator(mode="after")
    def validate_origin_binding(self) -> Self:
        if (self.installed_release is None) == (self.transition_binding is None):
            raise ValueError(
                "prepared release upgrade must bind one official or source installed origin"
            )
        if self.transition_binding is not None:
            binding = self.transition_binding
            if (
                binding.target_release != self.target_release
                or binding.deployment_profile != self.deployment_profile
                or binding.platform != self.platform
            ):
                raise ValueError("prepared source transition does not match its exact target")
        return self


class ReleaseResult(_ClosedModel):
    """Secret-free result shared by info, check, and upgrade."""

    status: Literal[
        "installed",
        "current",
        "upgrade_available",
        "transition_available",
        "check_unavailable",
        "upgrade_pending",
        "planned",
        "already_current",
        "upgraded",
        "migration_required",
    ]
    active_environment: Literal["development", "production"] | None = None
    installed_origin: Literal["official", "source"] | None = None
    source_commit_sha: str = ""
    deployment_profile: DeploymentProfile | None = None
    platform: ReleasePlatform | None = None
    current_version: str = ""
    current_release: ReleaseIdentity | None = None
    target_version: str = ""
    target_release: ReleaseIdentity | None = None
    release_source: str = ""
    detail: str = ""
    remediation: str = ""
    dry_run: bool = False
    preflight: dict[str, object] = Field(default_factory=dict)
    operation_id: str | None = None
    log_path: str | None = None
    maintenance_status: MaintenanceStatusResult | None = None
    next_steps: tuple[str, ...] = ()


class ReleaseMaintenancePort(Protocol):
    def status(self) -> MaintenanceStatusResult: ...


class ReleaseActivatorPort(Protocol):
    def activate(
        self,
        release: ReleaseIdentity,
        *,
        timeout_seconds: int,
        **operation_sinks: object,
    ) -> ReleaseResult: ...
