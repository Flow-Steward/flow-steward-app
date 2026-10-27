from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from core.application.installer import ReleaseIdentity, ReleaseImageIdentity
from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.activation import ReleaseActivator, ReleaseHandoffJournal
from core.application.release.catalog import (
    PublishedRelease,
    ReleaseDiscoveryError,
    ReleaseError,
    ReleaseNotFoundError,
    ReleaseVersion,
)
from core.application.release.images import required_product_roles
from core.application.release.installed_origin import (
    OfficialInstallationOrigin,
    SourceInstallationOrigin,
)
from core.application.release.service import (
    OfficialReleaseSelector,
    PreparedReleaseUpgrade,
    ReleaseCheckRequest,
    ReleaseInfoRequest,
    ReleaseInstallRequest,
    ReleaseResult,
    ReleaseService,
    ReleaseTransitionBinding,
    ReleaseUpgradeRequest,
    release_identity_from_resolution,
)
from core.infrastructure.installer.registry_image_resolver import (
    OfficialReleaseResolver,
    RegistryImageIdentity,
)


def _identity(
    version: str,
    marker: str,
    platform: str = "linux/amd64",
    profile: str = "scalable",
) -> ReleaseIdentity:
    digest = f"sha256:{marker * 64}"
    return ReleaseIdentity(
        version=version,
        tag=f"v{version}",
        commit_sha=marker * 40,
        registry="ghcr.io/flow-steward",
        profile=profile,
        platform=platform,
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/flow-steward/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision=marker * 40,
                platform=platform,
            )
            for role in required_product_roles(profile)
        ),
    )


def _published(version: str, marker: str) -> PublishedRelease:
    return PublishedRelease(
        version=ReleaseVersion.parse(version),
        tag=f"v{version}",
        commit_sha=marker * 40,
        published_at="2026-09-25T10:00:00Z",
        source=f"https://github.com/Flow-Steward/flow-steward-app/releases/tag/v{version}",
        repository="Flow-Steward/flow-steward-app",
    )


def test_release_identity_persists_selected_child_reference_with_distinct_oci_digests() -> None:
    published = _published("1.0.10", "2")
    index_digest = "sha256:" + "a" * 64
    manifest_digest = "sha256:" + "b" * 64
    config_digest = "sha256:" + "c" * 64

    identities = tuple(
        RegistryImageIdentity(
            registry_ref=f"ghcr.io/flow-steward/{role.repository}@{manifest_digest}",
            index_digest=index_digest,
            manifest_digest=manifest_digest,
            config_digest=config_digest,
            revision=published.commit_sha,
            role=role.role,
            platform="linux/amd64",
            local_image_id=manifest_digest,
        )
        for role in required_product_roles("compact")
    )
    release = release_identity_from_resolution(
        published,
        registry="ghcr.io/flow-steward",
        profile="compact",
        platform="linux/amd64",
        identities=identities,
    )

    assert release.images[0].registry_ref.endswith("@" + manifest_digest)
    assert release.images[0].index_digest == index_digest
    assert release.images[0].manifest_digest == manifest_digest
    assert release.images[0].config_digest == config_digest


def test_release_identity_rejects_incomplete_or_cross_profile_role_sets() -> None:
    digest = "sha256:" + "a" * 64
    with pytest.raises(ValidationError, match="complete scalable role set"):
        ReleaseIdentity.model_validate(
            {
                "version": "1.0.10",
                "tag": "v1.0.10",
                "commit_sha": "a" * 40,
                "registry": "ghcr.io/flow-steward",
                "platform": "linux/amd64",
                "profile": "scalable",
                "images": [
                    {
                        "role": "compact-runtime",
                        "registry_ref": "ghcr.io/example/compact@" + digest,
                        "index_digest": digest,
                        "manifest_digest": digest,
                        "config_digest": digest,
                        "revision": "a" * 40,
                        "platform": "linux/amd64",
                    }
                ],
            }
        )


def test_release_identity_rejects_images_from_a_different_registry() -> None:
    release = _identity("1.0.10", "2")
    payload = release.model_dump(mode="json")
    payload["images"][0]["registry_ref"] = payload["images"][0]["registry_ref"].replace(
        "ghcr.io/flow-steward/", "docker.io/other/"
    )

    with pytest.raises(ValidationError, match="selected registry"):
        ReleaseIdentity.model_validate(payload)


def test_release_identity_rejects_digest_reference_with_trailing_data() -> None:
    release = _identity("1.0.10", "2")
    payload = release.model_dump(mode="json")
    payload["images"][0]["registry_ref"] += ".junk"

    with pytest.raises(ValidationError, match="exactly pinned"):
        ReleaseIdentity.model_validate(payload)


