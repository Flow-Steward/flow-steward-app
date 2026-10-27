"""Shared read-only release status and forward-only upgrade policy."""

from __future__ import annotations

from typing import Any, Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.catalog import (
    DEFAULT_RELEASE_REPOSITORY,
    PublishedRelease,
    ReleaseDiscoveryError,
    ReleaseVersion,
    validated_release_repository,
)
from core.application.release.contracts import (
    ReleaseIdentity,
    release_identity_from_resolution,
)
from core.application.release.installed_origin import (
    InstalledReleaseOrigin,
    InstalledReleaseOriginPort,
    SourceInstallationOrigin,
)

_MAINTENANCE_ENABLE_COMMAND = "flow-steward maintenance enable --confirm"
_MAINTENANCE_DISABLE_COMMAND = "flow-steward maintenance disable --confirm"
_BACKUP_CREATE_COMMAND = "flow-steward backup create"
_DATABASE_UPGRADE_COMMAND = "flow-steward database upgrade --maintenance-confirmed --confirm"
_LINUX_AMD64 = "linux/amd64"
_LINUX_ARM64 = "linux/arm64"
_SUPPORTED_RELEASE_PLATFORMS = frozenset({_LINUX_AMD64, _LINUX_ARM64})
_TARGET_CONTEXT_MISMATCH_CAUSE = (
    "Resolved release identity does not match the active profile and platform."
)
_TARGET_CONTEXT_MISMATCH_REMEDIATION = (
    "Resolve the release again for this installation and host platform."
)
_INSTALLED_CONTEXT_MISMATCH_REMEDIATION = (
    "Repair the active installation configuration or release state."
)
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
    timeout_seconds: int = Field(default=60, ge=1, le=3600)

    @model_validator(mode="after")
    def validate_target_version(self) -> Self:
        if self.target_version:
            ReleaseVersion.parse(self.target_version)
        return self


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
    timeout_seconds: int = Field(default=60, ge=1, le=3600)

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


class ReleaseDiscoveryPort(Protocol):
    def latest(self) -> PublishedRelease: ...

    def for_version(self, version: str) -> PublishedRelease: ...


class ReleaseStatePort(Protocol):
    def load_installed_release(self) -> ReleaseIdentity | None: ...

    def load_pending_release(self) -> ReleaseIdentity | None: ...


class ReleaseResolverPort(Protocol):
    def resolve(
        self,
        release: PublishedRelease,
        *,
        profile: DeploymentProfile,
        platform: ReleasePlatform,
    ) -> ReleaseIdentity: ...

    def verify(self, release: ReleaseIdentity) -> ReleaseIdentity: ...

    def verify_exact_identity(self, release: ReleaseIdentity) -> ReleaseIdentity: ...


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


