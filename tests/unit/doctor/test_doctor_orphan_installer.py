"""orphan_installer (the doctor task, D3; the 22-hour orphans): an installer container
whose ./install.sh is gone is stopped by doctor --fix; one whose ./install.sh lives is
never touched. Which ./install.sh lives is the launcher's to say -- it runs on the host,
and the doctor's container cannot see host processes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.doctor import repairs
from core.application.doctor.contracts import CheckBlocked
from core.infrastructure.installer import installation_doctor as doctor_probe
from core.infrastructure.installer.state_store import InstallerStateStore

_ORPHAN = "a" * 64
_WORKING = "b" * 64
_A_DOCTOR = "c" * 64
_OLD_UNLABELLED = "d" * 64


class _Gateway:
    def __init__(self, containers: list[dict[str, str]]) -> None:
        self.containers = containers
        self.stopped: list[str] = []

    def management_containers(self) -> list[dict[str, str]]:
        return list(self.containers)

    def stop_container(self, container_id: str) -> bool:
        self.stopped.append(container_id)
        return True


def _containers() -> list[dict[str, str]]:
    return [
        {"id": _ORPHAN, "module": "installer", "host_pid": "111"},  # its install.sh is gone
        {"id": _WORKING, "module": "installer", "host_pid": "222"},  # install.sh 222 lives
        {"id": _A_DOCTOR, "module": "doctor", "host_pid": "333"},  # someone's plain doctor
    ]


@pytest.fixture
def state(tmp_path: Path) -> InstallerStateStore:
    root = tmp_path / ".flow-steward"
    root.mkdir()
    return InstallerStateStore(root, lifecycle_state_root=tmp_path)


def _probe(state, gateway, *, holding: bool = False) -> doctor_probe.InstallationDoctorProbe:
    probe = doctor_probe.InstallationDoctorProbe(
        root=state.root.parent,
        gateway=gateway,
        state=state,
        config=SimpleNamespace(deployment_profile="compact"),
        log=doctor_probe.TemporaryDoctorLog(),
    )
    probe.holding = holding
    return probe


def test_the_container_of_a_running_install_sh_is_never_an_orphan(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auditor's red test: install.sh 222 lives, so its container stays."""
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_DOCTOR_LIVE_INSTALLER_PID": "222"})

    orphans = _probe(state, _Gateway(_containers())).orphan_installers()

    assert orphans == [(_ORPHAN, "111")]  # not the working one, not a doctor


def test_with_no_install_sh_alive_an_unlabelled_old_installer_is_an_orphan_too(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_DOCTOR_LIVE_INSTALLER_PID": "none"})
    old = {"id": _OLD_UNLABELLED, "module": "installer", "host_pid": ""}

    orphans = _probe(state, _Gateway([old])).orphan_installers()

    assert orphans == [(_OLD_UNLABELLED, "")]


def test_an_unlabelled_installer_is_left_while_some_install_sh_lives(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It cannot say whose it is, so it may be the live one's."""
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_DOCTOR_LIVE_INSTALLER_PID": "222"})
    old = {"id": _OLD_UNLABELLED, "module": "installer", "host_pid": ""}

    assert _probe(state, _Gateway([old])).orphan_installers() == []


def test_without_the_launchers_word_nothing_is_guessed(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_probe, "runtime_env", {})

    probe = _probe(state, _Gateway(_containers()))
    with pytest.raises(CheckBlocked):
        probe.orphan_installers()


def test_the_doctors_own_container_is_not_its_orphan(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_DOCTOR_LIVE_INSTALLER_PID": "none"})
    monkeypatch.setattr(doctor_probe.socket, "gethostname", lambda: _ORPHAN[:12])

    assert _probe(state, _Gateway(_containers()[:1])).orphan_installers() == []


def test_fix_stops_the_orphan_while_it_owns_the_installation(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_INSTALLER_HOST_PID": "444"})
    (state.root / "installer-host.pid").write_text("444\n", encoding="utf-8")
    gateway = _Gateway([_containers()[0]])

    done = _probe(state, gateway, holding=True).stop_orphan_installers()

    assert gateway.stopped == [_ORPHAN]
    assert done == f"Stopped installer containers nobody ran: {_ORPHAN[:12]}."


def test_fix_stops_nothing_once_another_install_sh_took_the_installation(
    state: InstallerStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-check right before `docker stop`: the record is no longer this run's."""
    monkeypatch.setattr(doctor_probe, "runtime_env", {"FS_INSTALLER_HOST_PID": "444"})
    (state.root / "installer-host.pid").write_text("555\n", encoding="utf-8")
    gateway = _Gateway([_containers()[0]])

    probe = _probe(state, gateway, holding=True)
    with pytest.raises(RuntimeError, match=r"another \./install\.sh took the installation"):
        probe.stop_orphan_installers()

    assert gateway.stopped == []


def test_the_check_names_orphans_and_passes_without_them() -> None:
    (check,) = [c for c in repairs.repair_checks() if c.check_id == "orphan_installer"]
    with_orphan = SimpleNamespace(
        orphan_installers=lambda: [(_ORPHAN, "111")], installation_lock_held=lambda: False
    )
    without = SimpleNamespace(orphan_installers=list)

    finding = check.diagnose(with_orphan)

    assert finding.repairable
    assert f"{_ORPHAN[:12]} (its ./install.sh, process 111, is gone)" in finding.cause
    assert check.diagnose(without).status == "ok"


# -- the gateway's listing ----------------------------------------------------------------


def test_only_this_installations_management_containers_are_listed(tmp_path: Path) -> None:
    from core.infrastructure.installer.gateway import DockerInstallerGateway
    from core.infrastructure.installer.process import ProcessOutcome

    root = str(tmp_path)
    rows = "\n".join(
        (
            f'{_ORPHAN}\tinstaller\t{root}\t111\t["-m","core.entrypoints.cli.installer"]\t{root}|',
            f'{_WORKING}\tinstaller\t/srv/other\t222\t["-m","core.entrypoints.cli.installer"]\t/srv/other|',
            f'{_OLD_UNLABELLED}\t\t\t\t["-m","core.entrypoints.cli.installer"]\t{root}|',
            f'{_A_DOCTOR}\t\t\t\t["sleep","1"]\t{root}|',
        )
    )

    class _Docker:
        def run(self, command, **_kwargs):
            out = "\n".join((_ORPHAN, _WORKING, _OLD_UNLABELLED, _A_DOCTOR))
            if command[:3] == ["docker", "container", "inspect"]:
                out = rows
            return ProcessOutcome(command=tuple(command), returncode=0, stdout=out)

    (tmp_path / "infra/docker").mkdir(parents=True)
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=InstallerStateStore(tmp_path / ".flow-steward", lifecycle_state_root=tmp_path),
        process=_Docker(),
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-mine"},
    )

    listed = gateway.management_containers()

    assert listed == [
        {"id": _ORPHAN, "module": "installer", "host_pid": "111"},
        {"id": _OLD_UNLABELLED, "module": "installer", "host_pid": ""},
    ]