def test_release_identity_rejects_multiple_digest_separators() -> None:
    release = _identity("1.0.10", "2")
    payload = release.model_dump(mode="json")
    payload["images"][0]["registry_ref"] = (
        "ghcr.io/flow-steward/app@unexpected@" + payload["images"][0]["manifest_digest"]
    )

    with pytest.raises(ValidationError, match="exactly pinned"):
        ReleaseIdentity.model_validate(payload)


def test_exact_release_verifies_manifest_identity_without_remote_discovery() -> None:
    archived = _identity("1.0.10", "2")
    discovery = FakeDiscovery({}, fail=True)
    resolver = FakeResolver({})
    selector = OfficialReleaseSelector(
        discovery=discovery,
        state=FakeState(installed=None),
        resolver=resolver,
        profile="scalable",
        platform="linux/amd64",
    )

    assert selector.for_exact_identity(archived) == archived
    assert resolver.verify_calls == [archived]


def test_release_service_rejects_pending_identity_for_a_different_profile() -> None:
    harness = Harness()
    harness.state.pending = _identity("1.0.10", "2", profile="compact")
    request = ReleaseUpgradeRequest()

    with pytest.raises(LifecycleError) as raised:
        harness.service.prepare_upgrade(request)

    assert raised.value.code == "release_target_context_mismatch"


@dataclass
class FakeDiscovery:
    releases: dict[str, PublishedRelease]
    fail: bool = False

    def latest(self) -> PublishedRelease:
        if self.fail:
            raise ReleaseError("rate limited")
        return max(self.releases.values(), key=lambda item: item.version)

    def for_version(self, version: str) -> PublishedRelease:
        if self.fail:
            raise ReleaseError("unavailable")
        return self.releases[version]


@dataclass
class FakeState:
    installed: ReleaseIdentity | None
    pending: ReleaseIdentity | None = None
    handoff: object | None = None
    transition_binding: ReleaseTransitionBinding | None = None

    def load_installed_release(self) -> ReleaseIdentity | None:
        return self.installed

    def load_pending_release(self) -> ReleaseIdentity | None:
        return self.pending

    def load_release_handoff(self):
        return self.handoff

    def load_release_transition_binding(self):
        return self.transition_binding


@dataclass
class FakeResolver:
    identities: dict[str, ReleaseIdentity]
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    probe_calls: list[tuple[str, str, str]] = field(default_factory=list)
    verify_calls: list[ReleaseIdentity] = field(default_factory=list)
    fail: bool = False

    def resolve(self, release: PublishedRelease, *, profile: str, platform: str) -> ReleaseIdentity:
        self.calls.append((str(release.version), profile, platform))
        if self.fail:
            raise ValueError("official images incomplete")
        return self.identities[str(release.version)]

    def probe(self, release: PublishedRelease, *, profile: str, platform: str) -> ReleaseIdentity:
        self.probe_calls.append((str(release.version), profile, platform))
        if self.fail:
            raise ValueError("official images incomplete")
        return self.identities[str(release.version)]

    def verify(self, release: ReleaseIdentity) -> ReleaseIdentity:
        self.verify_calls.append(release)
        if self.fail:
            raise ValueError("official images incomplete")
        return release

    def verify_exact_identity(self, release: ReleaseIdentity) -> ReleaseIdentity:
        return self.verify(release)


@dataclass
class FakeMaintenance:
    phase: str = "enabled"

    def status(self) -> MaintenanceStatusResult:
        return MaintenanceStatusResult(status=self.phase)


@dataclass
class FakeActivator:
    calls: list[ReleaseIdentity] = field(default_factory=list)
    transition_bindings: list[ReleaseTransitionBinding | None] = field(default_factory=list)
    result_status: str = "upgraded"

    def activate(
        self, release: ReleaseIdentity, *, timeout_seconds: int, **_sinks: object
    ) -> ReleaseResult:
        assert timeout_seconds > 0
        self.calls.append(release)
        self.transition_bindings.append(
            _sinks.get("transition_binding")
            if isinstance(_sinks.get("transition_binding"), ReleaseTransitionBinding)
            else None
        )
        if self.result_status == "migration_required":
            return ReleaseResult(
                status="migration_required",
                remediation=(
                    "Create a backup, run `flow-steward database upgrade "
                    "--maintenance-confirmed --confirm`, then repeat release upgrade."
                ),
            )
        return ReleaseResult(status="upgraded")


@dataclass
class FakeOrigin:
    value: OfficialInstallationOrigin | SourceInstallationOrigin | None = None
    failure: LifecycleError | None = None
    calls: int = 0

    def load(self):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        assert self.value is not None
        return self.value


