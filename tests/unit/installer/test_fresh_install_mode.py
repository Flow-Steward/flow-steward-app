"""Fresh install: a published release tag installs official images, anything else builds."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from click.testing import CliRunner

from core.application.installer import (
    InstallationKind,
    InstallConfig,
    InstallRunResult,
    ReleaseIdentity,
    ReleaseImageIdentity,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.release.catalog import (
    PublishedRelease,
    ReleaseCoordinates,
    ReleaseNotFoundError,
    ReleaseVersion,
)
from core.application.release.images import required_product_roles
from core.entrypoints.cli import installer
from core.infrastructure.release.github import GitHubHTTPError

_REPOSITORY = "Flow-Steward/flow-steward-app"
_HEAD = "c" * 40
_OTHER = "d" * 40


def _coordinates(commit_sha: str = _HEAD) -> ReleaseCoordinates:
    return ReleaseCoordinates(
        version=ReleaseVersion.parse("0.9.1"),
        tag="v0.9.1",
        commit_sha=commit_sha,
        source="local tagged checkout",
        repository=_REPOSITORY,
    )


def _published(commit_sha: str = _HEAD) -> PublishedRelease:
    return PublishedRelease(
        version=ReleaseVersion.parse("0.9.1"),
        tag="v0.9.1",
        commit_sha=commit_sha,
        published_at="2026-10-27T00:00:00Z",
        source=f"https://github.com/{_REPOSITORY}/releases/tag/v0.9.1",
        repository=_REPOSITORY,
    )


def _identity(profile: str) -> ReleaseIdentity:
    digest = "sha256:" + "e" * 64
    return ReleaseIdentity(
        version="0.9.1",
        tag="v0.9.1",
        commit_sha=_HEAD,
        registry="ghcr.io/flow-steward",
        profile=profile,
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/flow-steward/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision=_HEAD,
                platform="linux/amd64",
            )
            for role in required_product_roles(profile)
        ),
    )


class _Checkout:
    """LocalCheckoutReleaseSource stand-in answering the fresh-install probe."""

    answer: ReleaseCoordinates | None = None
    probes: list[str] = []  # noqa: RUF012 - reset per test by the fixture

    def __init__(self, *, repo_root: Path, repository: str) -> None:
        self.repository = repository
        type(self).probes.append(repository)

    def probe(self) -> ReleaseCoordinates | None:
        return type(self).answer

    def exact(self) -> ReleaseCoordinates:
        raise AssertionError("a fresh install must not use the raising checkout check")


def _forbidden(*_args, **_kwargs):
    pytest.fail("a source install must not query GitHub or a registry")


@pytest.fixture
def fresh_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """A checkout with no installer state, an isolated runtime environment."""
    environment: dict[str, str] = {}
    monkeypatch.setattr(installer, "_ROOT", tmp_path)
    monkeypatch.setattr(installer, "installer_state_root", lambda _root: tmp_path / "state")
    monkeypatch.setattr(installer, "_runtime_env", environment)
    monkeypatch.setattr(installer.host_platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(_Checkout, "answer", None)
    monkeypatch.setattr(_Checkout, "probes", [])
    monkeypatch.setattr(installer, "LocalCheckoutReleaseSource", _Checkout)
    return environment


def _official_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    published: PublishedRelease | Exception,
    resolved: ReleaseIdentity | Exception | None = None,
) -> dict[str, object]:
    observed: dict[str, object] = {}

    class Discovery:
        def __init__(self, *, repository: str, fetch_json) -> None:
            self.repository = repository
            observed["repository"] = repository

        def for_version(self, version: str) -> PublishedRelease:
            observed["version"] = version
            if isinstance(published, Exception):
                raise published
            return published

    class Resolver:
        def probe(self, release, *, profile, platform):
            observed.update(release=release, profile=profile, platform=platform)
            if isinstance(resolved, Exception):
                raise resolved
            assert resolved is not None
            return resolved

    monkeypatch.setattr(installer, "GitHubReleaseDiscovery", Discovery)
    monkeypatch.setattr(installer, "OfficialReleaseResolver", Resolver)
    return observed


@pytest.mark.parametrize("discover", [True, False])
@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_checkout_not_at_its_release_tag_builds_from_source(
    monkeypatch: pytest.MonkeyPatch, fresh_tree: dict[str, str], profile: str, discover: bool
) -> None:
    monkeypatch.setattr(installer, "GitHubReleaseDiscovery", _forbidden)
    monkeypatch.setattr(installer, "OfficialReleaseResolver", _forbidden)

    release = installer._prepare_official_release(
        InstallConfig(environment="production", deployment_profile=profile), discover=discover
    )

    assert release is None
    assert fresh_tree == {"FS_INSTALL_MODE": "source"}
    # A resumed source installation is never re-decided from the checkout.
    assert _Checkout.probes == ([_REPOSITORY] if discover else [])


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_fresh_checkout_at_published_release_tag_installs_official_images(
    monkeypatch: pytest.MonkeyPatch, fresh_tree: dict[str, str], profile: str
) -> None:
    fresh_tree["FS_RELEASE_REPOSITORY"] = _REPOSITORY
    monkeypatch.setattr(_Checkout, "answer", _coordinates())
    target = _identity(profile)
    observed = _official_sources(monkeypatch, published=_published(), resolved=target)

    release = installer._prepare_official_release(
        InstallConfig(environment="production", deployment_profile=profile), discover=True
    )

    assert release == target
    assert observed["repository"] == _REPOSITORY
    assert observed["version"] == "0.9.1"
    assert observed["release"] == _published()
    assert observed["profile"] == profile
    assert observed["platform"] == "linux/amd64"
    assert fresh_tree["FS_INSTALL_MODE"] == "official"
    assert fresh_tree["FS_BUILD_SHA"] == _HEAD
    role_variables = {
        "compact-runtime": "FS_COMPACT_IMAGE",
        "fs-app": "FS_APP_IMAGE",
        "fs-worker": "FS_WORKER_IMAGE",
        "control-plane": "FS_CONTROL_IMAGE",
        "fs-cli": "FS_CLI_IMAGE",
        "fs-mcp": "MCP_IMAGE",
    }
    for image in target.images:
        assert fresh_tree[role_variables[image.role]] == image.registry_ref


@pytest.mark.parametrize(
    ("published", "resolved", "detail"),
    [
        (
            ReleaseNotFoundError(
                f"Published stable release 0.9.1 is unavailable from {_REPOSITORY}"
            ),
            None,
            "Published stable release 0.9.1 is unavailable",
        ),
        (
            GitHubHTTPError("GitHub Release discovery is temporarily unavailable"),
            None,
            "GitHub Release discovery is temporarily unavailable",
        ),
        (_published(_OTHER), None, f"published v0.9.1 is commit {_OTHER}"),
        (
            _published(),
            ValueError("No configured registry (ghcr, docker_hub) provides a complete image set"),
            "No configured registry (ghcr, docker_hub)",
        ),
        (_published(), OSError("docker: not found"), "docker: not found"),
    ],
    ids=["not-published", "github-unreachable", "tag-elsewhere", "images-missing", "no-docker"],
)
def test_release_tag_without_installable_images_fails_without_source_fallback(
    monkeypatch: pytest.MonkeyPatch,
    fresh_tree: dict[str, str],
    published: PublishedRelease | Exception,
    resolved: Exception | None,
    detail: str,
) -> None:
    monkeypatch.setattr(_Checkout, "answer", _coordinates())
    _official_sources(monkeypatch, published=published, resolved=resolved)

    config = InstallConfig(environment="production", deployment_profile="compact")
    with pytest.raises(LifecycleError) as raised:
        installer._prepare_official_release(config, discover=True)

    failure = raised.value
    assert failure.code == "release_images_unavailable"
    assert detail in failure.cause
    assert "v0.9.1" in failure.cause
    assert "Wait until v0.9.1 is published as a stable release" in failure.remediation
    assert "check out a commit that is not a release tag" in failure.remediation
    assert failure.retryable is True
    assert fresh_tree.get("FS_INSTALL_MODE") != "source"


def test_network_failure_names_its_underlying_cause(
    monkeypatch: pytest.MonkeyPatch, fresh_tree: dict[str, str]
) -> None:
    monkeypatch.setattr(_Checkout, "answer", _coordinates())
    unreachable = GitHubHTTPError("GitHub Release discovery is temporarily unavailable")
    unreachable.__cause__ = URLError("[Errno -3] Temporary failure in name resolution")
    _official_sources(monkeypatch, published=unreachable)

    config = InstallConfig(environment="production", deployment_profile="scalable")
    with pytest.raises(LifecycleError) as raised:
        installer._prepare_official_release(config, discover=True)

    assert "Temporary failure in name resolution" in raised.value.cause


class _RecordingService:
    def __init__(self) -> None:
        self.target_release: ReleaseIdentity | None = None
        self.installed = False

    def inspect(self):
        return SimpleNamespace(kind=InstallationKind.FRESH)

    def install(self, config, explicit_fields=frozenset(), target_release=None):
        self.installed = True
        self.target_release = target_release
        return InstallRunResult(
            classification=InstallationKind.FRESH,
            status="installed",
            product_url="https://localhost:8099",
            operation_id="op-1",
            log_path=".flow-steward/logs/install-op-1.log",
        )


@pytest.fixture
def cli_service(monkeypatch: pytest.MonkeyPatch, fresh_tree: dict[str, str]) -> _RecordingService:
    service = _RecordingService()
    monkeypatch.setattr(installer, "_create_service", lambda **_kwargs: service)
    monkeypatch.setattr(installer, "_recorded_installation_values", lambda _root: {})
    return service


def test_installer_says_it_installs_the_official_release(
    monkeypatch: pytest.MonkeyPatch, cli_service: _RecordingService
) -> None:
    monkeypatch.setattr(_Checkout, "answer", _coordinates())
    target = _identity("scalable")
    _official_sources(monkeypatch, published=_published(), resolved=target)

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--confirm", "--plain", "--deployment-profile", "scalable"],
    )

    assert result.exit_code == 0, result.output
    assert cli_service.target_release == target
    assert (
        "installing official release v0.9.1 from ghcr.io/flow-steward "
        "(no local product image build)"
    ) in result.output


def test_installer_says_it_builds_from_source(
    monkeypatch: pytest.MonkeyPatch, cli_service: _RecordingService
) -> None:
    monkeypatch.setattr(installer, "GitHubReleaseDiscovery", _forbidden)
    monkeypatch.setattr(installer, "OfficialReleaseResolver", _forbidden)

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--confirm", "--plain", "--deployment-profile", "scalable"],
    )

    assert result.exit_code == 0, result.output
    assert cli_service.target_release is None
    assert "building product images from this checkout" in result.output


def test_unpublished_release_tag_stops_before_any_installation(
    monkeypatch: pytest.MonkeyPatch, cli_service: _RecordingService
) -> None:
    monkeypatch.setattr(_Checkout, "answer", _coordinates())
    _official_sources(
        monkeypatch,
        published=ReleaseNotFoundError("Published stable release 0.9.1 is unavailable"),
    )

    result = CliRunner().invoke(
        installer.installer_command,
        ["--non-interactive", "--confirm", "--plain", "--deployment-profile", "scalable"],
    )

    assert result.exit_code == int(installer.ExitCode.DEPENDENCY), result.output
    assert "release_images_unavailable" in result.output
    assert not cli_service.installed
