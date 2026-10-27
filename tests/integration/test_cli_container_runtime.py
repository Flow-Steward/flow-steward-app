"""Compose contracts for retained and one-shot Flow Steward CLI runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from core.application.clone.contracts import CloneDataClass
from core.application.lifecycle.contracts import DeploymentConfig
from core.entrypoints.cli.lifecycle_runtime import LifecycleCLIRuntime
from core.infrastructure.config.env_registry import CloneEnvOwner, env_spec_map

ROOT = Path(__file__).parents[2]
PLATFORM_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
COMPOSE_PATHS = (
    ROOT / "infra/docker/docker-compose.yaml",
    ROOT / "infra/docker/docker-compose.prod.yaml",
)


def _compose(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _service(path: Path) -> dict[str, Any]:
    services = _compose(path).get("services")
    assert isinstance(services, dict)
    service = services.get("fs-cli")
    assert isinstance(service, dict), f"{path} does not define fs-cli"
    return service


def _named_service(path: Path, name: str) -> dict[str, Any]:
    services = _compose(path).get("services")
    assert isinstance(services, dict)
    service = services.get(name)
    assert isinstance(service, dict), f"{path} does not define {name}"
    return service


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.name)
def test_fs_cli_service_uses_dedicated_cli_image_target_and_profile(compose_path: Path) -> None:
    service = _service(compose_path)

    if compose_path.name == "docker-compose.prod.yaml":
        assert service["image"] == (
            "${FS_CLI_IMAGE:?FS_CLI_IMAGE is required for immutable production provenance}"
        )
    else:
        assert service["image"] == (f"${{FS_CLI_IMAGE:-flow-steward-cli:{PLATFORM_VERSION}}}")
    if "build" in service:
        assert service["build"]["target"] == "cli"
    assert service["profiles"] == ["cli"]
    assert "depends_on" not in service
    assert service["restart"] == "always"
    assert "ports" not in service
    assert "expose" not in service
    assert "tty" not in service
    assert "stdin_open" not in service
    assert service["entrypoint"] == ["python", "-m", "core.entrypoints.cli.warm_runtime"]
    assert service["labels"] == {
        "com.flow-steward.cli-runtime": "true",
        "com.flow-steward.cli-runtime-contract": "1",
        "com.flow-steward.cli-runtime-revision": "${FS_CLI_RUNTIME_REVISION:-unconfigured}",
    }


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.name)
def test_fs_cli_service_mounts_only_required_runtime_paths(compose_path: Path) -> None:
    service = _service(compose_path)
    volumes = service.get("volumes")
    assert isinstance(volumes, list)
    joined = "\n".join(str(item) for item in volumes)

    assert (
        "${FS_DOCKER_HOST_PROJECT_ROOT:-../../}:"
        "${FS_DOCKER_HOST_PROJECT_ROOT:-/run/flow-steward-host-project}:rw"
    ) in volumes
    assert "/var/run/docker.sock:/var/run/docker.sock" in volumes
    assert len(volumes) == (5 if compose_path.name == "docker-compose.prod.yaml" else 3)
    assert ":/app/extensions" not in joined
    assert ":/app/logs" not in joined
    assert ":/app/backups" not in joined
    assert ":/projects" not in joined
    assert "FS_PROJECTS_ROOT" not in joined
    assert "FS_TEST_PROJECT_ROOT" not in joined
    assert ":/root" not in joined
    assert ":/home" not in joined
    assert "${HOME}" not in joined
    assert "~/" not in joined


def test_host_directory_cli_overlay_binds_transfer_and_shared_trees_without_auto_creation() -> None:
    base = _service(ROOT / "infra/docker/scalable/cli-host-directories.yaml")
    assert base["volumes"] == [
        {
            "type": "bind",
            "source": "${FS_EXTENSIONS_DIR:-../../extensions}",
            "target": "/app/extensions",
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "${FS_BACKUPS_DIR:-../../backups}",
            "target": "/app/backups",
            "bind": {"create_host_path": False},
        },
    ]
    artifacts = _service(ROOT / "infra/docker/scalable/cli-host-directories-artifacts.yaml")
    assert artifacts["volumes"] == [
        {
            "type": "bind",
            "source": "${FS_LOGS_DIR:-../../logs}/artifacts",
            "target": "/app/logs/artifacts",
            "bind": {"create_host_path": False},
        },
    ]


def test_clone_uses_management_container_paths_not_host_env_paths() -> None:
    runtime = object.__new__(LifecycleCLIRuntime)
    environment = {
        "FS_PROJECTS_ROOT": "/srv/host/projects",
        "FS_EXTENSIONS_DIR": "/srv/host/extensions",
        "FS_BACKUPS_DIR": "/srv/host/backups",
        "ARTIFACT_LOCAL_ROOT_DIR": "/srv/host/artifacts",
    }

    scalable = runtime._clone_data_roots(DeploymentConfig(), environment)

    assert scalable == {
        CloneDataClass.EXTENSIONS: Path("/app/extensions"),
        CloneDataClass.LOCAL_ARTIFACTS: Path("/app/logs/artifacts"),
        CloneDataClass.DURABLE_STATE: runtime._clone_state_root(),
    }
    assert runtime._clone_output_root(DeploymentConfig(), environment) == Path("/app/backups")
    assert runtime._clone_input_root(DeploymentConfig(), environment) == Path("/app/backups")


def test_clone_target_shell_binds_host_paths_separately_from_container_io() -> None:
    runtime = object.__new__(LifecycleCLIRuntime)
    environment = {
        "FS_PROJECTS_ROOT": "/srv/target/projects",
        "FS_EXTENSIONS_DIR": "/srv/target/extensions",
        "FS_LOGS_DIR": "/srv/target/logs",
        "FS_INSTALLER_STATE_DIR": "/srv/target/.flow-steward",
    }

    roots = runtime._clone_shell_filesystem_roots(DeploymentConfig(), environment)

    assert roots == {
        "local_artifacts": "/srv/target/logs/artifacts",
        "extensions": "/srv/target/extensions",
        "durable_state": "/srv/target/.flow-steward",
    }


def test_clone_target_shell_normalizes_compose_relative_host_paths() -> None:
    runtime = object.__new__(LifecycleCLIRuntime)
    environment = {
        "FS_DOCKER_HOST_PROJECT_ROOT": "/srv/flow-steward",
        "FS_PROJECTS_ROOT": "/srv/projects",
        "FS_EXTENSIONS_DIR": "../../extensions",
        "FS_LOGS_DIR": "../../logs",
        "FS_INSTALLER_STATE_DIR": "../../.flow-steward",
    }

    roots = runtime._clone_shell_filesystem_roots(DeploymentConfig(), environment)

    assert roots == {
        "local_artifacts": "/srv/flow-steward/logs/artifacts",
        "extensions": "/srv/flow-steward/extensions",
        "durable_state": "/srv/flow-steward/.flow-steward",
    }


def test_clone_environment_ownership_is_canonical_in_env_registry() -> None:
    specs = env_spec_map()

    for name in (
        "FS_CONFIGURATION_STATE_ROOT",
        "FS_HOST_ROOT_DIR",
        "FS_INSTALLER_STATE_DIR",
        "FS_CLI_OPERATION_LOG_ROOT",
        "FS_ENV_FILE",
        "FS_PUBLIC_HOST",
        "FS_AUTH_TRUSTED_ORIGINS",
        "FS_MCP_ISSUER_URL",
        "FS_MCP_RESOURCE_URL",
        "POSTGRES_PASSWORD",
    ):
        assert specs[name].clone_source_owned is False
    for name in (
        "FS_EXTENSION_RUNTIME_SIGNING_SECRET",
        "FS_DEFAULT_PROVIDER",
        "FS_ALLOWED_ATTACHMENT_MIMES",
        "ARTIFACT_S3_PUBLIC_ENDPOINT_URL",
    ):
        assert specs[name].clone_source_owned is True

    for name in ("FS_BUILD_SHA", "FS_BUILD_REVISION", "FS_DEPLOYMENT_REVISION"):
        assert specs[name].clone_owner is CloneEnvOwner.DERIVED
    assert specs["FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY"].clone_owner is CloneEnvOwner.TARGET


def test_every_production_environment_key_has_closed_clone_ownership() -> None:
    production_specs = tuple(
        spec for spec in env_spec_map().values() if "production" in spec.environments
    )

    assert production_specs
    assert all(spec.clone_owner in set(CloneEnvOwner) for spec in production_specs)


def test_compact_clone_durable_root_includes_bootstrap_and_runtime_state() -> None:
    runtime = object.__new__(LifecycleCLIRuntime)

    compact = runtime._clone_data_roots(
        DeploymentConfig(environment="production", deployment_profile="compact"),
        {},
    )

    assert compact[CloneDataClass.DURABLE_STATE] == Path("/data/state")


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.name)
def test_fs_cli_service_receives_env_identity_and_project_network(compose_path: Path) -> None:
    service = _service(compose_path)
    environment = service.get("environment")
    assert isinstance(environment, dict)

    assert "env_file" not in service
    assert service["user"] == "${FS_HOST_UID:-1000}:${FS_HOST_GID:-1000}"
    assert service["group_add"] == ["${FS_DOCKER_GID:-0}"]
    assert "cap_add" not in service
    assert environment["HOME"] == "/tmp"
    assert environment["PYTHONPYCACHEPREFIX"] == "/tmp/flow-steward/pycache"
    assert "MCP_URL" not in environment
    assert "FS_EXTENSIONS_ROOT" not in environment
    assert environment["FS_DOCKER_HOST_PROJECT_ROOT"] == ("${FS_DOCKER_HOST_PROJECT_ROOT:-}")
    assert service["networks"] == ["default", "artifact-storage"]


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.name)
def test_one_shot_extension_manager_has_narrow_archive_mutation_access(
    compose_path: Path,
) -> None:
    service = _named_service(compose_path, "fs-extension-manager")
    volumes = service["volumes"]
    host_root = "${FS_DOCKER_HOST_PROJECT_ROOT:-/run/flow-steward-host-project}"

    assert service["profiles"] == ["cli"]
    assert service["restart"] == "no"
    assert service["command"] == ["true"]
    assert service["working_dir"] == host_root
    assert service["environment"]["FS_EXTENSIONS_ROOT"] == "/app/extensions"
    assert service["environment"]["PYTHONPATH"] == "/app"
    assert service["environment"]["PYTHONSAFEPATH"] == "1"
    assert (f"${{FS_DOCKER_HOST_PROJECT_ROOT:-../../}}:{host_root}:ro") in volumes
    assert any(str(volume).endswith(":/app/extensions:rw") for volume in volumes)
    # A mutating command writes its operation log before it runs; the log directory is
    # the one writable path beside the archive root (a top-level mount, not nested).
    assert (
        "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}/logs:/run/flow-steward-logs:rw"
    ) in volumes
    assert not any("extension-install-policy" in str(volume) for volume in volumes)
    assert len(volumes) == 3
    assert "/var/run/docker.sock:/var/run/docker.sock" not in volumes
    assert "ports" not in service
    assert "expose" not in service


@pytest.mark.parametrize("compose_path", COMPOSE_PATHS, ids=lambda p: p.name)
def test_fs_app_and_cli_share_host_identity_for_owner_only_lifecycle_state(
    compose_path: Path,
) -> None:
    app = _named_service(compose_path, "fs-app")
    cli = _named_service(compose_path, "fs-cli")

    assert app["user"] == cli["user"] == ("${FS_HOST_UID:-1000}:${FS_HOST_GID:-1000}")
    assert app["environment"]["HOME"] == "/tmp"
    if compose_path.name == "docker-compose.prod.yaml":
        assert (
            "${FS_DOCKER_HOST_PROJECT_ROOT:-../../}/.flow-steward/operations:"
            "/app/.flow-steward/operations:ro"
        ) in app["volumes"]


def test_development_test_runner_can_inspect_project_containers() -> None:
    service = _named_service(COMPOSE_PATHS[0], "fs-test-runner")

    assert service["group_add"] == ["${FS_DOCKER_GID:-0}"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in service["volumes"]