@dataclass
class Harness:
    profile: str = "scalable"
    installed: ReleaseIdentity = field(default_factory=lambda: _identity("1.0.9", "1"))
    target: ReleaseIdentity = field(default_factory=lambda: _identity("1.0.10", "2"))

    def __post_init__(self) -> None:
        self.discovery = FakeDiscovery(
            {
                self.installed.version: _published(self.installed.version, "1"),
                self.target.version: _published(self.target.version, "2"),
            }
        )
        self.state = FakeState(self.installed)
        self.resolver = FakeResolver(
            {self.installed.version: self.installed, self.target.version: self.target}
        )
        self.maintenance = FakeMaintenance()
        self.activator = FakeActivator()
        self.origin = FakeOrigin(value=OfficialInstallationOrigin(release=self.installed))
        self.service = ReleaseService(
            discovery=self.discovery,
            state=self.state,
            resolver=self.resolver,
            maintenance=self.maintenance,
            activator=self.activator,
            profile=self.profile,
            platform="linux/amd64",
            origin=self.origin,
        )


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_fresh_install_check_and_upgrade_share_release_selection(
    profile: str,
) -> None:
    harness = Harness(
        profile=profile,
        installed=_identity("1.0.9", "1", profile=profile),
        target=_identity("1.0.10", "2", profile=profile),
    )

    fresh = harness.service.prepare_install(ReleaseInstallRequest(discover_latest=True))
    checked = harness.service.check(ReleaseCheckRequest())
    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest())

    assert fresh == harness.target
    assert checked.target_release == harness.target
    assert prepared.target_release == harness.target
    assert harness.resolver.probe_calls == [
        ("1.0.10", profile, "linux/amd64"),
        ("1.0.10", profile, "linux/amd64"),
        ("1.0.10", profile, "linux/amd64"),
    ]
    assert harness.resolver.calls == []


def test_existing_install_reuses_and_verifies_durable_release_without_discovery() -> None:
    harness = Harness()
    harness.discovery.fail = True

    selected = harness.service.prepare_install(ReleaseInstallRequest(discover_latest=False))

    assert selected == harness.installed
    assert harness.resolver.verify_calls == [harness.installed]
    assert harness.resolver.calls == []
    assert harness.resolver.probe_calls == []


def test_existing_profile_switch_resolves_same_version_through_shared_selector() -> None:
    installed = _identity("1.0.10", "2", profile="compact")
    target = _identity("1.0.10", "2", profile="scalable")
    harness = Harness(profile="scalable", installed=installed, target=target)
    harness.resolver.identities[installed.version] = target

    selected = harness.service.prepare_install(ReleaseInstallRequest(discover_latest=False))

    assert selected == target
    assert harness.resolver.probe_calls == [("1.0.10", "scalable", "linux/amd64")]
    assert harness.resolver.verify_calls == []


def test_existing_profile_switch_builds_valid_coordinates_for_real_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = _identity("1.0.10", "2", profile="compact")
    target = _identity("1.0.10", "2", profile="scalable")
    resolver = OfficialReleaseResolver()
    observed = []

    def resolve(coordinates, *, profile, platform):
        observed.append((coordinates, profile, platform))
        return target

    monkeypatch.setattr(resolver, "_resolve", resolve)
    selector = OfficialReleaseSelector(
        discovery=FakeDiscovery({}, fail=True),
        state=FakeState(installed=installed),
        resolver=resolver,
        profile="scalable",
        platform="linux/amd64",
    )

    assert selector.prepare_install(ReleaseInstallRequest(discover_latest=False)) == target
    coordinates, profile, platform = observed[0]
    assert coordinates.repository == "Flow-Steward/flow-steward-app"
    assert coordinates.commit_sha == installed.commit_sha
    assert (profile, platform) == ("scalable", "linux/amd64")


@pytest.mark.parametrize("profile", ["compact", "scalable"])
@pytest.mark.parametrize("repository", [None, "example/private-releases"])
def test_profile_switch_passes_valid_coordinates_to_official_resolver(
    monkeypatch: pytest.MonkeyPatch, profile: str, repository: str | None
) -> None:
    installed_profile = "scalable" if profile == "compact" else "compact"
    installed = _identity("1.0.10", "2", profile=installed_profile)
    target = _identity("1.0.10", "2", profile=profile)
    discovery = FakeDiscovery({}, fail=True)
    if repository is not None:
        monkeypatch.setattr(discovery, "repository", repository, raising=False)
    resolver = OfficialReleaseResolver()
    observed = []

    def resolve_coordinates(coordinates, *, profile, platform):
        observed.append(coordinates)
        assert profile == target.profile
        assert platform == target.platform
        return target

    monkeypatch.setattr(resolver, "_resolve", resolve_coordinates)
    selector = OfficialReleaseSelector(
        discovery=discovery,
        state=FakeState(installed=installed),
        resolver=resolver,
        profile=profile,
        platform="linux/amd64",
    )

    assert selector.prepare_install(ReleaseInstallRequest(discover_latest=False)) == target
    assert observed[0].repository == (repository or "Flow-Steward/flow-steward-app")
    assert observed[0].commit_sha == installed.commit_sha


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_info_and_check_use_durable_state_for_both_profiles(profile: str) -> None:
    harness = Harness(
        profile=profile,
        installed=_identity("1.0.9", "1", profile=profile),
        target=_identity("1.0.10", "2", profile=profile),
    )

    info = harness.service.info(ReleaseInfoRequest())
    check = harness.service.check(ReleaseCheckRequest())

    assert info.status == "installed"
    assert info.current_version == "1.0.9"
    assert info.current_release == harness.installed
    assert check.status == "upgrade_available"
    assert check.target_version == "1.0.10"
    assert harness.resolver.calls == []
    assert harness.resolver.probe_calls == [("1.0.10", profile, "linux/amd64")]


