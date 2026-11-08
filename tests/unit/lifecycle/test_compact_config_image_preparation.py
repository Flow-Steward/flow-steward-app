"""Installed immutable Compact images never fall back to a local source build."""

from pathlib import Path

import pytest

from core.application.lifecycle.configuration_service import ConfigApplyRequest
from core.application.lifecycle.contracts import ComposeTarget, ConfigurationPatch, DeploymentConfig
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_service import StackGatewayError
from core.application.release.images import all_release_roles
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway
from tests.unit.lifecycle.test_configuration_service import (
    FakeConfigurationGateway,
    _artifact_gateway,
    _service,
    _write_lifecycle_secret_environment,
)
from tests.unit.lifecycle.test_stack_service import _Process, _target


def _compact_target() -> ComposeTarget:
    return _target().model_copy(
        update={
            "stack_environment": "production",
            "deployment_profile": "compact",
            "env_file": ".env.prod",
            "compose_files": ("infra/docker/docker-compose.compact.yaml",),
            "required_services": ("compact-runtime",),
            "runtime_services": ("compact-runtime",),
            "rebuild_services": (),
        }
    )


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_missing_compact_local_image_fails_without_build_or_pull(
    tmp_path: Path, profile: str
) -> None:
    image = f"sha256:{'a' * 64}"
    process = _Process([(0, f"{image}\n"), (1, ""), (0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    with pytest.raises(StackGatewayError):
        gateway.prepare_target_images(
            _compact_target().model_copy(update={"deployment_profile": profile}),
            environment={"FS_COMPACT_IMAGE": image},
        )

    assert all("build" not in command and "pull" not in command for command in process.commands)


def test_missing_compact_digest_is_pulled_and_verified_without_build(tmp_path: Path) -> None:
    image = f"registry.example.test/compact@sha256:{'a' * 64}"
    process = _Process([(0, f"{image}\n"), (1, ""), (0, ""), (0, f"sha256:{'b' * 64}\n")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.prepare_target_images(_compact_target(), environment={"FS_COMPACT_IMAGE": image})

    assert all("build" not in command for command in process.commands)
    assert any("pull" in command for command in process.commands)
    assert process.commands[-1][:3] == ("docker", "image", "inspect")


def test_unavailable_compact_digest_fails_without_build(tmp_path: Path) -> None:
    image = f"registry.example.test/compact@sha256:{'a' * 64}"
    process = _Process([(0, f"{image}\n"), (1, ""), (0, ""), (1, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    with pytest.raises(StackGatewayError):
        gateway.prepare_target_images(_compact_target(), environment={"FS_COMPACT_IMAGE": image})

    assert all("build" not in command for command in process.commands)


@pytest.mark.parametrize("inventory", ("", "flow-steward-app:latest\n"))
@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_production_rejects_incomplete_or_unpinned_role_images(
    tmp_path: Path, inventory: str, profile: str
) -> None:
    process = _Process([(0, inventory), (0, f"sha256:{'b' * 64}\n")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    role = "compact-runtime" if profile == "compact" else "fs-app"
    target = _compact_target().model_copy(
        update={"deployment_profile": profile, "required_services": (role,)}
    )

    with pytest.raises(StackGatewayError):
        gateway.prepare_target_images(
            target,
            environment={"FS_COMPACT_IMAGE": inventory.strip(), "FS_APP_IMAGE": inventory.strip()},
        )

    assert all("build" not in command and "pull" not in command for command in process.commands)


def test_development_retains_explicit_source_image_build(tmp_path: Path) -> None:
    process = _Process([(0, "local-source:dev\n"), (1, ""), (0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.prepare_target_images(_target(), environment={"FS_INSTALL_MODE": "source"})

    assert "build" in process.commands[-1]


def test_production_dependency_tags_can_be_pulled_without_source_build(tmp_path: Path) -> None:
    image = f"registry.example.test/compact@sha256:{'a' * 64}"
    process = _Process(
        [
            (0, f"{image}\nrabbitmq:3.13-management\n"),
            (1, ""),
            (0, ""),
            (0, ""),
            (0, f"sha256:{'b' * 64}\nsha256:{'c' * 64}\n"),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.prepare_target_images(_compact_target(), environment={"FS_COMPACT_IMAGE": image})

    assert ("docker", "pull", "rabbitmq:3.13-management") in process.commands
    assert all("build" not in command for command in process.commands)


def test_scalable_production_reuses_each_canonical_role_image(tmp_path: Path) -> None:
    roles = tuple(
        role
        for role in all_release_roles()
        if role.role in {"fs-app", "fs-worker", "control-plane", "fs-mcp", "postgres"}
    )
    environment = {
        role.environment_variable: f"sha256:{index:064x}"
        for index, role in enumerate(roles, start=1)
    }
    images = tuple(environment.values())
    target = _compact_target().model_copy(
        update={
            "deployment_profile": "scalable",
            "required_services": (
                "fs-app",
                "fs-worker",
                "control-plane",
                "fs-mcp",
                "pg-fs",
                "fs-extension-installer",
                "memory-postgres-extensions",
            ),
        }
    )
    process = _Process([(0, "\n".join(images)), (0, "\n".join(images))])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.prepare_target_images(target, environment=environment)

    assert len(process.commands) == 2
    assert all("build" not in command and "pull" not in command for command in process.commands)


@pytest.mark.parametrize("target_profile", ("compact", "scalable"))
def test_config_apply_prepares_production_images_before_capture_or_mutation(
    tmp_path: Path, target_profile: str
) -> None:
    class MissingImageGateway(FakeConfigurationGateway):
        def prepare_target_images(self, config, target, environment) -> None:
            self.calls.append("prepare")
            raise StackGatewayError(action="target_image_reuse", detail="Exact image is missing")

    gateway = MissingImageGateway()
    service = _service(tmp_path, gateway=gateway)
    active = DeploymentConfig(environment="production", deployment_profile="compact")
    service.store.write_active(active)
    _write_lifecycle_secret_environment(tmp_path / ".env.prod")
    service.set(ConfigurationPatch(deployment_profile=target_profile, public_host="fs.xenonled.eu"))
    pending = service.read_pending()
    before_environment = (tmp_path / ".env.prod").read_bytes()

    with pytest.raises(LifecycleError) as raised:
        service.apply(ConfigApplyRequest())

    assert raised.value.code == "configuration_images_unavailable"
    assert "exact installed images or registry access" in raised.value.remediation
    assert "flow-steward config apply" in raised.value.remediation
    assert gateway.calls == ["validate", "prepare"]
    assert service.read_active() == active
    assert service.read_pending() == pending
    assert (tmp_path / ".env.prod").read_bytes() == before_environment


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_configuration_activation_missing_image_never_builds_or_implicitly_pulls(
    tmp_path: Path, profile: str
) -> None:
    target = (
        _compact_target()
        if profile == "compact"
        else _target().model_copy(update={"stack_environment": "production"})
    )
    process = _Process(
        [
            (0, "\n".join(target.runtime_services)),
            (1, "Exact installed image is missing"),
        ]
    )
    gateway, _fake_docker = _artifact_gateway(tmp_path)
    gateway._docker = DockerLifecycleGateway(project_root=tmp_path, process=process)
    active = DeploymentConfig(environment="production", deployment_profile=profile)
    candidate = active.model_copy(update={"public_host": "fs.xenonled.eu"})
    snapshot = {
        "operation_id": "config-activation",
        "project_name": target.project_name,
        "selected_services": [],
        "services": [],
    }

    with pytest.raises(StackGatewayError):
        gateway.activate(active, candidate, target, target, snapshot)

    commands = [command for command in process.commands if "up" in command]
    assert len(commands) == 1
    assert "--no-build" in commands[0]
    assert commands[0][commands[0].index("--pull") + 1] == "never"
