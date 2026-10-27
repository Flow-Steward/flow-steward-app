"""F3 (live on fs-acc, 2026-09-25): without FS_BACKUP_ENCRYPTION_KEY in .env.prod every
doctor check came out "JSONDecodeError: Expecting value: line 1 column 1 (char 0)".
Compose warns `The "FS_BACKUP_ENCRYPTION_KEY" variable is not set` on stderr and still
answers; the installer read `compose ps --format json` with the streams merged, so the
warning was the first "JSON" line. A value is read from stdout alone (AC17-R138)."""

from __future__ import annotations

import json
from pathlib import Path

from core.application.installer import InstallConfig
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

_WARNING = (
    'time="2026-09-25T10:00:00Z" level=warning msg="The \\"FS_BACKUP_ENCRYPTION_KEY\\" '
    'variable is not set. Defaulting to a blank string."'
)


class _WarningCompose:
    """Compose with an unset variable: a warning on stderr, the answer on stdout."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.answer = "\n".join(json.dumps(row) for row in rows)

    def run(self, command, *, separate_stderr: bool = False, **_kwargs):
        command = tuple(command)
        if "ps" not in command:
            return ProcessOutcome(command=command, returncode=0, stdout="")
        if separate_stderr:
            return ProcessOutcome(
                command=command, returncode=0, stdout=self.answer, stderr=_WARNING
            )
        return ProcessOutcome(command=command, returncode=0, stdout=_WARNING + "\n" + self.answer)


def _gateway(tmp_path: Path, rows: list[dict[str, str]]) -> DockerInstallerGateway:
    (tmp_path / "infra/docker").mkdir(parents=True)
    return DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward", lifecycle_state_root=tmp_path),
        process=_WarningCompose(rows),
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-acc"},
        sleep=lambda _seconds: None,
    )


_COMPACT = InstallConfig(
    environment="production", deployment_profile="compact", data_placement="compact_volume"
)
_RUNNING = [
    {"Service": service, "State": "running", "Health": ""}
    for service in ("compact-runtime", "pg-fs", "rabbitmq-fs", "traefik")
]


def test_stopped_services_reads_the_answer_not_the_warning(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, [*_RUNNING[:-1], {"Service": "traefik", "State": "exited"}])

    stopped = gateway.stopped_services(_COMPACT, log=gateway.state_store.open_log("f3"))

    assert stopped == ["traefik"]


def test_health_is_verified_from_the_answer_not_the_warning(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, _RUNNING)

    gateway.verify_health(_COMPACT, log=gateway.state_store.open_log("f3"))  # no raise


def test_running_stack_is_not_restarted_because_of_a_warning(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, _RUNNING)

    log = gateway.state_store.open_log("f3-start")
    try:
        result = gateway.start_stopped_services(_COMPACT, log=log)
    finally:
        log.close()

    assert result == ""


def test_a_running_database_is_not_started_again_because_of_a_warning(tmp_path: Path) -> None:
    """_start_stopped_database read no row from the merged text, took "no container yet"
    and went on; with the answer read, a running pg-fs is left as it is."""
    gateway = _gateway(tmp_path, [{"Service": "pg-fs", "State": "running"}])
    commands: list[tuple[str, ...]] = []
    run = gateway.process.run

    def recording(command, **kwargs):
        commands.append(tuple(command))
        return run(command, **kwargs)

    gateway.process.run = recording

    gateway._start_stopped_database(_COMPACT, log=gateway.state_store.open_log("f3"))

    assert not any("up" in command for command in commands)


def test_a_stopped_database_is_started_through_the_warning(tmp_path: Path) -> None:
    """The green direction: an exited pg-fs is still seen, and started."""
    gateway = _gateway(tmp_path, [{"Service": "pg-fs", "State": "exited"}])
    commands: list[tuple[str, ...]] = []
    run = gateway.process.run

    def recording(command, **kwargs):
        commands.append(tuple(command))
        return run(command, **kwargs)

    gateway.process.run = recording

    gateway._start_stopped_database(_COMPACT, log=gateway.state_store.open_log("f3"))

    assert any("up" in command and "pg-fs" in command for command in commands)


class _WarningWorkers:
    def run(self, command, *, separate_stderr: bool = False, **_kwargs):
        command = tuple(command)
        answer = (
            "1\n"
            if "printenv" in command
            else json.dumps({"worker_states": {"worker-1": "active"}, "workers_complete": True})
        )
        return ProcessOutcome(
            command=command,
            returncode=0,
            stdout=answer if separate_stderr else _WARNING + "\n" + answer,
            stderr=_WARNING if separate_stderr else "",
        )


def test_worker_count_warning_does_not_recreate_an_unchanged_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    gateway = _gateway(tmp_path, _RUNNING)
    gateway.process = _WarningWorkers()

    def unexpected_recreate(*_args, **_kwargs):
        raise AssertionError("an unchanged runtime must not be recreated")

    monkeypatch.setattr(gateway, "start_runtime", unexpected_recreate)
    log = gateway.state_store.open_log("workers-count")
    try:
        config = _COMPACT.model_copy(update={"worker_processes": 1})
        assert gateway.converge_compact_worker_processes(config, log=log) == ""
    finally:
        log.close()


def test_worker_readiness_json_is_not_polluted_by_compose_warning(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, _RUNNING)
    gateway.process = _WarningWorkers()
    log = gateway.state_store.open_log("workers-ready")
    try:
        config = _COMPACT.model_copy(update={"worker_processes": 1})
        assert gateway._await_compact_workers(config, log=log) == 1
    finally:
        log.close()