def _source_origin(commit_sha: str = "c" * 40) -> SourceInstallationOrigin:
    return SourceInstallationOrigin(
        commit_sha=commit_sha,
        deployment_profile="scalable",
        platform="linux/amd64",
        installation_id="flow-steward-scalable",
        configuration_sha256="d" * 64,
        release_repository="Flow-Steward/flow-steward-app",
    )


def _source_harness(commit_sha: str = "c" * 40) -> tuple[Harness, FakeOrigin]:
    harness = Harness()
    harness.state.installed = None
    origin = FakeOrigin(value=_source_origin(commit_sha))
    harness.discovery.repository = origin.value.release_repository
    harness.service = ReleaseService(
        discovery=harness.discovery,
        state=harness.state,
        resolver=harness.resolver,
        maintenance=harness.maintenance,
        activator=harness.activator,
        profile="scalable",
        platform="linux/amd64",
        origin=origin,
    )
    return harness, origin


def test_source_info_reports_commit_without_fabricating_official_identity() -> None:
    harness, origin = _source_harness()

    result = harness.service.info(ReleaseInfoRequest())

    assert result.status == "installed"
    assert result.installed_origin == "source"
    assert result.source_commit_sha == "c" * 40
    assert result.current_version == ""
    assert result.current_release is None
    assert result.deployment_profile == "scalable"
    assert result.platform == "linux/amd64"
    assert origin.calls == 1
    assert harness.discovery.fail is False
    assert harness.resolver.probe_calls == []
    assert harness.activator.calls == []


def test_clone_prerequisite_returns_official_identity_or_typed_source_refusal() -> None:
    official = Harness()
    source, _origin = _source_harness()

    assert official.service.require_official_installation() == official.installed
    with pytest.raises(LifecycleError) as raised:
        source.service.require_official_installation()

    assert raised.value.code == "clone_official_release_required"
    assert "release check" in raised.value.remediation
    assert "maintenance" in raised.value.remediation
    assert "release upgrade" in raised.value.remediation


@pytest.mark.parametrize(
    ("relationship", "source_commit"),
    (
        ("equal", "2" * 40),
        ("ancestor", "1" * 40),
        ("descendant", "3" * 40),
        ("unrelated", "4" * 40),
    ),
)
def test_source_check_always_reports_explicit_transition_with_unknown_ordering(
    relationship: str,
    source_commit: str,
) -> None:
    harness, origin = _source_harness(source_commit)

    result = harness.service.check(ReleaseCheckRequest())

    assert relationship in {"equal", "ancestor", "descendant", "unrelated"}
    assert result.status == "transition_available"
    assert result.installed_origin == "source"
    assert result.source_commit_sha == source_commit
    assert result.current_version == ""
    assert result.current_release is None
    assert result.target_release == harness.target
    assert result.target_version == harness.target.version
    assert "ordering" in result.detail.lower()
    assert "release upgrade" in result.remediation
    assert origin.calls == 1
    assert harness.resolver.probe_calls == [("1.0.10", "scalable", "linux/amd64")]
    assert harness.resolver.calls == []
    assert harness.resolver.verify_calls == []
    assert harness.activator.calls == []


@pytest.mark.parametrize(
    "failure",
    (
        ReleaseDiscoveryError(
            "Private release is unavailable without authentication",
            code="release_github_private_not_found",
            remediation="Install an owner-only GitHub token.",
            retryable=False,
        ),
        ReleaseDiscoveryError(
            "GitHub Release authentication failed",
            code="release_github_authentication_failed",
            remediation="Replace the invalid owner-only GitHub token.",
            retryable=False,
        ),
        ReleaseDiscoveryError(
            "GitHub rate limit is exhausted",
            code="release_github_rate_limited",
            remediation="Retry after the GitHub rate limit resets.",
            retryable=True,
        ),
        ReleaseNotFoundError("Published release v0.3.0 is unavailable"),
    ),
)
def test_source_check_preserves_typed_discovery_failures_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    failure: ReleaseDiscoveryError,
) -> None:
    harness, _origin = _source_harness()

    def fail():
        raise failure

    monkeypatch.setattr(harness.discovery, "latest", fail)

    result = harness.service.check(ReleaseCheckRequest())

    assert result.status == "check_unavailable"
    assert result.installed_origin == "source"
    assert result.source_commit_sha == "c" * 40
    assert result.detail == str(failure)
    assert result.remediation == failure.remediation
    assert harness.resolver.probe_calls == []
    assert harness.activator.calls == []


