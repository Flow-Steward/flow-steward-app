"""PID-1 process ownership for the fixed Compact runtime."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
from core.infrastructure.runtime.compact.process_spec import ProcessSpec

_PATH_PROC = "/proc"
_UID_SIGNAL_CODE = """
import os, sys
os.seteuid(int(sys.argv[1]))
try:
    os.kill(int(sys.argv[2]), int(sys.argv[3]))
except ProcessLookupError:
    pass
"""


@dataclass(slots=True)
class RestartBudget:
    limit: int
    window_seconds: float
    initial_backoff: float
    maximum_backoff: float
    _failures: deque[float] = field(default_factory=deque)

    def record_failure(self, now: float) -> float | None:
        while self._failures and now - self._failures[0] > self.window_seconds:
            self._failures.popleft()
        if len(self._failures) >= self.limit:
            return None
        self._failures.append(now)
        delay = self.initial_backoff * (2 ** (len(self._failures) - 1))
        return min(delay, self.maximum_backoff)

    def record_ready(self, _now: float) -> None:
        """Readiness does not reset crash-loop history inside the active window."""


class ManagedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class CompactConfigurationError(RuntimeError):
    """A fixed role could not be created from the validated configuration."""


def _stat_after_name(pid: str) -> list[str]:
    """The fields of /proc/<pid>/stat after the command name: state, ppid, pgrp, session.

    The name is parenthesised and may itself contain spaces and parentheses: a child
    called "extension job" shifted every field of a plain split, its parent pid was read
    from the wrong column, and it was not found -- so a replacement worker started while
    the old subprocess still ran (P1-1, found again by External review).
    """
    stat = (Path(_PATH_PROC) / pid / "stat").read_text(encoding="utf-8")
    return stat.rsplit(")", 1)[1].split()


def _linux_child_pids(parent_pid: int) -> set[int]:
    result: set[int] = set()
    with suppress(OSError):
        for entry in os.scandir(_PATH_PROC):
            if not entry.name.isdecimal():
                continue
            with suppress(OSError, ValueError, IndexError):
                if int(_stat_after_name(entry.name)[1]) == parent_pid:
                    result.add(int(entry.name))
    return result


def _process_table() -> dict[int, tuple[int, int]]:
    """pid -> (parent pid, session id), from /proc; empty where there is no /proc."""
    table: dict[int, tuple[int, int]] = {}
    with suppress(OSError):
        for entry in os.scandir(_PATH_PROC):
            if not entry.name.isdecimal():
                continue
            with suppress(OSError, ValueError, IndexError):
                fields = _stat_after_name(entry.name)
                table[int(entry.name)] = (int(fields[1]), int(fields[3]))
    return table


def _descendants(root: int, table: dict[int, tuple[int, int]]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, (parent, _session) in table.items():
        children.setdefault(parent, []).append(pid)
    found: set[int] = set()
    frontier = [root]
    while frontier:
        for child in children.get(frontier.pop(), ()):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


def _enable_child_subreaper() -> bool:
    if not Path(_PATH_PROC).is_dir():
        return False
    try:
        import ctypes

        return ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    except (AttributeError, OSError):
        return False


def _drop_privileges(uid: int, gid: int, supplementary_gids: Sequence[int]) -> Callable[[], None]:
    def _apply() -> None:
        os.setgroups(list(supplementary_gids))
        os.setgid(gid)
        os.setuid(uid)

    return _apply


def _default_process_factory(spec: ProcessSpec) -> ManagedProcess:
    return subprocess.Popen(  # nosec B603 - fixed commands from build_process_specs
        list(spec.command),
        env=spec.environment,
        start_new_session=True,
        pass_fds=spec.pass_fds,
        preexec_fn=_drop_privileges(spec.uid, spec.gid, spec.supplementary_gids),
    )


def _signal_pid(pid: int, signum: int) -> None:
    """Signal an isolated role even where root lacks CAP_KILL."""
    try:
        os.kill(pid, signum)
        return
    except PermissionError:
        if os.geteuid() != 0:
            raise
    try:
        status = (Path(_PATH_PROC) / str(pid) / "status").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProcessLookupError(pid) from exc
    uid = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("Uid:"))
    # Never change credentials in the threaded supervisor. This isolated helper changes
    # only its effective UID; real/saved UID 0 lets the parent stop it on timeout even
    # without CAP_KILL. No application environment, secrets, or imports enter it.
    subprocess.run(  # nosec B603 - fixed interpreter, code, and numeric arguments
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _UID_SIGNAL_CODE,
            str(uid),
            str(pid),
            str(signum),
        ],
        env={},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=1,
        check=True,
    )


def _signal_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except PermissionError:
        if os.geteuid() != 0:
            raise
        # A group may outlive its leader. Resolve every surviving member instead of
        # guessing the missing leader's UID; each PID gets its own numeric helper.
        for entry in os.scandir(_PATH_PROC):
            if not entry.name.isdecimal():
                continue
            with suppress(FileNotFoundError, ProcessLookupError):
                if int(_stat_after_name(entry.name)[2]) == pgid:
                    _signal_pid(int(entry.name), signum)


def _signal_process_tree(process: ManagedProcess, signum: int) -> None:
    descendants: list[int] = []
    parent_by_pid: dict[int, int] = {}
    with suppress(OSError):
        for entry in os.scandir(_PATH_PROC):
            if not entry.name.isdecimal():
                continue
            with suppress(OSError, ValueError, IndexError):
                parent_by_pid[int(entry.name)] = int(_stat_after_name(entry.name)[1])
        frontier = [process.pid]
        while frontier:
            parent = frontier.pop()
            children = [pid for pid, ppid in parent_by_pid.items() if ppid == parent]
            descendants.extend(children)
            frontier.extend(children)
    for descendant in reversed(descendants):
        with suppress(ProcessLookupError):
            _signal_pid(descendant, signum)
    try:
        _signal_group(process.pid, signum)
    except ProcessLookupError:
        return


def _signal_process(process: ManagedProcess, signum: int) -> None:
    if signum == signal.SIGTERM:
        with suppress(ProcessLookupError):
            _signal_pid(process.pid, signum)
        return
    _signal_process_tree(process, signum)


class CompactSupervisor:
    """Own and restart the fixed Compact child inventory."""

    def __init__(
        self,
        *,
        specs: Sequence[ProcessSpec],
        settings: CompactRuntimeSettings,
        process_factory: Callable[[ProcessSpec], ManagedProcess] = _default_process_factory,
        signal_process: Callable[[ManagedProcess, int], None] = _signal_process,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        role_state: Callable[[str, bool], None] | None = None,
        readiness: Callable[[], bool] | None = None,
        runtime_reset: Callable[[], None] | None = None,
        role_reset: Callable[[str], None] | None = None,
    ) -> None:
        self._specs = tuple(specs)
        self._settings = settings
        self._process_factory = process_factory
        self._signal_process = signal_process
        self._monotonic = monotonic
        self._sleep = sleep
        self._role_state = role_state or (lambda _role, _ready: None)
        self._readiness = readiness
        self._runtime_reset = runtime_reset or (lambda: None)
        self._role_reset = role_reset or (lambda _role: None)
        self._shutdown_requested = threading.Event()
        self._preexisting_children = _linux_child_pids(os.getpid())
        self._subreaper_enabled = _enable_child_subreaper()
        self._children: dict[str, tuple[ProcessSpec, ManagedProcess]] = {}
        self._budgets = {
            spec.role: RestartBudget(
                limit=settings.restart_limit,
                window_seconds=settings.restart_window_seconds,
                initial_backoff=settings.restart_backoff_seconds,
                maximum_backoff=settings.restart_backoff_max_seconds,
            )
            for spec in self._specs
        }

    def request_shutdown(self, _signum: int = signal.SIGTERM, _frame: object | None = None) -> None:
        self._shutdown_requested.set()

    def start(self) -> None:
        self._runtime_reset()
        try:
            for spec in self._specs:
                self._children[spec.role] = (spec, self._process_factory(spec))
        except Exception as exc:
            self.shutdown()
            raise CompactConfigurationError(f"failed to create Compact role {spec.role}") from exc

    def run(self) -> int:
        try:
            self.start()
        except CompactConfigurationError:
            return 78
        started_at = self._monotonic()
        not_ready_since = started_at
        ever_ready = False
        while not self._shutdown_requested.is_set():
            if self._readiness is not None:
                ready = self._readiness()
                if ready:
                    ever_ready = True
                    not_ready_since = self._monotonic()
                else:
                    budget = (
                        self._settings.dependency_timeout_seconds
                        if ever_ready
                        else self._settings.startup_timeout_seconds
                    )
                    if self._monotonic() - not_ready_since >= budget:
                        self.shutdown()
                        return 1
            failed_role: str | None = None
            for role, (_spec, process) in tuple(self._children.items()):
                if process.poll() is not None:
                    failed_role = role
                    break
            if failed_role is None:
                self._sleep(self._settings.health_interval_seconds)
                continue
            delay = self._budgets[failed_role].record_failure(self._monotonic())
            if delay is None:
                # A role that keeps failing ends the runtime, as before: the container
                # exits and its restart policy decides, rather than running without it.
                self.shutdown()
                return 1
            if self._contain_role_failure(failed_role):
                # Review P2: only the role that died is restarted. Restarting the whole
                # group took the application, MCP and every other worker's jobs down
                # with one crashed worker.
                self._sleep(delay)
                if self._shutdown_requested.is_set():
                    break
                try:
                    self._children[failed_role] = (
                        self._spec_for(failed_role),
                        self._process_factory(self._spec_for(failed_role)),
                    )
                except Exception:
                    self.shutdown()
                    return 1
                continue
            # The dead role's tree could not be proven gone on its own -- a descendant
            # left its session, or there is no /proc to ask. Fall back to the group.
            if not self.shutdown():
                # P1-1: something the old group started is still alive. Starting a new
                # group next to it is how an external effect gets done twice; exiting
                # hands the container to its restart policy, which ends the whole tree.
                return 1
            self._sleep(delay)
            if self._shutdown_requested.is_set():
                break
            try:
                self.start()
            except CompactConfigurationError:
                return 1
        self.shutdown()
        return 0

    def _spec_for(self, role: str) -> ProcessSpec:
        return next(spec for spec in self._specs if spec.role == role)

    def _contain_role_failure(self, role: str) -> bool:
        """Clear a dead role's own tree; True only when it is proven gone on its own.

        Each role starts in its own session (start_new_session), so everything it
        started carries its pid as session id -- including what the subreaper adopted
        when the role died. Those are killed and reaped until a pass finds none, within
        the kill reserve: the P1-1 guarantee, narrowed to one role, that no new instance
        starts beside the old tree. Only processes inside this supervisor's own tree are
        considered, so a pid that happens to match does not reach anything else.

        False -- fall back to the group -- when that cannot be proven: no /proc or no
        subreaper, a descendant that is still alive at the deadline, or an adopted
        process that belongs to no live role (it left the dead role's session).
        """
        _spec, process = self._children.pop(role)
        with suppress(Exception):
            process.wait(timeout=0)
        self._role_reset(role)
        if not self._subreaper_enabled:
            return False
        session = process.pid
        supervisor = os.getpid()
        deadline = self._monotonic() + self._settings.kill_reserve_seconds
        while True:
            table = _process_table()
            ours = _descendants(supervisor, table)
            members = {pid for pid in ours if table[pid][1] == session}
            for pid in members:
                with suppress(ProcessLookupError):
                    _signal_pid(pid, signal.SIGKILL)
            for pid in self._adopted_descendants():
                with suppress(ChildProcessError, ProcessLookupError):
                    os.waitpid(pid, os.WNOHANG)
            if not members:
                break
            if self._monotonic() >= deadline:
                return False
            self._sleep(min(0.05, max(0.0, deadline - self._monotonic())))
        table = _process_table()
        live_sessions = {child.pid for _spec, child in self._children.values()}
        stray = {
            pid
            for pid in self._adopted_descendants()
            if pid in table and table[pid][1] not in live_sessions
        }
        return not stray

    def shutdown(self) -> bool:
        """Stop every role and everything they started; True when nothing is left.

        P1-1: returning early whenever no managed role was left skipped the descendant
        cleanup entirely -- exactly the state a crash leaves, with the roles gone and
        what they started still running.
        """
        shutdown_deadline = self._monotonic() + self._settings.shutdown_grace_seconds
        if not self._children:
            return self._terminate_adopted_descendants(
                deadline=min(
                    shutdown_deadline, self._monotonic() + self._settings.kill_reserve_seconds
                ),
                reap_managed=True,
            )
        terminate_deadline = shutdown_deadline - self._settings.kill_reserve_seconds
        service_shutdown_reserve = min(
            10.0,
            max(
                1.0,
                (self._settings.shutdown_grace_seconds - self._settings.kill_reserve_seconds) / 4,
            ),
        )
        drain_deadline = max(self._monotonic(), terminate_deadline - service_shutdown_reserve)
        draining_roles = tuple(
            role for role in self._children if role == "control" or role.startswith("worker-")
        )
        self._signal_roles(draining_roles, signal.SIGTERM)
        for role in draining_roles:
            if not role.startswith("worker-"):
                self._role_state(role, False)
        while self._monotonic() < drain_deadline:
            self._reap_exited()
            if not any(role in self._children for role in draining_roles):
                break
            self._sleep(min(0.1, max(0.0, drain_deadline - self._monotonic())))
        service_roles = tuple(role for role in self._children if role not in draining_roles)
        self._signal_roles(service_roles, signal.SIGTERM)
        for role in service_roles:
            self._role_state(role, False)
        while self._monotonic() < terminate_deadline:
            self._reap_exited()
            if not self._children and not self._adopted_descendants():
                break
            self._sleep(min(0.1, max(0.0, terminate_deadline - self._monotonic())))
        for _spec, process in self._children.values():
            if process.poll() is None:
                self._signal_process(process, signal.SIGKILL)
        # Reap roles and adopted chains together inside the same remaining reserve.
        # Waiting for the last role first postponed the chain sweep and restarted a
        # second kill-reserve timer after Compose's complete shutdown allowance.
        return self._terminate_adopted_descendants(deadline=shutdown_deadline, reap_managed=True)

    def _signal_roles(self, roles: Sequence[str], signum: int) -> None:
        for role in roles:
            child = self._children.get(role)
            if child is None:
                continue
            _spec, process = child
            if process.poll() is None:
                self._signal_process(process, signum)

    def _terminate_adopted_descendants(
        self, *, deadline: float | None = None, reap_managed: bool = False
    ) -> bool:
        """Kill adopted descendants, optionally reaping stopped managed roles too.

        P1-1: this took one snapshot. In a chain role -> A -> B the role's death hands A
        over, A is in the snapshot and killed -- and only then is B handed over, after
        the snapshot was taken, so B survived next to the new worker group. Killing a
        process is what orphans its children, so the snapshot is taken again after every
        kill until a pass finds nothing new. Shutdown supplies its original deadline;
        a standalone orphan sweep receives one kill reserve. Neither a late role reap
        nor another adopted generation extends that deadline. Standalone sweeps leave
        managed roles alone; full shutdown also requires every stopped role to be reaped.
        """
        if deadline is None:
            deadline = self._monotonic() + self._settings.kill_reserve_seconds
        pending: set[int] = set()
        while True:
            if reap_managed:
                self._reap_exited()
            adopted = self._adopted_descendants()
            for pid in adopted - pending:
                with suppress(ProcessLookupError):
                    _signal_pid(pid, signal.SIGKILL)
            pending |= adopted
            for pid in tuple(pending):
                with suppress(ChildProcessError, ProcessLookupError):
                    waited, _status = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        pending.discard(pid)
                        continue
                if not Path(f"/proc/{pid}").exists():
                    pending.discard(pid)
            if (
                (not reap_managed or not self._children)
                and not pending
                and not self._adopted_descendants()
            ):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(min(0.05, max(0.0, deadline - self._monotonic())))

    def _adopted_descendants(self) -> set[int]:
        if not self._subreaper_enabled:
            return set()
        managed = {process.pid for _spec, process in self._children.values()}
        return _linux_child_pids(os.getpid()) - self._preexisting_children - managed

    def _reap_exited(self) -> None:
        for role, (_spec, process) in tuple(self._children.items()):
            if process.poll() is None:
                continue
            with suppress(Exception):
                process.wait(timeout=0)
            self._children.pop(role, None)
