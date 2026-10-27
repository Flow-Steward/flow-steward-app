"""AC17-R162: ./install.sh on the same revision brings a stopped stack back up.

Found by the auditor following the runbook's restore: `stack stop`, then ./install.sh
on the same revision failed with "control-plane: missing; fs-app: missing" (exit 8) --
the already-installed path only checked health -- and the site stayed down.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.application.installer import InstallConfig
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

_SCALABLE_CORE = ("pg-fs", "rabbitmq-fs")
_SCALABLE_RUNTIME = ("control-plane", "fs-app", "fs-extension-installer", "fs-mcp", "fs-worker")


class _Docker:
    def __init__(self, running: set[str]) -> None:
        self.running = running
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        key = tuple(command)
        self.commands.append(key)
        if key[-4:] == ("ps", "--all", "--format", "json"):
            rows = [
                {"Service": service, "State": "running" if service in self.running else "exited"}
                for service in (*_SCALABLE_CORE, *_SCALABLE_RUNTIME)
            ]
            return ProcessOutcome(command=key, returncode=0, stdout=json.dumps(rows))
        return ProcessOutcome(command=key, returncode=0)

    def ups(self) -> list[tuple[str, ...]]:
        return [key[key.index("up") :] for key in self.commands if "up" in key]


def _gateway(root: Path, docker: _Docker) -> DockerInstallerGateway:
    (root / "infra/docker").mkdir(parents=True, exist_ok=True)
    return DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward"),
        process=docker,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
        sleep=lambda _seconds: None,
    )


def _start(root: Path, docker: _Docker, monkeypatch: pytest.MonkeyPatch) -> str:
    gateway = _gateway(root, docker)
    expected = {*_SCALABLE_CORE, *_SCALABLE_RUNTIME}
    monkeypatch.setattr(gateway, "_health_expected_services", lambda _config: set(expected))
    log = gateway.state_store.open_log(f"start-{uuid4().hex}")
    try:
        return gateway.start_stopped_services(InstallConfig(environment="production"), log=log)
    finally:
        log.close()


def test_a_stopped_stack_is_started_core_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(running=set())

    summary = _start(tmp_path, docker, monkeypatch)

    ups = docker.ups()
    assert len(ups) == 2
    assert set(_SCALABLE_CORE) <= set(ups[0])  # traefik too
    assert "fs-app" not in ups[0]
    assert set(_SCALABLE_RUNTIME) <= set(ups[1])
    assert summary.startswith("Started the stopped services: control-plane, fs-app")


def test_only_the_runtime_is_started_when_the_core_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(running={*_SCALABLE_CORE, "fs-mcp"})

    summary = _start(tmp_path, docker, monkeypatch)

    ups = docker.ups()
    assert len(ups) == 1
    assert "fs-app" in ups[0]
    assert "pg-fs" not in ups[0]
    assert "fs-mcp" not in summary
    assert "pg-fs" not in summary


def test_a_running_stack_is_not_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The green direction, and the R142 rule: nothing asked for a restart."""
    docker = _Docker(running={*_SCALABLE_CORE, *_SCALABLE_RUNTIME})

    assert _start(tmp_path, docker, monkeypatch) == ""
    assert docker.ups() == []


class _Database(_Docker):
    def __init__(self, state: str | None) -> None:
        super().__init__(running=set())
        self.state = state

    def run(self, command, **kwargs):
        key = tuple(command)
        if key[-5:] == ("ps", "--all", "--format", "json", "pg-fs"):
            self.commands.append(key)
            rows = [] if self.state is None else [{"Service": "pg-fs", "State": self.state}]
            return ProcessOutcome(command=key, returncode=0, stdout=json.dumps(rows))
        return super().run(command, **kwargs)

    def compose_verbs(self) -> list[str]:
        verbs = []
        for key in self.commands:
            for verb in ("up", "run"):
                if verb in key:
                    verbs.append(verb)
                    break
        return verbs


def _precheck(root: Path, docker: _Database) -> None:
    gateway = _gateway(root, docker)
    log = gateway.state_store.open_log(f"precheck-{uuid4().hex}")
    try:
        gateway.precheck_migrations(InstallConfig(environment="production"), log=log)
    finally:
        log.close()


def test_an_update_of_a_stopped_stack_starts_the_database_it_checks(tmp_path: Path) -> None:
    """Live on Compact: the R160 precheck ran before `start`, and a stopped stack failed
    with "failed to resolve host 'pg-fs'" under a hint about a migration it did not have."""
    docker = _Database("exited")

    _precheck(tmp_path, docker)

    assert docker.compose_verbs() == ["up", "run"]  # the container as it is, then the check
    start = next(key for key in docker.commands if "up" in key)
    assert start[-1] == "pg-fs"
    assert {"--no-recreate", "--no-deps", "--wait"} <= set(start)


@pytest.mark.parametrize("state", ["running", None], ids=["running", "no-container"])
def test_a_running_database_is_not_touched_before_the_check(
    tmp_path: Path, state: str | None
) -> None:
    docker = _Database(state)

    _precheck(tmp_path, docker)

    assert docker.compose_verbs() == ["run"]


@pytest.mark.parametrize(
    ("environment", "pins"),
    [({"FS_COMPACT_IMAGE": "flow-steward-compact:86bd3eb1d3c1"}, ("FS_COMPACT_IMAGE",)), ({}, ())],
    ids=["exported", "clean-shell"],
)
def test_compact_images_named_in_the_shell_are_reported_as_its_pins(
    tmp_path: Path, environment: dict[str, str], pins: tuple[str, ...]
) -> None:
    (tmp_path / "infra/docker").mkdir(parents=True, exist_ok=True)
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward"),
        process=_Docker(running=set()),
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment=environment,
        sleep=lambda _seconds: None,
    )

    compact = InstallConfig(environment="production", deployment_profile="compact")
    assert gateway._shell_image_pins(compact) == pins


def test_a_stopped_replica_is_started_and_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3 acceptance: recorded 3, fs-worker-3 stopped -- it came back through the runtime's
    recreate, and "Started the stopped services" was never said."""

    class _Replicas(_Docker):
        def run(self, command, **kwargs):
            key = tuple(command)
            if key[-4:] == ("ps", "--all", "--format", "json"):
                self.commands.append(key)
                rows = [
                    {"Service": service, "State": "running"}
                    for service in (*_SCALABLE_CORE, *_SCALABLE_RUNTIME)
                ] + [{"Service": "fs-worker", "State": "exited"}]
                return ProcessOutcome(command=key, returncode=0, stdout=json.dumps(rows))
            return super().run(command, **kwargs)

    docker = _Replicas(running=set())

    summary = _start(tmp_path, docker, monkeypatch)

    assert summary == "Started the stopped services: fs-worker."
    assert len(docker.ups()) == 1
    assert "fs-worker" in docker.ups()[0]
