"""`doctor --fix` never runs alongside another lifecycle operation (the doctor task,
principle 2): refused by name, with nothing changed; without one, it repairs."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.doctor.contracts import DoctorCheck, DoctorFinding
from core.application.doctor.service import DoctorService
from core.entrypoints.cli import doctor
from core.infrastructure.installer.state_store import InstallerStateStore


class _Broken:
    def __init__(self) -> None:
        self.broken = True
        self.repairs = 0


def _service(world: _Broken) -> DoctorService:
    def diagnose(_probe) -> DoctorFinding:
        if world.broken:
            return DoctorFinding("stack_stopped", "fail", cause="fs-app", repairable=True)
        return DoctorFinding("stack_stopped", "ok")

    def repair(_probe, _finding) -> str:
        world.repairs += 1
        world.broken = False
        return "started"

    return DoctorService(
        [DoctorCheck("stack_stopped", "Services running", diagnose, repair)], profile="scalable"
    )


def _state(tmp_path: Path) -> InstallerStateStore:
    root = tmp_path / ".flow-steward"
    return InstallerStateStore(root, lifecycle_state_root=root)


def _args() -> argparse.Namespace:
    return argparse.Namespace(ids=[], confirm=True, json=False)


def _hold(path: Path, payload: dict[str, str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, json.dumps(payload).encode())
    return fd


def _fix(tmp_path: Path, world: _Broken) -> int:
    config = SimpleNamespace(deployment_profile="scalable")
    return doctor._fix(_service(world), SimpleNamespace(), _state(tmp_path), config, _args())


def test_fix_during_a_config_apply_is_refused_by_name_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state(tmp_path)
    operations = state.lifecycle_lock_path.parent
    operations.mkdir(parents=True, mode=0o700)
    (operations / "op-apply.json").write_text(
        json.dumps({"kind": "config_apply", "current_step": "apply_runtime"}), encoding="utf-8"
    )
    fd = _hold(state.lifecycle_lock_path, {"operation_id": "op-apply"})
    world = _Broken()
    try:
        code = _fix(tmp_path, world)
    finally:
        os.close(fd)

    out = capsys.readouterr()
    assert code == 6
    assert "Operation op-apply is running: config apply, step apply_runtime." in out.err
    assert "Nothing was changed." in out.err
    assert world.repairs == 0


def test_fix_during_install_sh_is_refused_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state(tmp_path)
    fd = _hold(state.lock_path, {})
    world = _Broken()
    try:
        code = _fix(tmp_path, world)
    finally:
        os.close(fd)

    out = capsys.readouterr()
    assert code == 6
    assert "Another Flow Steward installer operation is already active." in out.err
    assert "Nothing was changed." in out.err
    assert world.repairs == 0


def test_fix_with_no_operation_running_repairs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The green direction of the two refusals above."""
    world = _Broken()

    code = _fix(tmp_path, world)

    assert code == 0, capsys.readouterr()
    assert world.repairs == 1
    assert not world.broken
