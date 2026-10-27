"""Review P2: Scalable's worker count is configuration that every `compose up` keeps.

Measured with Compose v5.4 before this change: `up -d --scale w=3` gave three
containers, and the next plain `up -d w` -- which every lifecycle operation and the
installer's update run -- gave one. A `deploy.replicas` read from the environment kept
three. So the runbook's `--scale fs-worker=N` was undone by the next change of anything,
and until the rollback capture learned replicas, no lifecycle operation could start
over N > 1 at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.application.installer import InstallConfig
from core.application.installer.errors import InstallerFailure
from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import ConfigurationPatch, DeploymentConfig
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("compose_file", ["docker-compose.yaml", "docker-compose.prod.yaml"])
def test_fs_worker_takes_its_count_from_the_configuration(compose_file: str) -> None:
    worker = yaml.safe_load((ROOT / "infra/docker" / compose_file).read_text(encoding="utf-8"))[
        "services"
    ]["fs-worker"]

    assert worker["deploy"]["replicas"] == "${FS_WORKER_REPLICAS:-1}"
    assert "container_name" not in worker  # a fixed name would make replicas impossible


def test_the_count_is_scalable_only_and_defaults_to_one() -> None:
    assert DeploymentConfig().worker_replicas == 1
    assert DeploymentConfig(deployment_profile="scalable", worker_replicas=3).worker_replicas == 3
    with pytest.raises(ValueError, match="worker_processes"):
        DeploymentConfig(environment="production", deployment_profile="compact", worker_replicas=2)
    assert ConfigurationPatch(worker_replicas=3).worker_replicas == 3


@pytest.mark.parametrize(
    ("profile", "expected"),
    (("scalable", {"FS_WORKER_REPLICAS": "3"}), ("compact", {})),
)
def test_the_rendered_environment_carries_the_count_only_where_it_applies(
    profile: str, expected: dict[str, str]
) -> None:
    config = DeploymentConfig(
        environment="production",
        deployment_profile=profile,  # type: ignore[arg-type]
        worker_replicas=3 if profile == "scalable" else 1,
    )
    target = ConfigurationService(
        store=type("S", (), {"project_name": lambda self, _c: "flow-steward"})()
    ).derive_target(config)

    values = ConfigurationService._active_compose_values(config, target)

    assert {k: v for k, v in values.items() if k == "FS_WORKER_REPLICAS"} == expected


class _Docker:
    """Answers the installer's label queries for one project's running containers."""

    def __init__(self, workers: dict[str, str], *, stopped: int = 0) -> None:
        self.workers = workers
        self.stopped = [f"s{index}" for index in range(stopped)]
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        key = tuple(command)
        self.commands.append(key)
        if key[:2] == ("docker", "ps") and "-q" in key:
            is_worker = any(part.endswith("service=fs-worker") for part in key)
            listed = [*self.workers, *(self.stopped if "-a" in key else ())]
            stdout = "".join(f"{cid}\n" for cid in listed) if is_worker else ""
            return ProcessOutcome(command=key, returncode=0, stdout=stdout)
        if key[:5] == ("docker", "container", "inspect", "--format", "{{.Image}}"):
            stdout = "".join(f"{self.workers[cid]}\n" for cid in key[5:])
            return ProcessOutcome(command=key, returncode=0, stdout=stdout)
        return ProcessOutcome(command=key, returncode=0)


def _gateway(tmp_path: Path, docker: _Docker, *, recorded: str | None) -> DockerInstallerGateway:
    (tmp_path / "infra/docker").mkdir(parents=True)
    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    env = "APP_PORT=8595\n" + (f"FS_WORKER_REPLICAS={recorded}\n" if recorded else "")
    (tmp_path / ".env.prod").write_text(env, encoding="utf-8")
    return DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward"),
        process=docker,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
        sleep=lambda _seconds: None,
    )


class _Log:
    def write(self, _line: str) -> None:
        pass


_IMAGE = "sha256:" + "a" * 64
_SCALABLE = InstallConfig(environment="production", deployment_profile="scalable")


def test_the_running_image_of_several_replicas_is_their_common_image(tmp_path: Path) -> None:
    """R141's update detection asked for "the one running container" and got nothing."""
    gateway = _gateway(tmp_path, _Docker({"w1": _IMAGE, "w2": _IMAGE, "w3": _IMAGE}), recorded="3")

    assert gateway._running_image_id(_SCALABLE, "fs-worker") == _IMAGE


def test_replicas_on_different_images_have_no_single_running_image(tmp_path: Path) -> None:
    other = "sha256:" + "b" * 64
    gateway = _gateway(tmp_path, _Docker({"w1": _IMAGE, "w2": other}), recorded="2")

    assert gateway._running_image_id(_SCALABLE, "fs-worker") == ""


@pytest.mark.parametrize(
    ("running", "recorded", "refused"),
    ((3, None, True), (3, "1", True), (3, "3", False), (1, None, False), (0, "3", False)),
)
def test_an_update_refuses_a_worker_count_nobody_recorded(
    tmp_path: Path, running: int, recorded: str | None, refused: bool
) -> None:
    workers = {f"w{index}": _IMAGE for index in range(running)}
    gateway = _gateway(tmp_path, _Docker(workers), recorded=recorded)

    if refused:
        log = _Log()
        with pytest.raises(InstallerFailure) as raised:
            gateway.refuse_unrecorded_worker_scale(_SCALABLE, log=log)
        assert raised.value.code == "worker_replicas_unrecorded"
        assert f"--worker-replicas {running}" in raised.value.remediation
    else:
        gateway.refuse_unrecorded_worker_scale(_SCALABLE, log=_Log())


@pytest.mark.parametrize(
    ("running", "stopped", "recorded", "refused"),
    ((2, 1, "3", False), (1, 2, "3", False), (2, 2, "3", True), (3, 1, "1", True)),
    ids=["one-stopped", "two-stopped", "one-extra-stopped", "scaled-up-then-stopped"],
)
def test_a_stopped_worker_of_the_recorded_set_is_not_an_unrecorded_scale(
    tmp_path: Path, running: int, stopped: int, recorded: str, refused: bool
) -> None:
    """C3 (External review): recorded 3, one worker stopped -- the update refused with
    "runs 2 containers" instead of starting the third, which the next step does."""
    workers = {f"w{index}": _IMAGE for index in range(running)}
    gateway = _gateway(tmp_path, _Docker(workers, stopped=stopped), recorded=recorded)

    if refused:
        log = _Log()
        with pytest.raises(InstallerFailure):
            gateway.refuse_unrecorded_worker_scale(_SCALABLE, log=log)
    else:
        gateway.refuse_unrecorded_worker_scale(_SCALABLE, log=_Log())