def test_source_check_distinguishes_invalid_installed_provenance_from_discovery() -> None:
    harness, origin = _source_harness()
    origin.failure = LifecycleError.stable_failure(
        code="release_installed_origin_invalid",
        cause="Installed source provenance is inconsistent.",
        remediation="Repair the installed provenance.",
        step="release_state",
    )
    request = ReleaseCheckRequest()

    with pytest.raises(LifecycleError) as raised:
        harness.service.check(request)

    assert raised.value.code == "release_installed_origin_invalid"
    assert harness.resolver.probe_calls == []
    assert harness.activator.calls == []


def test_source_check_reports_incomplete_official_images_without_activation() -> None:
    harness, _origin = _source_harness()
    harness.resolver.fail = True

    result = harness.service.check(ReleaseCheckRequest())

    assert result.status == "check_unavailable"
    assert result.installed_origin == "source"
    assert "official images incomplete" in result.detail
    assert harness.resolver.probe_calls == [("1.0.10", "scalable", "linux/amd64")]
    assert harness.activator.calls == []


@pytest.mark.parametrize("operation", ("check", "prepare_upgrade"))
def test_source_release_refuses_discovery_repository_different_from_durable_origin(
    operation: str,
) -> None:
    harness, origin = _source_harness()
    harness.discovery.repository = "example/other-releases"
    harness.service = ReleaseService(
        discovery=harness.discovery,
        state=harness.state,
        resolver=harness.resolver,
        maintenance=harness.maintenance,
        activator=harness.activator,
        profile="scalable",
        platform="linux/amd64",
        origin=origin,
    )

    def invoke_operation() -> object:
        if operation == "check":
            return harness.service.check(ReleaseCheckRequest())
        return harness.service.prepare_upgrade(ReleaseUpgradeRequest())

    with pytest.raises(LifecycleError) as raised:
        invoke_operation()

    assert raised.value.code == "release_source_repository_mismatch"
    assert harness.resolver.probe_calls == []
    assert harness.activator.calls == []


@pytest.mark.parametrize("requested", ("", "1.0.10"))
def test_source_upgrade_preparation_binds_origin_and_exact_target(requested: str) -> None:
    harness, origin = _source_harness("2" * 40)

    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest(target_version=requested))
    review = harness.service.review_upgrade(prepared)

    assert prepared.installed_release is None
    assert prepared.transition_binding == ReleaseTransitionBinding(
        installed_origin=origin.value,
        configuration_sha256="d" * 64,
        release_repository="Flow-Steward/flow-steward-app",
        deployment_profile="scalable",
        platform="linux/amd64",
        target_release=harness.target,
    )
    assert prepared.target_release == harness.target
    assert review["installed_origin"] == "source"
    assert review["source_commit_sha"] == "2" * 40
    assert review["semantic_ordering"] == "unknown"
    assert review["installation_mode_change"] == "source -> official"
    assert review["target_version"] == "1.0.10"
    assert review["target_tag"] == "v1.0.10"
    assert review["target_commit_sha"] == "2" * 40
    assert review["target_images"] == [
        image.model_dump(mode="json") for image in harness.target.images
    ]
    assert review["resume_command"] == (
        "flow-steward release upgrade --target-version 1.0.10 --confirm"
    )
    assert harness.activator.calls == []


def test_source_dry_run_revalidates_binding_but_never_activates() -> None:
    harness, origin = _source_harness()

    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest(dry_run=True))
    result = harness.service.upgrade(prepared)

    assert result.status == "planned"
    assert result.installed_origin == "source"
    assert result.source_commit_sha == "c" * 40
    assert result.current_release is None
    assert result.target_release == harness.target
    assert origin.calls == 2
    assert harness.activator.calls == []


@pytest.mark.parametrize(
    "changed_origin",
    (
        _source_origin().model_copy(update={"configuration_sha256": "e" * 64}),
        _source_origin().model_copy(update={"release_repository": "example/other"}),
        _source_origin().model_copy(update={"installation_id": "other-installation"}),
        _source_origin().model_copy(update={"deployment_profile": "compact"}),
        _source_origin().model_copy(update={"platform": "linux/arm64"}),
        _source_origin().model_copy(update={"commit_sha": "e" * 40}),
    ),
)
def test_source_upgrade_refuses_origin_or_configuration_drift_before_activation(
    changed_origin: SourceInstallationOrigin,
) -> None:
    harness, origin = _source_harness()
    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest())
    origin.value = changed_origin

    with pytest.raises(LifecycleError) as raised:
        harness.service.upgrade(prepared)

    assert raised.value.code == "release_upgrade_state_changed"
    assert harness.activator.calls == []


