"""Clean hosts must prepare infrastructure before exact-image startup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.installer import InstallConfig
from core.application.installer.errors import InstallerFailure
from core.infrastructure.config.env_registry import registry_default_value
from core.infrastructure.installer.core_dependency_images import prepare_core_dependency_images
from core.infrastructure.installer.process import ProcessOutcome
from tests.unit.installer.test_installer_gateway import (
    FakeProcessExecutor,
    _exact_prebuilt_values,
    _gateway,
    _write_env_values,
)


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_exact_runtime_prepares_missing_core_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(
        environment="production", deployment_profile=profile, file_storage="local_fs"
    )
    values = _exact_prebuilt_values()
    values["FS_COMPACT_IMAGE"] = "sha256:" + "a" * 64
    _write_env_values(tmp_path / ".env.prod", values)
    monkeypatch.setattr(gateway, "_build_compact_runtime_image", lambda *_a, **_kw: None)
    monkeypatch.setattr(gateway, "_build_scalable_candidate_images", lambda *_a, **_kw: None)
    monkeypatch.setattr(gateway, "_resolve_compact_registry_image", lambda *_a, **_kw: None)
    monkeypatch.setattr(gateway, "_prepare_exact_prebuilt_provenance", lambda *_a, **_kw: None)
    images = {
        "traefik": "traefik:test",
        "rabbitmq-fs": "rabbitmq:test",
        "pg-fs": "registry.example/postgres@sha256:" + "b" * 64,
        "compact-runtime": values["FS_COMPACT_IMAGE"],
    }
    command = (*gateway._compose_prefix(config), "config", "--format", "json")
    process.outcomes[command] = ProcessOutcome(
        command=command,
        returncode=0,
        stdout=json.dumps({"services": {name: {"image": image} for name, image in images.items()}}),
    )
    for image in images.values():
        inspect = ("docker", "image", "inspect", image)
        process.outcomes[inspect] = ProcessOutcome(command=inspect, returncode=1)
    log = gateway.state_store.open_log("clean-host")
    try:
        gateway.prepare_core(config, log=log)
    finally:
        log.close()

    pulled = [command[-1] for command in process.commands if command[:2] == ("docker", "pull")]
    assert set(pulled) == {images["traefik"], images["rabbitmq-fs"], images["pg-fs"]}
    assert not any(command[:3] == ("docker", "buildx", "build") for command in process.commands)


def _prepare_dependencies(tmp_path, *, images, config=None, present=False, environment=None):
    process = FakeProcessExecutor()
    prefix = ("docker", "compose")
    command = (*prefix, "config", "--format", "json")
    process.outcomes[command] = ProcessOutcome(
        command=command,
        returncode=0,
        stdout=json.dumps({"services": {name: {"image": image} for name, image in images.items()}}),
    )
    for image in images.values():
        inspect = ("docker", "image", "inspect", image)
        process.outcomes[inspect] = ProcessOutcome(command=inspect, returncode=0 if present else 1)
    prepare_core_dependency_images(
        failure=_gateway(tmp_path, process)._core_dependency_failure,
        config=config
        or InstallConfig(
            file_storage="local_fs", ingress_mode="external_proxy", tls_mode="reverse_proxy"
        ),
        compose_prefix=prefix,
        repo_root=tmp_path,
        environment=environment or {},
        run=process.run,
        checked=process.run,
    )
    return process.commands


def test_existing_dependencies_are_not_replaced(tmp_path):
    commands = _prepare_dependencies(
        tmp_path, images={"pg-fs": "pg:test", "rabbitmq-fs": "rabbit:test"}, present=True
    )
    assert all(
        command[:2] != ("docker", "pull") and command[:2] != ("docker", "buildx")
        for command in commands
    )


def test_missing_default_postgres_is_built_from_canonical_dockerfile(tmp_path):
    image = registry_default_value("POSTGRES_IMAGE")
    commands = _prepare_dependencies(
        tmp_path,
        images={"pg-fs": image, "rabbitmq-fs": "rabbit:test"},
        environment={"AGE_REF": "test-ref"},
    )
    builds = [command for command in commands if command[:3] == ("docker", "buildx", "build")]
    assert builds == [
        (
            "docker",
            "buildx",
            "build",
            "--load",
            "--tag",
            image,
            "--file",
            str(tmp_path / "infra/docker/postgres-extended/Dockerfile"),
            "--build-arg",
            "AGE_REF=test-ref",
            str(tmp_path),
        )
    ]
    assert ("docker", "pull", image) not in commands


def test_missing_local_image_fails_without_substitution(tmp_path):
    with pytest.raises(InstallerFailure, match=r"pinned local image.*missing"):
        _prepare_dependencies(
            tmp_path, images={"pg-fs": "sha256:" + "a" * 64, "rabbitmq-fs": "rabbit:test"}
        )


def test_bundled_storage_image_is_pulled_once(tmp_path):
    commands = _prepare_dependencies(
        tmp_path,
        config=InstallConfig(
            file_storage="bundled", ingress_mode="external_proxy", tls_mode="reverse_proxy"
        ),
        images={
            "pg-fs": "pg:test",
            "rabbitmq-fs": "rabbit:test",
            "seaweedfs": "seaweed:test",
            "seaweedfs-config": "seaweed:test",
        },
    )
    assert commands.count(("docker", "pull", "seaweed:test")) == 1
    assert not any("traefik" in part for command in commands for part in command)


def test_resume_rechecks_missing_infrastructure(tmp_path, monkeypatch):
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process)
    config = InstallConfig(environment="production", file_storage="local_fs")
    _write_env_values(tmp_path / ".env.prod", _exact_prebuilt_values())
    monkeypatch.setattr(gateway, "_production_provenance_postcondition_holds", lambda _config: True)
    inspect = ("docker", "image", "inspect", "test-traefik:present")
    process.outcomes[inspect] = ProcessOutcome(command=inspect, returncode=1)
    assert gateway.postcondition_holds("prepare", config) is False
