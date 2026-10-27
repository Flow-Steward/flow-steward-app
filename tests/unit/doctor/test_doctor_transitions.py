"""P3 of the D3b acceptance (live on fs-acc, 2026-09-25): during a running ./install.sh a
plain doctor said no operation ran, reported the restart's 502 and refused connections
as failures, and offered --fix for an orphan that was still doing the update."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.doctor import repairs
from core.application.doctor.contracts import CheckBlocked
from core.infrastructure.installer import installation_doctor as doctor_probe
from core.infrastructure.installer.state_store import InstallerStateStore


@pytest.fixture
def state(tmp_path: Path) -> InstallerStateStore:
    root = tmp_path / ".flow-steward"
    root.mkdir()
    return InstallerStateStore(root, lifecycle_state_root=tmp_path)


class _Gateway:
    """Every service up; the runtime answers only what a test sets."""

    def __init__(self, *, health_error: str = "") -> None:
        self.health_error = health_error

    def stopped_services(self, _config, *, log) -> list[str]:
        return []

    def running_worker_count(self, _config) -> int:
        if self.health_error:
            raise OSError(self.health_error)
        return 1

    def public_route_problem(self, _config) -> tuple[str, str]:
        return ("The public URL x/health does not answer: HTTP Error 502.", "Check DNS")

    def queue_consumers(self, _config) -> int:
        return 0

    def management_containers(self) -> list[dict[str, str]]:
        return [{"id": "a" * 64, "module": "installer", "host_pid": "111"}]


def _probe(state, gateway) -> doctor_probe.InstallationDoctorProbe:
    return doctor_probe.InstallationDoctorProbe(
        root=state.root.parent,
        gateway=gateway,
        state=state,
        config=SimpleNamespace(deployment_profile="compact", worker_processes=1),
        log=doctor_probe.TemporaryDoctorLog(),
    )


def test_a_plain_doctor_sees_the_install_sh_the_launcher_found_alive(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_DOCTOR_LIVE_INSTALLER_PID": "1036597"})

    assert _probe(state, _Gateway()).operation_running() == "./install.sh (process 1036597)"


@pytest.mark.parametrize("read", ["public_route_problem", "queue_consumers", "worker_counts"])
def test_runtime_checks_are_not_checked_while_an_operation_runs(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_DOCTOR_LIVE_INSTALLER_PID": "1036597"})

    read_runtime = getattr(_probe(state, _Gateway()), read)
    with pytest.raises(CheckBlocked, match="an operation is running"):
        read_runtime()


def test_a_runtime_that_refuses_connections_is_not_checked(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_DOCTOR_LIVE_INSTALLER_PID": "none"})
    gateway = _Gateway(
        health_error="urllib.error.URLError: <urlopen error [Errno 111] Connection refused>"
    )

    probe = _probe(state, gateway)
    with pytest.raises(CheckBlocked, match="the runtime does not answer"):
        probe.worker_counts()


def test_with_nothing_running_the_runtime_checks_run(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The green side: no operation, a runtime that answers."""
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_DOCTOR_LIVE_INSTALLER_PID": "none"})

    probe = _probe(state, _Gateway())

    assert probe.worker_counts().running == 1
    assert probe.queue_consumers() == 0


def test_an_orphan_still_holding_the_lock_is_to_be_waited_for(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its ./install.sh is gone, but it is doing the update: --fix would refuse anyway."""
    import fcntl
    import os

    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_DOCTOR_LIVE_INSTALLER_PID": "none"})
    state.lock_path.write_text("", encoding="utf-8")
    fd = os.open(state.lock_path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    monkeypatch.setattr(doctor_probe, "flock_held", lambda path: path == state.lock_path)
    (check,) = [c for c in repairs.repair_checks() if c.check_id == "orphan_installer"]
    try:
        finding = check.diagnose(_probe(state, _Gateway()))
    finally:
        os.close(fd)

    assert finding.status == "warn"
    assert not finding.repairable
    assert "still performing the operation" in finding.cause
    assert finding.next_step == "Rerun ./flow-steward doctor after it has finished."