def test_source_upgrade_refuses_reviewed_target_drift_before_activation() -> None:
    harness, _origin = _source_harness()
    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest())
    assert prepared.transition_binding is not None
    changed = prepared.model_copy(
        update={
            "target_release": _identity("1.0.11", "3"),
        }
    )

    with pytest.raises(LifecycleError) as raised:
        harness.service.upgrade(changed)

    assert raised.value.code == "release_upgrade_state_changed"
    assert harness.activator.calls == []


def test_confirmed_source_upgrade_passes_reviewed_binding_to_activator() -> None:
    harness, _origin = _source_harness()
    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest())

    result = harness.service.upgrade(prepared)

    assert result.status == "upgraded"
    assert harness.activator.calls == [harness.target]
    assert harness.activator.transition_bindings == [prepared.transition_binding]


def test_release_activator_forwards_source_binding_to_handoff() -> None:
    harness, _origin = _source_harness()
    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest())
    observed: list[tuple[ReleaseIdentity, ReleaseTransitionBinding | None]] = []

    class Handoff:
        def activate(
            self,
            release,
            *,
            timeout_seconds,
            held_locks=None,
            transition_binding=None,
        ):
            assert timeout_seconds == 90
            assert held_locks is None
            observed.append((release, transition_binding))
            return ReleaseResult(status="upgraded")

    result = ReleaseActivator(Handoff()).activate(
        harness.target,
        timeout_seconds=90,
        transition_binding=prepared.transition_binding,
    )

    assert result.status == "upgraded"
    assert observed == [(harness.target, prepared.transition_binding)]


def test_source_transition_resume_uses_durable_target_without_origin_or_discovery() -> None:
    harness, origin = _source_harness()
    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest())
    assert prepared.transition_binding is not None
    harness.state.pending = harness.target
    harness.state.transition_binding = prepared.transition_binding
    harness.state.handoff = ReleaseHandoffJournal(
        target=harness.target,
        phase="checkout_applied",
    )
    origin.failure = LifecycleError.stable_failure(
        code="release_installed_origin_invalid",
        cause="Post-checkout runtime no longer looks like source.",
        remediation="Resume the durable transition.",
        step="release_state",
    )
    harness.discovery.fail = True

    resumed = harness.service.prepare_upgrade(ReleaseUpgradeRequest())
    result = harness.service.upgrade(resumed)

    assert resumed.target_release == harness.target
    assert resumed.transition_binding == prepared.transition_binding
    assert resumed.release_source == "durable source transition handoff"
    assert result.status == "upgraded"
    assert origin.calls == 1
    assert harness.resolver.verify_calls == [harness.target]
    assert harness.activator.transition_bindings == [prepared.transition_binding]


def test_source_transition_resume_uses_binding_durable_before_the_journal() -> None:
    harness, origin = _source_harness()
    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest())
    assert prepared.transition_binding is not None
    harness.state.transition_binding = prepared.transition_binding
    origin.failure = LifecycleError.stable_failure(
        code="release_installed_origin_invalid",
        cause="A crash occurred after binding but before the handoff journal.",
        remediation="Resume the exact durable transition.",
        step="release_state",
    )
    harness.discovery.fail = True

    resumed = harness.service.prepare_upgrade(ReleaseUpgradeRequest())

    assert resumed.target_release == harness.target
    assert resumed.transition_binding == prepared.transition_binding
    assert resumed.release_source == "durable source transition handoff"
    assert harness.resolver.verify_calls == [harness.target]


def test_check_compares_semantic_versions_not_strings() -> None:
    harness = Harness(
        installed=_identity("1.0.10", "2"),
        target=_identity("1.0.10", "2"),
    )
    harness.discovery.releases["1.0.9"] = _published("1.0.9", "1")

    result = harness.service.check(ReleaseCheckRequest())

    assert result.status == "current"


def test_check_unavailable_is_typed_actionable_and_non_mutating() -> None:
    harness = Harness()
    harness.discovery.fail = True

    result = harness.service.check(ReleaseCheckRequest())

    assert result.status == "check_unavailable"
    assert "rate limited" in result.detail
    assert "Retry" in result.remediation
    assert harness.resolver.calls == []
    assert harness.resolver.probe_calls == []
    assert harness.activator.calls == []


