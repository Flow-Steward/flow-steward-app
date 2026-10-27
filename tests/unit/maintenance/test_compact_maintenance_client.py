"""Host management must use Compact's real operator, never a local fallback marker."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_service import StackGatewayError
from core.application.maintenance.compact_client import CompactMaintenanceClient
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway
from core.infrastructure.lifecycle.state_store import LifecycleStateStore


def _target() -> ComposeTarget:
    return ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        project_name="owned-compact",
        deployment_profile="compact",
        compose_files=("infra/docker/docker-compose.compact.yaml",),
        env_file=".env.prod",
    )


class Process:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.commands: list[tuple[str, ...]] = []

    def run(self, command: list[str], **_kwargs: object):
        self.commands.append(tuple(command))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr="")


def _client(tmp_path: Path, process: Process) -> CompactMaintenanceClient:
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    return CompactMaintenanceClient(
        lambda operation: gateway.compact_maintenance_operation(_target(), operation),
        mutation_state=LifecycleStateStore(
            tmp_path, installation_lock_path=tmp_path / ".flow-steward/install.lock"
        ),
    )


@pytest.mark.parametrize("status", ["enabled", "draining", "disabled"])
def test_compact_client_returns_observed_runtime_status(tmp_path: Path, status: str) -> None:
    result = {"status": status, "activated_at": None, "owner_kind": None}
    if status != "disabled":
        result.update(activated_at="2026-10-08T11:00:00Z", owner_kind="manual")
    process = Process(
        json.dumps(
            {
                "schema": "flow-steward.cli.v1",
                "ok": True,
                "command": "maintenance.status",
                "result": result,
            }
        )
    )

    observed = _client(tmp_path, process).status()

    assert observed.status == status
    assert observed.owner_kind == (None if status == "disabled" else "manual")
    assert process.commands[0][-10:] == (
        "exec",
        "-T",
        "--user",
        "10007:10007",
        "compact-runtime",
        "python",
        "-m",
        "core.entrypoints.cli.platform_runtime",
        "maintenance-status",
        "--json",
    )


@pytest.mark.parametrize(
    "output",
    [
        "not JSON",
        "{}",
        "[]",
        "x" * (64 * 1024 + 1),
        '{"schema":"flow-steward.cli.v1","ok":false,"command":"maintenance.status","result":{}}',
        '{"schema":"flow-steward.cli.v1","ok":true,"command":"release.info","result":{}}',
    ],
)
def test_compact_status_does_not_convert_invalid_reply_to_disabled(
    tmp_path: Path,
    output: str,
) -> None:
    client = _client(tmp_path, Process(output))
    with pytest.raises(StackGatewayError, match="invalid result"):
        client.status()


def test_compact_status_does_not_convert_docker_failure_to_disabled(tmp_path: Path) -> None:
    client = _client(tmp_path, Process("runtime not running", returncode=1))
    with pytest.raises(StackGatewayError):
        client.status()


def test_compact_status_rejects_invalid_closed_status(tmp_path: Path) -> None:
    output = json.dumps(
        {
            "schema": "flow-steward.cli.v1",
            "ok": True,
            "command": "maintenance.status",
            "result": {"status": "unknown"},
        }
    )
    client = _client(tmp_path, Process(output))
    with pytest.raises(ValidationError):
        client.status()


@pytest.mark.parametrize("operation", ["shell", "maintenance-status --user root"])
def test_compact_maintenance_rejects_nonfixed_operation(
    tmp_path: Path,
    operation: str,
) -> None:
    process = Process("{}")
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target()
    with pytest.raises(ValueError):
        gateway.compact_maintenance_operation(target, operation)
    assert not process.commands


def test_compact_maintenance_rejects_noncompact_target(tmp_path: Path) -> None:
    process = Process("{}")
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target().model_copy(update={"deployment_profile": "scalable"})
    with pytest.raises(ValueError):
        gateway.compact_maintenance_operation(target, "maintenance-status")
    assert not process.commands


@pytest.mark.parametrize(
    ("method", "operation", "command", "status"),
    [
        ("enable", "maintenance-enable", "maintenance.enable", "enabled"),
        ("disable", "maintenance-disable", "maintenance.disable", "disabled"),
    ],
)
def test_compact_client_mutations_retain_normal_confirmation(
    tmp_path: Path,
    method: str,
    operation: str,
    command: str,
    status: str,
) -> None:
    process = Process(
        json.dumps(
            {
                "schema": "flow-steward.cli.v1",
                "ok": True,
                "command": command,
                "result": {"status": status, "changed": True},
            }
        )
    )

    result = getattr(_client(tmp_path, process), method)()

    assert result.status == status
    assert result.changed is True
    assert process.commands[0][-3:] == (operation, "--json", "--confirm")


def test_compact_public_client_does_not_claim_an_arbitrary_owner(tmp_path: Path) -> None:
    process = Process("{}")
    client = _client(tmp_path, process)
    with pytest.raises(ValueError, match="manual operator"):
        client.enable(owner_kind="release", owner_id="another-owner")
    assert not process.commands


def test_compact_client_refuses_host_unfinished_operation_before_native_disable() -> None:
    observed = []

    class State:
        @contextmanager
        def operation_lock(self, operation_id, *, mutating):
            observed.append((operation_id, mutating))
            yield

        def unfinished_journals(self):
            return (object(),)

    client = CompactMaintenanceClient(
        lambda operation: observed.append(operation) or {}, mutation_state=State()
    )
    with pytest.raises(LifecycleError) as captured:
        client.disable()
    assert captured.value.code == "maintenance_unfinished_operations"
    assert observed == [("maintenance-disable", True)]


@pytest.mark.parametrize(
    ("operation", "timeout"),
    [("maintenance-status", 30.0), ("maintenance-enable", None), ("maintenance-disable", None)],
)
def test_compact_operator_bounds_observation_not_cooperative_drain(
    tmp_path: Path, operation: str, timeout: float | None
) -> None:
    observed: list[float | None] = []

    class TimedProcess(Process):
        def run(self, command: list[str], **kwargs: object):
            observed.append(kwargs["timeout_seconds"])
            return super().run(command, **kwargs)

    process = TimedProcess(
        json.dumps(
            {
                "schema": "flow-steward.cli.v1",
                "ok": True,
                "command": operation.replace("-", "."),
                "result": {},
            }
        )
    )

    DockerLifecycleGateway(project_root=tmp_path, process=process).compact_maintenance_operation(
        _target(), operation
    )

    assert observed == [timeout]


@pytest.mark.parametrize("method", ("enable", "disable"))
def test_compact_host_guard_remains_held_until_native_mutation_returns(
    tmp_path: Path, method: str
) -> None:
    import fcntl

    lock_path = tmp_path / ".flow-steward/install.lock"
    lock_path.parent.mkdir()
    lock_path.touch()

    class GuardedProcess(Process):
        def run(self, command: list[str], **kwargs: object):
            for path in (lock_path, lock_path.parent / "operations/active.lock"):
                with path.open("r") as attempted, pytest.raises(BlockingIOError):
                    fcntl.flock(attempted, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return super().run(command, **kwargs)

    process = GuardedProcess(
        json.dumps(
            {
                "schema": "flow-steward.cli.v1",
                "ok": True,
                "command": f"maintenance.{method}",
                "result": {
                    "status": "enabled" if method == "enable" else "disabled",
                    "changed": True,
                },
            }
        )
    )
    assert getattr(_client(tmp_path, process), method)().changed is True
    for path in (lock_path, lock_path.parent / "operations/active.lock"):
        with path.open("r") as attempted:
            fcntl.flock(attempted, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_compact_status_is_read_only_while_installer_lock_is_held(tmp_path: Path) -> None:
    import fcntl

    root = tmp_path / ".flow-steward"
    root.mkdir()
    with (root / "install.lock").open("w+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process = Process(
            '{"schema":"flow-steward.cli.v1","ok":true,"command":"maintenance.status",'
            '"result":{"status":"disabled"}}'
        )
        assert _client(tmp_path, process).status().status == "disabled"
    assert not (root / "operations").exists()