class OfficialReleaseSelector:
    """Canonical discovery and immutable-image selection for every release consumer."""

    def __init__(
        self,
        *,
        discovery: ReleaseDiscoveryPort,
        state: ReleaseStatePort,
        resolver: ReleaseResolverPort,
        profile: DeploymentProfile,
        platform: ReleasePlatform,
    ) -> None:
        self.discovery = discovery
        self.state = state
        self.resolver = resolver
        self.profile = profile
        self.platform = platform
        self.release_repository = validated_release_repository(
            str(getattr(discovery, "repository", DEFAULT_RELEASE_REPOSITORY))
        )

    def latest(self) -> tuple[ReleaseIdentity, str]:
        published = self.discovery.latest()
        return self.resolve(published), published.source

    def for_version(self, version: str) -> tuple[ReleaseIdentity, str]:
        published = self.discovery.for_version(version)
        return self.resolve(published), published.source

    def for_exact_identity(self, identity: ReleaseIdentity) -> ReleaseIdentity:
        """Verify the manifest's exact digest identity without remote discovery."""
        resolved = self.resolver.verify_exact_identity(identity)
        self.require_context(resolved)
        if not _equivalent_official_release_content(resolved, identity):
            raise LifecycleError.stable_failure(
                code="release_exact_image_set_mismatch",
                cause="The official complete image set does not match the archived exact release.",
                remediation="Restore only after the archived exact image set is available.",
                step="release_policy",
            )
        return resolved

    def resolve(self, published: PublishedRelease) -> ReleaseIdentity:
        probe = getattr(self.resolver, "probe", None) or self.resolver.resolve
        target = probe(published, profile=self.profile, platform=self.platform)
        self.require_context(target)
        return target

    def verify(self, release: ReleaseIdentity) -> ReleaseIdentity:
        target = self.resolver.verify(release)
        self.require_context(target)
        return target

    def prepare_install(self, request: ReleaseInstallRequest) -> ReleaseIdentity:
        installed = self.state.load_installed_release()
        pending = self.effective_pending(installed)
        if pending is not None and not request.discover_latest and installed is not None:
            raise LifecycleError.stable_failure(
                code="release_upgrade_pending",
                cause=f"Release {pending.version} is pending and cannot be resumed by install.sh.",
                remediation=(
                    f"Enable maintenance and run `flow-steward release upgrade "
                    f"--target-version {pending.version} --confirm`."
                ),
                step="release_policy",
            )
        if pending is not None:
            return self.verify(pending)
        if request.discover_latest:
            target, _source = self.latest()
            return target
        if installed is None:
            raise LifecycleError.stable_failure(
                code="release_installed_state_missing",
                cause="The existing production installation has no verified release identity.",
                remediation="Repair installed release state before changing this installation.",
                step="release_state",
            )
        if installed.profile == self.profile and installed.platform == self.platform:
            return self.verify(installed)
        if installed.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_installed_context_mismatch",
                cause="Installed release identity does not match the active host platform.",
                remediation=_INSTALLED_CONTEXT_MISMATCH_REMEDIATION,
                step="release_state",
            )
        published = PublishedRelease(
            version=ReleaseVersion.parse(installed.version),
            tag=installed.tag,
            commit_sha=installed.commit_sha,
            published_at="",
            source="durable installed release identity",
            repository=self.release_repository,
        )
        return self.resolve(published)

    def effective_pending(self, installed: ReleaseIdentity | None) -> ReleaseIdentity | None:
        pending = self.state.load_pending_release()
        return None if pending == installed else pending

    def require_context(self, target: ReleaseIdentity) -> None:
        if target.profile != self.profile or target.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_target_context_mismatch",
                cause=_TARGET_CONTEXT_MISMATCH_CAUSE,
                remediation=_TARGET_CONTEXT_MISMATCH_REMEDIATION,
                step="release_policy",
            )


def _equivalent_official_release_content(
    left: ReleaseIdentity,
    right: ReleaseIdentity,
) -> bool:
    """Compare immutable release content while allowing an official mirror change."""
    if (
        left.version,
        left.tag,
        left.commit_sha,
        left.profile,
        left.platform,
    ) != (
        right.version,
        right.tag,
        right.commit_sha,
        right.profile,
        right.platform,
    ):
        return False

    def content(release: ReleaseIdentity) -> tuple[tuple[str, str, str, str, str, str], ...]:
        return tuple(
            sorted(
                (
                    image.role,
                    image.index_digest,
                    image.manifest_digest,
                    image.config_digest,
                    image.revision,
                    image.platform,
                )
                for image in release.images
            )
        )

    return content(left) == content(right)


