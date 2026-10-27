from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.application.installer.contracts import InstallConfig, InstallJournal
from core.application.installer.errors import InstallerFailure
from core.application.lifecycle.errors import LifecycleError
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.release.installed_origin import InstalledReleaseOriginReader


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


@dataclass
class _State:
    installed: ReleaseIdentity | None = None
    pending: ReleaseIdentity | None = None
    handoff: object | None = None
    journal: InstallJournal | None = None
    installed_failure: Exception | None = None

    def load_installed_release(self):
        if self.installed_failure is not None:
            raise self.installed_failure
        return self.installed

    def load_pending_release(self):
        return self.pending

    def load_release_handoff(self):
        return self.handoff

    def load_journal(self):
        return self.journal


@dataclass
class _Configuration:
    active: InstallConfig | None
    environment: dict[str, str]

    def read_active(self):
        return self.active

    def read_durable_environment(self, config):
        assert config == self.active
        return dict(self.environment)


@dataclass
class _Provenance:
    revision: str = "c" * 40
    calls: int = 0

    def validated_source_revision(self, config: InstallConfig) -> str:
        assert config.environment == "production"
        self.calls += 1
        return self.revision


def _source_reader(
    *,
    profile: str = "compact",
    state: _State | None = None,
    environment: dict[str, str] | None = None,
    provenance: _Provenance | None = None,
    active: InstallConfig | None | object = ...,
    platform: str = "linux/amd64",
) -> tuple[InstalledReleaseOriginReader, _Provenance]:
    config = InstallConfig(environment="production", deployment_profile=profile)
    selected_state = state or _State(
        journal=InstallJournal(
            operation_id="source-install",
            status="completed",
            project_identity=f"flow-steward-{profile}",
            config=config,
            completed_steps=("manifest",),
        )
    )
    selected_provenance = provenance or _Provenance()
    values = {
        "FS_INSTALL_MODE": "source",
        "FS_BUILD_SHA": "c" * 40,
        "FS_COMPOSE_PROJECT_NAME": f"flow-steward-{profile}",
        "FS_STACK_ENVIRONMENT": "production",
        "FS_DEPLOYMENT_PROFILE": profile,
        "FS_RELEASE_REPOSITORY": "Flow-Steward/flow-steward-app",
        **(environment or {}),
    }
    return (
        InstalledReleaseOriginReader(
            state=selected_state,
            configuration=_Configuration(
                active=config if active is ... else active,
                environment=values,
            ),
            source_provenance=selected_provenance,
            platform=lambda: platform,
        ),
        selected_provenance,
    )


def test_official_identity_takes_precedence_without_source_fallback() -> None:
    release = _official_release()
    provenance = _Provenance()
    reader, provenance = _source_reader(
        state=_State(installed=release),
        provenance=provenance,
    )

    origin = reader.load()

    assert origin.kind == "official"
    assert origin.release == release
    assert provenance.calls == 0


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_completed_source_installation_returns_stable_installed_origin(profile: str) -> None:
    reader, provenance = _source_reader(profile=profile)

    first = reader.load()
    second = reader.load()

    assert first == second
    assert first.kind == "source"
    assert first.commit_sha == "c" * 40
    assert first.deployment_profile == profile
    assert first.platform == "linux/amd64"
    assert first.installation_id == f"flow-steward-{profile}"
    assert first.release_repository == "Flow-Steward/flow-steward-app"
    assert len(first.configuration_sha256) == 64
    assert provenance.calls == 2


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_source_origin_uses_current_configuration_after_supported_config_apply(
    profile: str,
) -> None:
    active = InstallConfig(
        environment="production",
        deployment_profile=profile,
        public_host="changed.example.test",
        public_port=9443,
        worker_processes=4,
    )
    reader, provenance = _source_reader(profile=profile, active=active)

    origin = reader.load()

    assert origin.kind == "source"
    assert origin.deployment_profile == profile
    assert (
        origin.configuration_sha256
        != _source_reader(profile=profile)[0].load().configuration_sha256
    )
    assert provenance.calls == 1


