"""The one discovery and immutable-image selection every release consumer uses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from core.application.lifecycle.errors import LifecycleError
from core.application.release.catalog import (
    DEFAULT_RELEASE_REPOSITORY,
    PublishedRelease,
    ReleaseVersion,
    validated_release_repository,
)
from core.application.release.contracts import ReleaseIdentity

if TYPE_CHECKING:
    from core.application.release.service import (
        DeploymentProfile,
        ReleaseInstallRequest,
        ReleasePlatform,
    )

_TARGET_CONTEXT_MISMATCH_CAUSE = (
    "Resolved release identity does not match the active profile and platform."
)
_TARGET_CONTEXT_MISMATCH_REMEDIATION = (
    "Resolve the release again for this installation and host platform."
)
_INSTALLED_CONTEXT_MISMATCH_REMEDIATION = (
    "Repair the active installation configuration or release state."
)


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
        leftover = self.profile_switch_leftover(installed)
        if leftover is not None and not request.discover_latest:
            # An interrupted profile switch, not a release upgrade: ./install.sh on the
            # profile it was switching to finishes it; on the installed profile the
            # leftover is superseded by the installed release staged for this run.
            if leftover.profile == self.profile:
                return self.verify(leftover)
            pending = None
        else:
            pending = self.effective_pending(installed)
        if pending is not None and not request.discover_latest and installed is not None:
            raise LifecycleError.stable_failure(
                code="release_upgrade_pending",
                cause=f"Release {pending.version} is pending and cannot be resumed by install.sh.",
                remediation=(
                    f"Run `flow-steward release upgrade "
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

    def profile_switch_leftover(self, installed: ReleaseIdentity | None) -> ReleaseIdentity | None:
        """The pending release an interrupted profile switch left, if that is what it is.

        A switch stages the installed release on the other profile: same version,
        commit and platform, other profile. It is no upgrade to resume.
        """
        pending = self.state.load_pending_release()
        if pending is None or installed is None:
            return None
        same_release = (pending.version, pending.commit_sha, pending.platform) == (
            installed.version,
            installed.commit_sha,
            installed.platform,
        )
        return pending if same_release and pending.profile != installed.profile else None

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


__all__ = [
    "OfficialReleaseSelector",
    "ReleaseDiscoveryPort",
    "ReleaseResolverPort",
    "ReleaseStatePort",
]