class ReleaseService:
    """Apply one policy to Compact and Scalable around shared release resolution."""

    def __init__(
        self,
        *,
        discovery: ReleaseDiscoveryPort,
        state: ReleaseStatePort,
        resolver: ReleaseResolverPort,
        maintenance: ReleaseMaintenancePort,
        activator: ReleaseActivatorPort,
        profile: DeploymentProfile,
        platform: ReleasePlatform,
        origin: InstalledReleaseOriginPort,
    ) -> None:
        if profile not in {"compact", "scalable"}:
            raise ValueError("unsupported deployment profile")
        if platform not in _SUPPORTED_RELEASE_PLATFORMS:
            raise ValueError("unsupported release platform")
        self.discovery = discovery
        self.state = state
        self.resolver = resolver
        self.maintenance = maintenance
        self.activator = activator
        self.profile = profile
        self.platform = platform
        self.origin = origin
        self.selector = OfficialReleaseSelector(
            discovery=discovery,
            state=state,
            resolver=resolver,
            profile=profile,
            platform=platform,
        )

    def prepare_install(self, request: ReleaseInstallRequest) -> ReleaseIdentity:
        return self.selector.prepare_install(request)

    def require_official_installation(self) -> ReleaseIdentity:
        """Return the exact official identity required by portable Clone."""
        origin = self._load_origin()
        if origin.kind == "official":
            return origin.release
        raise LifecycleError.stable_failure(
            code="clone_official_release_required",
            cause="Portable Clone requires a verified official release installation.",
            remediation=(
                "Run `flow-steward release check`, enable maintenance, and complete "
                "`flow-steward release upgrade --confirm` before using Clone."
            ),
            step="clone_prerequisite",
        )

    def info(self, request: ReleaseInfoRequest) -> ReleaseResult:
        del request
        origin = self._load_origin()
        if origin.kind == "official":
            return self._result(status="installed", installed=origin.release)
        return self._result(
            status="installed",
            installed=None,
            installed_origin="source",
            source_commit_sha=origin.commit_sha,
        )

    def check(self, request: ReleaseCheckRequest) -> ReleaseResult:
        """Compare durable applied state without mutating Git, Docker, or lifecycle state."""
        del request
        origin = self._load_origin()
        installed = origin.release if origin.kind == "official" else None
        if origin.kind == "source":
            self._require_source_repository(origin)
        pending = self._effective_pending(installed) if installed is not None else None
        if installed is not None and pending is not None and pending != installed:
            self._require_target_context(pending)
            return self._result(
                status="upgrade_pending",
                installed=installed,
                target=pending,
                detail=f"Release {pending.version} is prepared but not fully applied.",
                remediation=(
                    f"Keep maintenance enabled and repeat `flow-steward release upgrade "
                    f"--target-version {pending.version} --confirm`."
                ),
            )
        try:
            target, source = self.selector.latest()
        except LifecycleError:
            raise
        except ReleaseDiscoveryError as exc:
            return self._result(
                status="check_unavailable",
                installed=installed,
                installed_origin=origin.kind,
                source_commit_sha=origin.commit_sha if origin.kind == "source" else "",
                detail=str(exc),
                remediation=exc.remediation,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return self._result(
                status="check_unavailable",
                installed=installed,
                installed_origin=origin.kind,
                source_commit_sha=origin.commit_sha if origin.kind == "source" else "",
                detail=f"Stable release check is unavailable: {exc}",
                remediation="Retry after GitHub and an official registry are reachable.",
            )
        if origin.kind == "source":
            return self._result(
                status="transition_available",
                installed=None,
                installed_origin="source",
                source_commit_sha=origin.commit_sha,
                target=target,
                source=source,
                detail=(
                    "The installed source commit and published release have unknown semantic "
                    "ordering; an explicit transition to the verified official release is "
                    "available."
                ),
                remediation=(
                    f"Enable maintenance, then run `flow-steward release upgrade "
                    f"--target-version {target.version} --confirm`."
                ),
            )
        status: Literal["current", "upgrade_available"] = "upgrade_available"
        if installed is not None and ReleaseVersion.parse(target.version) <= ReleaseVersion.parse(
            installed.version
        ):
            status = "current"
        return self._result(
            status=status,
            installed=installed,
            target=target,
            source=source,
            remediation=(
                "No release action is required."
                if status == "current"
                else f"Enable maintenance, then run `flow-steward release upgrade "
                f"--target-version {target.version} --confirm`."
            ),
        )

    def prepare_upgrade(self, request: ReleaseUpgradeRequest) -> PreparedReleaseUpgrade:
        resumed = self._resume_source_transition(request)
        if resumed is not None:
            return resumed
        origin = self._load_origin()
        if origin.kind == "source":
            self._require_source_repository(origin)
            target, source = self._select_source_transition(request)
            binding = ReleaseTransitionBinding(
                installed_origin=origin,
                configuration_sha256=origin.configuration_sha256,
                release_repository=origin.release_repository,
                deployment_profile=self.profile,
                platform=self.platform,
                target_release=target,
            )
            return PreparedReleaseUpgrade(
                installed_release=None,
                target_release=target,
                transition_binding=binding,
                release_source=source,
                deployment_profile=self.profile,
                platform=self.platform,
                dry_run=request.dry_run,
                timeout_seconds=request.timeout_seconds,
            )
        installed, target, source = self._select_upgrade(request)
        return PreparedReleaseUpgrade(
            installed_release=installed,
            target_release=target,
            release_source=source,
            deployment_profile=self.profile,
            platform=self.platform,
            dry_run=request.dry_run,
            timeout_seconds=request.timeout_seconds,
        )

    def review_upgrade(self, prepared: PreparedReleaseUpgrade) -> dict[str, object]:
        installed = prepared.installed_release
        target = prepared.target_release
        if prepared.transition_binding is not None:
            origin = prepared.transition_binding.installed_origin
            return {
                "status": "ready",
                "installed_origin": "source",
                "source_commit_sha": origin.commit_sha,
                "semantic_ordering": "unknown",
                "installation_mode_change": "source -> official",
                "target_version": target.version,
                "target_tag": target.tag,
                "target_commit_sha": target.commit_sha,
                "target_images": [image.model_dump(mode="json") for image in target.images],
                "deployment_profile": prepared.deployment_profile,
                "platform": prepared.platform,
                "release_source": prepared.release_source,
                "maintenance": "enabled (left enabled after success or failure)",
                "migration_policy": (
                    "Installer prechecks migrations and stops before service recreation when one "
                    "is required; backup and database upgrade remain explicit operator commands."
                ),
                "resume_command": (
                    f"flow-steward release upgrade --target-version {target.version} --confirm"
                ),
                "next_steps": [_MAINTENANCE_DISABLE_COMMAND],
            }
        assert installed is not None
        return {
            "status": "already_current" if target == installed else "ready",
            "installed_version": installed.version,
            "target_version": target.version,
            "deployment_profile": prepared.deployment_profile,
            "platform": prepared.platform,
            "release_source": prepared.release_source,
            "maintenance": "enabled (left enabled after success or failure)",
            "migration_policy": (
                "Installer prechecks migrations and stops before service recreation when one "
                "is required; backup and database upgrade remain explicit operator commands."
            ),
            "next_steps": [_MAINTENANCE_DISABLE_COMMAND],
        }

    def plan_upgrade(self, request: ReleaseUpgradeRequest) -> dict[str, object]:
        """Compatibility wrapper for callers that only need a review document."""
        return self.review_upgrade(self.prepare_upgrade(request))

    def upgrade(
        self,
        request: ReleaseUpgradeRequest | PreparedReleaseUpgrade,
        **operation_sinks: object,
    ) -> ReleaseResult:
        prepared = (
            request
            if isinstance(request, PreparedReleaseUpgrade)
            else self.prepare_upgrade(request)
        )
        target = prepared.target_release
        binding = prepared.transition_binding
        durable_transition = self._load_transition_handoff()
        durable_binding = self._load_transition_binding()
        if (
            binding is not None
            and durable_binding == binding
            and (
                durable_transition is None
                or (
                    getattr(durable_transition, "target", None) == target
                    and getattr(durable_transition, "phase", "") != "installer_completed"
                )
            )
        ):
            origin: InstalledReleaseOrigin = binding.installed_origin
        else:
            origin = self.origin.load()
        if binding is not None:
            self._require_source_repository(binding.installed_origin)
            if (
                origin.kind != "source"
                or origin != binding.installed_origin
                or target != binding.target_release
                or prepared.installed_release is not None
            ):
                raise self._upgrade_state_changed()
            installed = None
        else:
            if (
                origin.kind != "official"
                or prepared.installed_release is None
                or origin.release != prepared.installed_release
            ):
                raise self._upgrade_state_changed()
            installed = origin.release
        self._require_target_context(target)
        if installed is not None and ReleaseVersion.parse(target.version) < ReleaseVersion.parse(
            installed.version
        ):
            raise self._downgrade_refused(installed.version, target.version)
        source = prepared.release_source
        pending = self._effective_pending(installed) if installed is not None else None
        if pending is not None and pending != target:
            raise LifecycleError.stable_failure(
                code="release_upgrade_pending",
                cause=f"Release {pending.version} is already pending.",
                remediation=(
                    f"Resume it with --target-version {pending.version} before selecting another."
                ),
                step="release_policy",
            )
        if installed is not None and target == installed:
            return self._result(
                status="already_current",
                installed=installed,
                target=target,
                source=source,
                maintenance=self.maintenance.status(),
                remediation="The requested release is already installed.",
            )
        if self.maintenance.status().status != "enabled":
            raise self._maintenance_required()
        if prepared.dry_run:
            return self._result(
                status="planned",
                installed=installed,
                installed_origin=origin.kind,
                source_commit_sha=origin.commit_sha if origin.kind == "source" else "",
                target=target,
                source=source,
                maintenance=self.maintenance.status(),
                remediation="Rerun without --dry-run to activate this exact release.",
                dry_run=True,
            )
        if binding is not None:
            operation_sinks["transition_binding"] = binding
        result = self.activator.activate(
            target, timeout_seconds=prepared.timeout_seconds, **operation_sinks
        )
        next_steps = result.next_steps
        if not next_steps and result.status == "migration_required":
            next_steps = (
                _BACKUP_CREATE_COMMAND,
                _DATABASE_UPGRADE_COMMAND,
                f"flow-steward release upgrade --target-version {target.version} --confirm",
            )
        elif not next_steps and result.status == "upgraded":
            next_steps = (_MAINTENANCE_DISABLE_COMMAND,)
        current = target if result.status == "upgraded" else installed
        return result.model_copy(
            update={
                "current_release": result.current_release or current,
                "current_version": result.current_version or (current.version if current else ""),
                "installed_origin": ("official" if result.status == "upgraded" else origin.kind),
                "source_commit_sha": (
                    origin.commit_sha
                    if result.status != "upgraded" and origin.kind == "source"
                    else ""
                ),
                "target_release": result.target_release or target,
                "target_version": result.target_version or target.version,
                "release_source": result.release_source or source,
                "deployment_profile": result.deployment_profile or self.profile,
                "platform": result.platform or self.platform,
                "maintenance_status": result.maintenance_status or self.maintenance.status(),
                "next_steps": next_steps,
            }
        )

    def _select_source_transition(
        self, request: ReleaseUpgradeRequest
    ) -> tuple[ReleaseIdentity, str]:
        if self.maintenance.status().status != "enabled":
            raise self._maintenance_required()
        try:
            published = (
                self.discovery.for_version(request.target_version)
                if request.target_version
                else self.discovery.latest()
            )
        except ReleaseDiscoveryError as exc:
            factory = LifecycleError.dependency if exc.retryable else LifecycleError.stable_failure
            raise factory(
                code=exc.code,
                cause=str(exc),
                remediation=exc.remediation,
                step="release_discovery",
            ) from exc
        return self.selector.resolve(published), published.source

    def _resume_source_transition(
        self, request: ReleaseUpgradeRequest
    ) -> PreparedReleaseUpgrade | None:
        journal = self._load_transition_handoff()
        binding = self._load_transition_binding()
        journal_target = getattr(journal, "target", None)
        phase = getattr(journal, "phase", "")
        if not isinstance(binding, ReleaseTransitionBinding) or phase == "installer_completed":
            return None
        target = binding.target_release
        if journal_target is not None and journal_target != target:
            raise self._upgrade_state_changed()
        self._require_source_repository(binding.installed_origin)
        requested = request.target_version
        if requested and requested != target.version:
            raise LifecycleError.stable_failure(
                code="release_upgrade_pending",
                cause=f"Release {target.version} is already pending.",
                remediation=(
                    f"Resume it with --target-version {target.version} before selecting another."
                ),
                step="release_policy",
            )
        verified = self.selector.verify(target)
        if verified != target:
            raise self._upgrade_state_changed()
        return PreparedReleaseUpgrade(
            installed_release=None,
            target_release=target,
            transition_binding=binding,
            release_source="durable source transition handoff",
            deployment_profile=binding.deployment_profile,
            platform=binding.platform,
            dry_run=request.dry_run,
            timeout_seconds=request.timeout_seconds,
        )

    def _load_transition_handoff(self) -> object | None:
        load = getattr(self.state, "load_release_handoff", None)
        return load() if callable(load) else None

    def _load_transition_binding(self) -> object | None:
        load = getattr(self.state, "load_release_transition_binding", None)
        return load() if callable(load) else None

    def _require_source_repository(self, origin: SourceInstallationOrigin) -> None:
        if origin.release_repository == self.selector.release_repository:
            return
        raise LifecycleError.stable_failure(
            code="release_source_repository_mismatch",
            cause=(
                "The configured release repository differs from the repository bound to "
                "the installed source origin."
            ),
            remediation=(
                "Restore FS_RELEASE_REPOSITORY to the installed durable value, then rerun "
                "release check and review the transition again."
            ),
            step="release_policy",
        )

    def _select_upgrade(
        self, request: ReleaseUpgradeRequest
    ) -> tuple[ReleaseIdentity, ReleaseIdentity, str]:
        installed = self._require_installed()
        pending = self._effective_pending(installed)
        requested = request.target_version
        if pending is not None and (not requested or requested == pending.version):
            target = self.selector.verify(pending)
            source = "durable pending release identity"
        else:
            try:
                published = (
                    self.discovery.for_version(requested) if requested else self.discovery.latest()
                )
            except ReleaseDiscoveryError as exc:
                factory = (
                    LifecycleError.dependency if exc.retryable else LifecycleError.stable_failure
                )
                raise factory(
                    code=exc.code,
                    cause=str(exc),
                    remediation=exc.remediation,
                    step="release_discovery",
                ) from exc
            target_version = published.version
            if target_version < ReleaseVersion.parse(installed.version):
                raise self._downgrade_refused(installed.version, str(target_version))
            if str(target_version) == installed.version and pending is None:
                return installed, installed, published.source
            if pending is not None and pending.version != str(target_version):
                raise LifecycleError.stable_failure(
                    code="release_upgrade_pending",
                    cause=f"Release {pending.version} is already pending.",
                    remediation=(
                        f"Resume it with --target-version {pending.version} before selecting another."
                    ),
                    step="release_policy",
                )
            if self.maintenance.status().status != "enabled":
                raise self._maintenance_required()
            target = self.selector.resolve(published)
            source = published.source
        if target.version != installed.version and self.maintenance.status().status != "enabled":
            raise self._maintenance_required()
        if ReleaseVersion.parse(target.version) < ReleaseVersion.parse(installed.version):
            raise self._downgrade_refused(installed.version, target.version)
        if target.profile != self.profile or target.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_target_context_mismatch",
                cause=_TARGET_CONTEXT_MISMATCH_CAUSE,
                remediation=_TARGET_CONTEXT_MISMATCH_REMEDIATION,
                step="release_policy",
            )
        return installed, target, source

    def _effective_pending(self, installed: ReleaseIdentity) -> ReleaseIdentity | None:
        """Treat a crash-stale marker for the installed identity as already committed."""
        return self.selector.effective_pending(installed)

    def _require_installed(self) -> ReleaseIdentity:
        installed = self.state.load_installed_release()
        if installed is None:
            raise LifecycleError.stable_failure(
                code="release_installed_state_missing",
                cause="No successfully applied release identity is recorded.",
                remediation="Complete an official installation before using release commands.",
                step="release_state",
            )
        if installed.profile != self.profile or installed.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_installed_context_mismatch",
                cause="Installed release identity does not match the active profile and platform.",
                remediation=_INSTALLED_CONTEXT_MISMATCH_REMEDIATION,
                step="release_state",
            )
        return installed

    def _load_origin(self) -> InstalledReleaseOrigin:
        origin = self.origin.load()
        if origin.kind == "official":
            installed = origin.release
            if installed.profile != self.profile or installed.platform != self.platform:
                raise LifecycleError.stable_failure(
                    code="release_installed_context_mismatch",
                    cause=(
                        "Installed release identity does not match the active profile and platform."
                    ),
                    remediation=_INSTALLED_CONTEXT_MISMATCH_REMEDIATION,
                    step="release_state",
                )
            return origin
        if origin.deployment_profile != self.profile or origin.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_installed_context_mismatch",
                cause="Installed source origin does not match the active profile and platform.",
                remediation="Repair the active installation configuration or source provenance.",
                step="release_state",
            )
        return origin

    def _require_target_context(self, target: ReleaseIdentity) -> None:
        if target.profile != self.profile or target.platform != self.platform:
            raise LifecycleError.stable_failure(
                code="release_target_context_mismatch",
                cause=_TARGET_CONTEXT_MISMATCH_CAUSE,
                remediation=_TARGET_CONTEXT_MISMATCH_REMEDIATION,
                step="release_policy",
            )

    def _result(
        self,
        *,
        status: Any,
        installed: ReleaseIdentity | None,
        target: ReleaseIdentity | None = None,
        source: str = "",
        detail: str = "",
        remediation: str = "",
        maintenance: MaintenanceStatusResult | None = None,
        dry_run: bool = False,
        installed_origin: Literal["official", "source"] | None = None,
        source_commit_sha: str = "",
    ) -> ReleaseResult:
        return ReleaseResult(
            status=status,
            installed_origin=installed_origin or ("official" if installed else None),
            source_commit_sha=source_commit_sha,
            deployment_profile=self.profile,
            platform=self.platform,
            current_version=installed.version if installed else "",
            current_release=installed,
            target_version=target.version if target else "",
            target_release=target,
            release_source=source,
            detail=detail,
            remediation=remediation,
            maintenance_status=maintenance,
            dry_run=dry_run,
            next_steps=(_MAINTENANCE_DISABLE_COMMAND,) if maintenance else (),
        )

    @staticmethod
    def _maintenance_required() -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_maintenance_required",
            cause="Release upgrade requires maintenance to be fully enabled.",
            remediation=f"Run `{_MAINTENANCE_ENABLE_COMMAND}`, then repeat the upgrade.",
            step="maintenance",
        )

    @staticmethod
    def _downgrade_refused(installed: str, target: str) -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_downgrade_refused",
            cause=(
                f"Public release upgrade cannot move from {installed} to older release {target}."
            ),
            remediation="Use clone restore for a validated destructive older-version restore.",
            step="release_policy",
        )

    @staticmethod
    def _upgrade_state_changed() -> LifecycleError:
        return LifecycleError.stable_failure(
            code="release_upgrade_state_changed",
            cause="Installed origin or the exact target changed after upgrade review.",
            remediation="Run the release upgrade command again and review the new plan.",
            step="release_policy",
        )


__all__ = [
    "OfficialReleaseSelector",
    "PreparedReleaseUpgrade",
    "ReleaseActivatorPort",
    "ReleaseCheckRequest",
    "ReleaseDiscoveryPort",
    "ReleaseInfoRequest",
    "ReleaseInstallRequest",
    "ReleaseResolverPort",
    "ReleaseResult",
    "ReleaseService",
    "ReleaseStatePort",
    "ReleaseTransitionBinding",
    "ReleaseUpgradeRequest",
    "release_identity_from_resolution",
    "release_platform_for_machine",
]