@pytest.mark.parametrize(
    ("state_update", "environment_update", "expected_code"),
    (
        ({"journal": None}, {}, "release_installed_state_missing"),
        ({"journal_status": "failed"}, {}, "release_installed_state_missing"),
        ({"pending": _official_release()}, {}, "release_installed_origin_invalid"),
        ({"handoff": object()}, {}, "release_installed_origin_invalid"),
        ({}, {"FS_INSTALL_MODE": "official"}, "release_installed_origin_invalid"),
        ({}, {"FS_BUILD_SHA": "d" * 40}, "release_installed_origin_invalid"),
        ({}, {"FS_COMPOSE_PROJECT_NAME": "other-install"}, "release_installed_origin_invalid"),
        ({}, {"FS_RELEASE_REPOSITORY": "invalid"}, "release_installed_origin_invalid"),
    ),
)
def test_missing_or_contradictory_state_never_silently_becomes_source(
    state_update: dict[str, object],
    environment_update: dict[str, str],
    expected_code: str,
) -> None:
    config = InstallConfig(environment="production", deployment_profile="compact")
    state_values = dict(state_update)
    journal_status = str(state_values.pop("journal_status", "completed"))
    state = _State(
        journal=InstallJournal(
            operation_id="source-install",
            status=journal_status,
            project_identity="flow-steward-compact",
            config=config,
            completed_steps=("manifest",),
        )
    )
    for name, value in state_values.items():
        setattr(state, name, value)
    reader, provenance = _source_reader(
        state=state,
        environment=environment_update,
    )

    with pytest.raises(LifecycleError) as raised:
        reader.load()

    assert raised.value.code == expected_code
    assert provenance.calls <= 1


@pytest.mark.parametrize(
    ("active", "platform"),
    (
        (None, "linux/amd64"),
        (InstallConfig(environment="production", deployment_profile="scalable"), "linux/amd64"),
        (..., "linux/s390x"),
    ),
)
def test_configuration_or_platform_drift_never_becomes_source(
    active: InstallConfig | None | object,
    platform: str,
) -> None:
    reader, _provenance = _source_reader(active=active, platform=platform)

    with pytest.raises(LifecycleError) as raised:
        reader.load()

    assert raised.value.code == "release_installed_origin_invalid"


def test_corrupt_official_state_fails_closed_before_source_provenance() -> None:
    provenance = _Provenance()
    reader, provenance = _source_reader(
        state=_State(installed_failure=ValueError("corrupt installed release")),
        provenance=provenance,
    )

    with pytest.raises(LifecycleError) as raised:
        reader.load()

    assert raised.value.code == "release_installed_origin_invalid"
    assert provenance.calls == 0


class _ImageProcess:
    def __init__(
        self,
        labels: dict[str, str],
        *,
        project_identity: str,
        running_images: dict[str, tuple[str, ...]],
    ) -> None:
        self.labels = labels
        self.project_identity = project_identity
        self.running_images = running_images

    def run(self, command: Sequence[str], **_kwargs: object) -> ProcessOutcome:
        argv = tuple(command)
        if argv[:3] == ("docker", "image", "inspect"):
            image = argv[-1]
            revision = self.labels.get(image)
            if revision is None:
                return ProcessOutcome(command=argv, returncode=1, stderr="missing image")
            return ProcessOutcome(
                command=argv,
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": image,
                            "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
                        }
                    ]
                ),
            )
        if argv[:3] == ("docker", "ps", "-q"):
            project_filter = next(
                (
                    value.removeprefix("label=com.docker.compose.project=")
                    for value in argv
                    if value.startswith("label=com.docker.compose.project=")
                ),
                "",
            )
            service_filter = next(
                (
                    value.removeprefix("label=com.docker.compose.service=")
                    for value in argv
                    if value.startswith("label=com.docker.compose.service=")
                ),
                "",
            )
            if project_filter != self.project_identity:
                return ProcessOutcome(command=argv, returncode=0)
            container_ids = tuple(
                f"{service_filter}-{index}"
                for index, _image in enumerate(self.running_images.get(service_filter, ()), start=1)
            )
            return ProcessOutcome(
                command=argv,
                returncode=0,
                stdout="\n".join(container_ids),
            )
        if argv[:3] == ("docker", "container", "inspect"):
            service = argv[-1].rsplit("-", 1)[0]
            return ProcessOutcome(
                command=argv,
                returncode=0,
                stdout="\n".join(self.running_images.get(service, ())),
            )
        return ProcessOutcome(command=argv, returncode=1, stderr="unexpected command")


_SOURCE_IMAGE_KEYS = {
    "compact-runtime": ("FS_COMPACT_IMAGE", "FS_COMPACT_IMAGE_REVISION"),
    "fs-app": ("FS_APP_IMAGE", "FS_APP_IMAGE_REVISION"),
    "fs-worker": ("FS_WORKER_IMAGE", "FS_WORKER_IMAGE_REVISION"),
    "control-plane": ("FS_CONTROL_IMAGE", "FS_CONTROL_IMAGE_REVISION"),
    "fs-cli": ("FS_CLI_IMAGE", "FS_CLI_IMAGE_REVISION"),
    "fs-mcp": ("MCP_IMAGE", "MCP_IMAGE_REVISION"),
}


