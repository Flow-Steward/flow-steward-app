"""Review P2: lifecycle rollback must survive Scalable's worker replicas.

The runbook scales Scalable workers with container replicas, and every lifecycle
operation began with a rollback capture that refused outright: "Rollback capture
requires one container per managed service". So once an operator had more than one
worker, `config apply`, `stack` and `release` could not start at all.

The capture now records a service's replicas as one entry with their count, and
requires them to agree -- one image, one topology -- because a restore recreates them
from one definition. The restore check compares the count as well.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.stack_service import (
    StackContainerRollbackState,
    StackGatewayError,
    StackRollbackSnapshot,
)
from core.infrastructure.lifecycle import docker_rollback_operations as gateway_module
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway

_WORKER_IMAGE = f"sha256:{'b' * 64}"
_OTHER_IMAGE = f"sha256:{'c' * 64}"


class _Process:
    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.responses = list(responses)
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        self.commands.append(tuple(command))
        returncode, stdout = self.responses.pop(0)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _target() -> ComposeTarget:
    return ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        env_file=".env.prod",
        compose_files=("infra/docker/docker-compose.prod.yaml",),
        compose_profiles=(),
        required_services=("fs-app", "fs-worker", "pg-fs"),
        runtime_services=("fs-app", "fs-worker"),
        rebuild_services=("fs-app", "fs-worker"),
        project_name="installed-project",
    )


def _ps(*workers: tuple[str, str]) -> str:
    return "\n".join(
        json.dumps(
            {
                "ID": container_id,
                "Name": f"installed-project-fs-worker-{index}",
                "Service": "fs-worker",
                "State": state,
                "Health": "",
            }
        )
        for index, (container_id, state) in enumerate(workers, start=1)
    )


def _inspect(*images: tuple[str, str]) -> str:
    return "\n".join(
        json.dumps(
            {
                "Id": container_id,
                "Image": image,
                "Config": {"Labels": {"com.docker.compose.service": "fs-worker"}},
            }
        )
        for container_id, image in images
    )


def _capture(process: _Process, tmp_path) -> StackRollbackSnapshot:
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    return gateway.capture_rollback(
        _target(),
        operation_id="operation-replicas",
        selected_services=("fs-worker",),
        managed_services=("fs-worker",),
    )


def test_three_worker_replicas_are_captured_as_one_service_with_their_count(tmp_path) -> None:
    process = _Process(
        [
            (0, _ps(("w1", "running"), ("w2", "running"), ("w3", "running"))),
            (0, ""),
            (0, _inspect(("w1", _WORKER_IMAGE), ("w2", _WORKER_IMAGE), ("w3", _WORKER_IMAGE))),
        ]
    )

    snapshot = _capture(process, tmp_path)

    (worker,) = snapshot.services
    assert (worker.service, worker.topology, worker.image_id) == (
        "fs-worker",
        "running",
        _WORKER_IMAGE,
    )
    assert worker.replicas == 3
    assert process.commands[-1][-3:] == ("w1", "w2", "w3")


@pytest.mark.parametrize(
    ("states", "images", "named"),
    (
        (
            (("w1", "running"), ("w2", "running")),
            (("w1", _WORKER_IMAGE), ("w2", _OTHER_IMAGE)),
            "image",
        ),
        (
            (("w1", "running"), ("w2", "exited")),
            (("w1", _WORKER_IMAGE), ("w2", _WORKER_IMAGE)),
            "topology",
        ),
    ),
    ids=("mixed-images", "mixed-topology"),
)
def test_replicas_that_disagree_are_refused_by_name(tmp_path, states, images, named) -> None:
    """A restore recreates replicas from one definition; it cannot restore a mixture."""
    process = _Process([(0, _ps(*states)), (0, ""), (0, _inspect(*images))])

    with pytest.raises(StackGatewayError) as raised:
        _capture(process, tmp_path)

    assert "fs-worker" in raised.value.detail
    assert named in raised.value.detail


def test_a_restore_that_lost_replicas_is_a_mismatch() -> None:
    def snapshot(replicas: int) -> StackRollbackSnapshot:
        return StackRollbackSnapshot(
            operation_id="operation-replicas",
            project_name="installed-project",
            selected_services=("fs-worker",),
            services=(
                StackContainerRollbackState(
                    service="fs-worker",
                    topology="running",
                    container_id="w1",
                    image_id=_WORKER_IMAGE,
                    replicas=replicas,
                ),
            ),
        )

    mismatches = gateway_module._restore_mismatches(snapshot(3), snapshot(1))

    assert mismatches == ("fs-worker: expected 3 containers, observed 1",)
    assert gateway_module._restore_mismatches(snapshot(3), snapshot(3)) == ()


def test_evidence_recorded_before_replicas_existed_still_reads_as_one() -> None:
    old = {
        "operation_id": "operation-old",
        "project_name": "installed-project",
        "selected_services": ["fs-worker"],
        "services": [
            {
                "service": "fs-worker",
                "topology": "running",
                "container_id": "w1",
                "image_id": _WORKER_IMAGE,
            }
        ],
    }

    assert StackRollbackSnapshot.model_validate(old).services[0].replicas == 1


class _Accepting:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        self.commands.append(tuple(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_a_rollback_restores_the_worker_count_it_captured(tmp_path) -> None:
    """C4 (External review): recorded 1, three workers running by hand, and an apply of
    `--worker-replicas 3` that failed. The rollback restored the previous environment
    (FS_WORKER_REPLICAS=1), Compose brought fs-worker up at 1, and the restore check --
    expecting the 3 it captured -- refused forever."""
    snapshot = StackRollbackSnapshot(
        operation_id="operation-replicas",
        project_name="installed-project",
        selected_services=("fs-worker",),
        services=(
            StackContainerRollbackState(
                service="fs-worker",
                topology="running",
                container_id="w1",
                image_id=_WORKER_IMAGE,
                replicas=3,
            ),
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="a1",
                image_id=_OTHER_IMAGE,
            ),
        ),
    )
    process = _Accepting()
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.restore(
        _target(),
        action="restart",
        snapshot=snapshot,
        timeout_seconds=30,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    (up,) = [command for command in process.commands if "up" in command]
    assert up[up.index("--scale") : up.index("--scale") + 2] == ("--scale", "fs-worker=3")
    assert "fs-app=1" not in up  # a single container, which may carry a container_name