def test_typed_discovery_failure_preserves_remediation_for_check_and_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    failure = ReleaseDiscoveryError(
        "GitHub Release authentication failed",
        code="release_github_authentication_failed",
        remediation="Install a valid owner-only token.",
        retryable=False,
    )

    def fail():
        raise failure

    monkeypatch.setattr(harness.discovery, "latest", fail)
    checked = harness.service.check(ReleaseCheckRequest())

    assert checked.status == "check_unavailable"
    assert checked.detail == "GitHub Release authentication failed"
    assert checked.remediation == "Install a valid owner-only token."

    request = ReleaseUpgradeRequest()
    with pytest.raises(LifecycleError) as raised:
        harness.service.prepare_upgrade(request)

    assert raised.value.code == "release_github_authentication_failed"
    assert raised.value.remediation == "Install a valid owner-only token."
    assert raised.value.retryable is False


@pytest.mark.parametrize(
    "failure",
    (
        ReleaseNotFoundError("Published release v0.3.0 is unavailable"),
        ReleaseDiscoveryError(
            "GitHub Releases endpoint must return a JSON list",
            code="release_github_contract_invalid",
            remediation="Correct the published GitHub release metadata before retrying.",
            retryable=False,
        ),
    ),
)
def test_public_release_entrypoints_preserve_terminal_discovery_contracts(
    monkeypatch: pytest.MonkeyPatch,
    failure: ReleaseDiscoveryError,
) -> None:
    harness = Harness()

    def fail():
        raise failure

    monkeypatch.setattr(harness.discovery, "latest", fail)
    checked = harness.service.check(ReleaseCheckRequest())

    assert checked.status == "check_unavailable"
    assert checked.detail == str(failure)
    assert checked.remediation == failure.remediation

    request = ReleaseUpgradeRequest()
    with pytest.raises(LifecycleError) as raised:
        harness.service.prepare_upgrade(request)

    assert raised.value.code == failure.code
    assert raised.value.remediation == failure.remediation
    assert raised.value.retryable is False


def test_check_fails_closed_for_installed_or_resolved_context_mismatch() -> None:
    wrong_installed = Harness(
        profile="compact",
        installed=_identity("1.0.9", "1", profile="scalable"),
        target=_identity("1.0.10", "2", profile="compact"),
    )
    request = ReleaseCheckRequest()
    with pytest.raises(LifecycleError) as installed_error:
        wrong_installed.service.check(request)
    assert installed_error.value.code == "release_installed_context_mismatch"

    wrong_target = Harness()
    wrong_target.resolver.identities[wrong_target.target.version] = _identity(
        "1.0.10", "2", profile="compact"
    )
    with pytest.raises(LifecycleError) as target_error:
        wrong_target.service.check(request)
    assert target_error.value.code == "release_target_context_mismatch"


def test_pending_target_wins_over_checkout_or_newer_discovery() -> None:
    harness = Harness()
    harness.state.pending = harness.target

    result = harness.service.check(ReleaseCheckRequest())

    assert result.status == "upgrade_pending"
    assert result.current_version == "1.0.9"
    assert result.target_version == "1.0.10"
    assert harness.resolver.calls == []


def test_pending_upgrade_reverifies_exact_identity_before_activation() -> None:
    harness = Harness()
    harness.state.pending = harness.target
    harness.resolver.fail = True
    request = ReleaseUpgradeRequest()

    with pytest.raises(ValueError, match="official images incomplete"):
        harness.service.prepare_upgrade(request)

    assert harness.resolver.verify_calls == [harness.target]
    assert harness.discovery.fail is False


def test_upgrade_refuses_disabled_maintenance_before_image_resolution() -> None:
    harness = Harness()
    harness.maintenance.phase = "disabled"
    request = ReleaseUpgradeRequest()

    with pytest.raises(LifecycleError) as raised:
        harness.service.upgrade(request)

    assert raised.value.code == "release_maintenance_required"
    assert harness.resolver.calls == []
    assert harness.activator.calls == []


def test_public_upgrade_refuses_semantic_downgrade_before_mutation() -> None:
    harness = Harness(
        installed=_identity("1.0.10", "2"),
        target=_identity("1.0.9", "1"),
    )
    request = ReleaseUpgradeRequest(target_version="1.0.9")

    with pytest.raises(LifecycleError) as raised:
        harness.service.upgrade(request)

    assert raised.value.code == "release_downgrade_refused"
    assert harness.resolver.calls == []
    assert harness.activator.calls == []


def test_upgrade_allows_exact_installed_version_as_idempotent_noop() -> None:
    installed = _identity("1.0.10", "2")
    harness = Harness(installed=installed, target=installed)
    harness.maintenance.phase = "disabled"

    result = harness.service.upgrade(ReleaseUpgradeRequest(target_version="1.0.10"))

    assert result.status == "already_current"
    assert harness.resolver.calls == []
    assert harness.activator.calls == []


