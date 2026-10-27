"""Managed roles and an adopted chain must share the outer shutdown allowance."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest
import yaml

from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
from core.infrastructure.runtime.compact import supervisor as supervisor_module
from core.infrastructure.runtime.compact.process_spec import ProcessSpec

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("grace,reserve", [(4, 1), (180, 10)])
def test_shutdown_reaps_adopted_chain_before_late_managed_exit_within_one_budget(
    monkeypatch: pytest.MonkeyPatch, grace: int, reserve: int
) -> None:
    """Waiting for the last managed role must not postpone the adopted-chain sweep."""
    settings = CompactRuntimeSettings.from_environment(
        {
            "FS_DEPLOYMENT_PROFILE": "compact",
            "FS_COMPACT_SHUTDOWN_GRACE_SECONDS": str(grace),
            "FS_COMPACT_KILL_RESERVE_SECONDS": str(reserve),
        }
    )
    now = 30.0
    chain = [900_001, 900_002]
    killed_at: dict[int, float] = {}
    reaped: list[int] = []
    managed_killed = False

    class Process:
        pid = 900_000

        def poll(self) -> int | None:
            return -9 if managed_killed and now >= 30.0 + grace - 0.05 else None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            assert self.poll() == -9
            return -9

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    def signal_process(_process: Process, signum: int) -> None:
        nonlocal managed_killed
        if signum == signal.SIGKILL:
            managed_killed = True

    def children(_parent: int) -> set[int]:
        # A dead role's A -> B chain arrives during the forced-cleanup phase;
        # B is handed to the subreaper only after A has died and been reaped.
        return {chain[0]} if managed_killed and chain else set()

    def kill(pid: int, signum: int) -> None:
        assert signum == signal.SIGKILL
        killed_at[pid] = now

    def waitpid(pid: int, _options: int) -> tuple[int, int]:
        if now + 1e-9 < killed_at[pid] + 0.1:
            return (0, 0)
        assert chain[0] == pid
        chain.pop(0)
        reaped.append(pid)
        return (pid, 0)

    original_exists = Path.exists

    def exists(path: Path) -> bool:
        if str(path) in {"/proc/900001", "/proc/900002"}:
            return int(path.name) in chain
        return original_exists(path)

    monkeypatch.setattr(supervisor_module, "_enable_child_subreaper", lambda: True)
    monkeypatch.setattr(supervisor_module, "_linux_child_pids", children)
    monkeypatch.setattr(supervisor_module, "_signal_pid", kill)
    monkeypatch.setattr(supervisor_module.os, "waitpid", waitpid)
    monkeypatch.setattr(Path, "exists", exists)
    spec = ProcessSpec(role="app", command=("unused",), uid=0, gid=0, port=None, environment={})
    owner = supervisor_module.CompactSupervisor(
        specs=(spec,),
        settings=settings,
        process_factory=lambda _spec: Process(),
        signal_process=signal_process,
        monotonic=lambda: now,
        sleep=sleep,
    )
    owner.start()

    assert owner.shutdown() is True

    assert reaped == [900_001, 900_002]
    assert owner._children == {}
    assert now <= 30.0 + grace + 1e-9
    assert killed_at[900_002] < 30.0 + grace - 0.05


def test_compact_compose_stop_allowance_matches_complete_supervisor_budget() -> None:
    """Compose's configured allowance covers adopted cleanup as well as role cleanup."""
    compose = yaml.safe_load((ROOT / "infra/docker/docker-compose.compact.yaml").read_text())
    runtime = compose["services"]["compact-runtime"]
    assert runtime["stop_grace_period"] == "${FS_COMPACT_SHUTDOWN_GRACE_SECONDS:-180}s"
    assert runtime["environment"]["FS_COMPACT_SHUTDOWN_GRACE_SECONDS"] == (
        "${FS_COMPACT_SHUTDOWN_GRACE_SECONDS:-180}"
    )