def _gateway_with_source_images(
    tmp_path: Path,
    *,
    profile: str,
    missing_role: str = "",
    changed_role: str = "",
    running_state: str = "current",
) -> tuple[DockerInstallerGateway, InstallConfig]:
    revision = "c" * 40
    tmp_path.mkdir(parents=True, exist_ok=True)
    values = {
        "FS_INSTALL_MODE": "source",
        "FS_BUILD_SHA": revision,
    }
    labels: dict[str, str] = {}
    running_images: dict[str, tuple[str, ...]] = {}
    runtime_service_by_role = {
        "compact-runtime": "compact-runtime",
        "fs-app": "fs-app",
        "fs-worker": "fs-worker",
        "control-plane": "control-plane",
        "fs-mcp": "fs-mcp",
    }
    for index, role in enumerate(required_product_roles(profile), start=1):
        if role.role == missing_role:
            continue
        image_key, revision_key = _SOURCE_IMAGE_KEYS[role.role]
        image = "sha256:" + f"{index:x}" * 64
        values[image_key] = image
        values[revision_key] = revision
        labels[image] = "d" * 40 if role.role == changed_role else revision
        service = runtime_service_by_role.get(role.role)
        if service is not None:
            running_images[service] = (image,)
    if running_state in {"stale", "mixed"}:
        service = next(iter(running_images))
        stale_image = "sha256:" + "f" * 64
        labels[stale_image] = "b" * 40
        running_images[service] = (
            (stale_image,)
            if running_state == "stale"
            else (running_images[service][0], stale_image)
        )
    env_path = tmp_path / ".env.prod"
    env_path.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
        encoding="utf-8",
    )
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward"),
        process=_ImageProcess(
            labels,
            project_identity=(
                "other-installation"
                if running_state == "wrong-project"
                else f"flow-steward-{profile}"
            ),
            running_images=running_images,
        ),
        environment={"FS_COMPOSE_PROJECT_NAME": f"flow-steward-{profile}"},
    )
    return gateway, InstallConfig(environment="production", deployment_profile=profile)


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_gateway_validates_complete_source_image_provenance(
    tmp_path: Path,
    profile: str,
) -> None:
    gateway, config = _gateway_with_source_images(tmp_path, profile=profile)

    assert gateway.validated_source_revision(config) == "c" * 40


def test_gateway_validates_compact_source_without_separate_optional_images(tmp_path: Path) -> None:
    gateway, config = _gateway_with_source_images(tmp_path, profile="compact")
    env_path = tmp_path / ".env.prod"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    env_path.write_text(
        "\n".join(line for line in lines if not line.startswith(("FS_CLI_IMAGE", "MCP_IMAGE")))
        + "\n",
        encoding="utf-8",
    )

    assert gateway.validated_source_revision(config) == "c" * 40


@pytest.mark.parametrize("revision_key", ["FS_CLI_IMAGE_REVISION", "MCP_IMAGE_REVISION"])
def test_gateway_refuses_selected_compact_image_without_revision(
    tmp_path: Path, revision_key: str
) -> None:
    gateway, config = _gateway_with_source_images(tmp_path, profile="compact")
    env_path = tmp_path / ".env.prod"
    env_path.write_text(
        "\n".join(
            line
            for line in env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(revision_key + "=")
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(InstallerFailure, match="source image provenance"):
        gateway.validated_source_revision(config)


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize("running_state", ("stale", "mixed", "wrong-project"))
def test_gateway_refuses_source_images_not_bound_to_running_compose_runtime(
    tmp_path: Path,
    profile: str,
    running_state: str,
) -> None:
    gateway, config = _gateway_with_source_images(
        tmp_path,
        profile=profile,
        running_state=running_state,
    )

    with pytest.raises(InstallerFailure, match="running source image provenance"):
        gateway.validated_source_revision(config)


def test_terminal_release_publication_switches_durable_mode_to_official(tmp_path: Path) -> None:
    gateway, config = _gateway_with_source_images(tmp_path, profile="compact")
    release = _official_release()

    gateway.publish_release_environment(config, release)

    environment = dict(
        line.split("=", 1)
        for line in (tmp_path / ".env.prod").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert environment["FS_INSTALL_MODE"] == "official"
    assert environment["FS_RELEASE_VERSION"] == release.version
    assert environment["FS_BUILD_SHA"] == release.commit_sha


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_gateway_refuses_incomplete_or_mixed_source_image_provenance(
    tmp_path: Path,
    profile: str,
) -> None:
    roles = required_product_roles(profile)
    incomplete, config = _gateway_with_source_images(
        tmp_path / "incomplete",
        profile=profile,
        missing_role=roles[0].role if profile == "compact" else roles[-1].role,
    )
    mixed, mixed_config = _gateway_with_source_images(
        tmp_path / "mixed",
        profile=profile,
        changed_role=roles[0].role,
    )

    with pytest.raises(InstallerFailure, match="source image provenance"):
        incomplete.validated_source_revision(config)
    with pytest.raises(InstallerFailure, match="source image provenance"):
        mixed.validated_source_revision(mixed_config)
