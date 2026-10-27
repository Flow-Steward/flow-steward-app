"""AC17-R157: ./install.sh --worker-processes N changes an installed Compact's count."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.application.installer import InstallConfig
from core.application.installer.errors import InstallerFailure
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore


class _Runtime:
    """The Compact runtime as Docker shows it: its env, and its readiness details."""

    def __init__(self, running: str, *, registers: int | None = None) -> None:
        self.running = running
        self.registers = registers
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        key = tuple(command)
        self.commands.append(key)
        if key[-2:] == ("printenv", "FS_WORKER_PROCESSES"):
            return ProcessOutcome(command=key, returncode=0, stdout=self.running + "\n")
        if "up" in key and key[-1] == "compact-runtime":
            self.running = self.applied
            return ProcessOutcome(command=key, returncode=0)
        if "python" in key and "health/details" in key[-1]:
            count = int(self.running) if self.registers is None else self.registers
            details = {
                "worker_states": {f"worker-{index}": "active" for index in range(count)},
                "workers_complete": count == int(self.applied),
            }
            return ProcessOutcome(command=key, returncode=0, stdout=json.dumps(details))
        return ProcessOutcome(command=key, returncode=0)

    def recreated(self) -> bool:
        return any("up" in key and key[-1] == "compact-runtime" for key in self.commands)


def _compact(**fields) -> InstallConfig:
    return InstallConfig(environment="production", deployment_profile="compact", **fields)


@pytest.fixture
def installed(tmp_path: Path):
    (tmp_path / "infra/docker").mkdir(parents=True)
    state = tmp_path / ".flow-steward"
    state.mkdir()
    (tmp_path / ".env.prod").write_text(
        "POSTGRES_PASSWORD=kept-secret\nFS_WORKER_PROCESSES=2\n", encoding="utf-8"
    )
    (state / "compose.env").write_text(
        "FS_COMPOSE_PROJECT_NAME=fs-compact\nFS_WORKER_PROCESSES=2\n", encoding="utf-8"
    )

    def build(runtime: _Runtime) -> DockerInstallerGateway:
        return DockerInstallerGateway(
            repo_root=tmp_path,
            state_store=InstallerStateStore(state),
            process=runtime,
            disk_free_bytes=lambda _path: 10 * 1024**3,
            port_available=lambda _host, _port: True,
            architecture=lambda: "arm64",
            docker_cli_available=lambda: True,
            environment={},
            sleep=lambda _seconds: None,
        )

    return tmp_path, build


def _log(gateway: DockerInstallerGateway):
    return gateway.state_store.open_log(f"workers-{uuid4().hex}")


def test_a_new_count_is_recorded_in_one_key_of_each_file(installed) -> None:
    root, build = installed
    gateway = build(_Runtime("2"))
    log = _log(gateway)

    applied = gateway.set_compact_worker_processes(
        _compact(worker_processes=2), processes=4, log=log
    )

    assert applied.worker_processes == 4
    env = (root / ".env.prod").read_text(encoding="utf-8")
    assert "FS_WORKER_PROCESSES=4" in env
    assert "POSTGRES_PASSWORD=kept-secret" in env
    assert "FS_WORKER_PROCESSES=4" in (root / ".flow-steward/compose.env").read_text("utf-8")
    assert "worker_processes = 4" in (root / ".flow-steward/stack.toml").read_text("utf-8")


def test_the_runtime_is_recreated_and_the_new_count_verified(installed) -> None:
    _root, build = installed
    runtime = _Runtime("2")
    runtime.applied = "4"
    gateway = build(runtime)

    summary = gateway.converge_compact_worker_processes(
        _compact(worker_processes=4), log=_log(gateway)
    )

    assert runtime.recreated()
    assert summary == "Compact worker processes: 2 -> 4."


def test_the_same_count_is_no_change_and_no_recreate(installed) -> None:
    _root, build = installed
    runtime = _Runtime("2")
    runtime.applied = "2"
    gateway = build(runtime)

    assert (
        gateway.converge_compact_worker_processes(_compact(worker_processes=2), log=_log(gateway))
        == ""
    )
    assert not runtime.recreated()  # the R142 rule: nothing asked for a restart


def test_a_count_that_never_registers_fails_by_name(installed) -> None:
    _root, build = installed
    runtime = _Runtime("2", registers=3)
    runtime.applied = "4"
    gateway = build(runtime)

    config = _compact(worker_processes=4)
    operation_log = _log(gateway)
    with pytest.raises(InstallerFailure) as raised:
        gateway.converge_compact_worker_processes(config, log=operation_log)

    assert raised.value.code == "compact_workers_unverified"
    assert "3 registered" in raised.value.cause


def test_a_count_this_run_changed_is_verified_even_when_already_running(installed) -> None:
    """R157 acceptance: an earlier step recreated the runtime with the new count, so
    convergence saw nothing to do -- and never checked the workers registered."""
    _root, build = installed
    runtime = _Runtime("1", registers=2)  # the old workers are still registered
    runtime.applied = "1"
    gateway = build(runtime)

    config = _compact(worker_processes=1)
    operation_log = _log(gateway)
    with pytest.raises(InstallerFailure) as raised:
        gateway.converge_compact_worker_processes(config, log=operation_log, changed=True)

    assert raised.value.code == "compact_workers_unverified"
    assert not runtime.recreated()  # already running the count: no second recreate