def test_upgrade_ignores_crash_stale_pending_marker_for_installed_release() -> None:
    harness = Harness()
    harness.state.pending = harness.installed

    result = harness.service.upgrade(ReleaseUpgradeRequest(target_version=harness.target.version))

    assert result.status == "upgraded"
    assert harness.resolver.probe_calls == [(harness.target.version, "scalable", "linux/amd64")]
    assert harness.activator.calls == [harness.target]


def test_upgrade_resumes_exact_pending_identity_without_reresolution() -> None:
    harness = Harness()
    harness.state.pending = harness.target

    result = harness.service.upgrade(ReleaseUpgradeRequest(target_version="1.0.10"))

    assert result.status == "upgraded"
    assert result.current_release == harness.target
    assert result.current_version == harness.target.version
    assert harness.resolver.calls == []
    assert harness.activator.calls == [harness.target]


def test_upgrade_resumes_durable_pending_identity_when_discovery_is_unavailable() -> None:
    harness = Harness()
    harness.state.pending = harness.target
    harness.discovery.fail = True

    result = harness.service.upgrade(ReleaseUpgradeRequest())

    assert result.status == "upgraded"
    assert result.release_source == "durable pending release identity"
    assert harness.resolver.calls == []
    assert harness.resolver.probe_calls == []
    assert harness.activator.calls == [harness.target]


def test_prepared_upgrade_binds_execution_to_reviewed_release_without_rediscovery() -> None:
    harness = Harness()

    prepared = harness.service.prepare_upgrade(ReleaseUpgradeRequest())
    assert isinstance(prepared, PreparedReleaseUpgrade)
    assert prepared.target_release == harness.target
    assert harness.resolver.probe_calls == [("1.0.10", "scalable", "linux/amd64")]
    harness.discovery.releases["1.0.11"] = _published("1.0.11", "3")

    result = harness.service.upgrade(prepared)

    assert result.status == "upgraded"
    assert harness.activator.calls == [harness.target]
    assert harness.resolver.probe_calls == [("1.0.10", "scalable", "linux/amd64")]
    assert harness.resolver.calls == []


def test_upgrade_review_has_one_exact_migration_policy_statement() -> None:
    harness = Harness()

    review = harness.service.review_upgrade(
        harness.service.prepare_upgrade(ReleaseUpgradeRequest())
    )

    assert review["migration_policy"] == (
        "Installer prechecks migrations and stops before service recreation when one "
        "is required; backup and database upgrade remain explicit operator commands."
    )


@pytest.mark.parametrize(
    "target",
    (
        _identity("1.0.10", "2", profile="compact"),
        _identity("1.0.10", "2", platform="linux/arm64"),
    ),
)
def test_prepared_upgrade_revalidates_target_context_before_activation(
    target: ReleaseIdentity,
) -> None:
    harness = Harness()
    prepared = PreparedReleaseUpgrade(
        installed_release=harness.installed,
        target_release=target,
        release_source="durable review",
        deployment_profile="scalable",
        platform="linux/amd64",
    )

    with pytest.raises(LifecycleError) as raised:
        harness.service.upgrade(prepared)

    assert raised.value.code == "release_target_context_mismatch"
    assert harness.activator.calls == []


def test_prepared_upgrade_revalidates_forward_only_policy_before_activation() -> None:
    installed = _identity("1.0.10", "2")
    older = _identity("1.0.9", "1")
    harness = Harness(installed=installed, target=older)
    prepared = PreparedReleaseUpgrade(
        installed_release=installed,
        target_release=older,
        release_source="durable review",
        deployment_profile="scalable",
        platform="linux/amd64",
    )

    with pytest.raises(LifecycleError) as raised:
        harness.service.upgrade(prepared)

    assert raised.value.code == "release_downgrade_refused"
    assert harness.activator.calls == []


def test_upgrade_returns_actionable_migration_stop_and_keeps_maintenance_enabled() -> None:
    harness = Harness()
    harness.activator.result_status = "migration_required"

    result = harness.service.upgrade(ReleaseUpgradeRequest())

    assert result.status == "migration_required"
    assert "database upgrade" in result.remediation
    assert result.current_release == harness.installed
    assert result.target_release == harness.target
    assert result.release_source.endswith("/releases/tag/v1.0.10")
    assert result.deployment_profile == "scalable"
    assert result.platform == "linux/amd64"
    assert result.maintenance_status.status == "enabled"
    assert harness.maintenance.status().status == "enabled"


def test_dry_run_resolves_exact_target_but_never_activates() -> None:
    harness = Harness()

    result = harness.service.upgrade(ReleaseUpgradeRequest(dry_run=True))

    assert result.status == "planned"
    assert result.target_release == harness.target
    assert harness.activator.calls == []
