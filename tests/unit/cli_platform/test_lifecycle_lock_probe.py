"""AC17-R156: the launcher asks the running fs-cli whether a lifecycle operation is alive."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.entrypoints.cli import lifecycle_lock_probe
from core.infrastructure.lifecycle.state_store import LifecycleStateStore


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setitem(
        lifecycle_lock_probe.runtime_env, "FS_DOCKER_HOST_PROJECT_ROOT", str(tmp_path)
    )
    return tmp_path


def test_an_operation_holding_the_lock_is_named(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with LifecycleStateStore(root).operation_lock("c15d47fe-config-apply", mutating=True):
        assert lifecycle_lock_probe.main() == 0

    assert capsys.readouterr().out.strip() == "c15d47fe-config-apply"


def test_nothing_is_running_once_the_holder_is_gone(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with LifecycleStateStore(root).operation_lock("finished-apply", mutating=True):
        pass

    assert lifecycle_lock_probe.main() == 3
    assert capsys.readouterr().out == ""
