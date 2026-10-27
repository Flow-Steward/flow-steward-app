"""
Flow Steward Worker Manager — supervises one dedicated worker process per project.

Usage:
  python -m core.infrastructure.runtime.worker.manager

  # Optional: restrict this manager to a subset of project IDs
  python -m core.infrastructure.runtime.worker.manager --project-ids <uuid>,<uuid>

Design:
  - Discovers project IDs from the platform projects table.
  - Launches one `python -m core.entrypoints.cli.worker_process --project-id <id>` child per project.
  - Restarts crashed children after a short delay.
  - Stops children when a project disappears from the desired set.
  - Ready-queue + lease scheduling is the default runtime and is launched via
    `python -m core.entrypoints.cli.worker_process` directly (without this manager).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger("platform.worker_manager")


def _parse_project_ids(raw: str | None) -> set[str] | None:
    """Parse a comma-separated project ID list from CLI/env input."""
    if raw is None:
        return None

    items = [part.strip() for part in str(raw).split(",")]
    values = {part for part in items if part}
    return values or None


def _discover_project_ids() -> list[str]:
    """Return all configured project IDs from the platform database."""
    from core.infrastructure.config.runtime_config import CONFIG
    from core.infrastructure.db.platform_stores import PlatformProjectStore

    dsn = CONFIG.get("fs_observability_dsn", "") or CONFIG.get("fs_db_dsn", "")
    if not dsn:
        raise RuntimeError("Flow Steward logger DSN is not configured")

    bl = PlatformProjectStore(dsn)
    projects = bl.get_projects() or []
    return sorted({str(project["id"]) for project in projects if project.get("id") is not None})


def _signal_process_tree(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child process group when possible, else fall back to the child PID."""
    signal_value = str(int(sig))

    try:
        result = subprocess.run(
            ["kill", f"-{signal_value}", "--", f"-{proc.pid}"],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            return
    except Exception:
        pass

    with contextlib.suppress(Exception):
        subprocess.run(
            ["kill", f"-{signal_value}", "--", str(proc.pid)],
            capture_output=True,
            check=False,
            text=True,
        )


@dataclass
class ManagedWorker:
    project_id: str
    process: subprocess.Popen
    started_at: float


class ProjectWorkerManager:
    """Supervise one dedicated worker child process per project."""

    def __init__(
        self,
        *,
        discover_projects: Callable[[], list[str]] | None = None,
        project_filter: set[str] | None = None,
        discovery_interval: float = 10.0,
        restart_delay: float = 2.0,
        shutdown_timeout: float = 20.0,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._discover_projects = discover_projects or _discover_project_ids
        self._project_filter = set(project_filter) if project_filter else None
        self._discovery_interval = max(1.0, float(discovery_interval))
        self._restart_delay = max(0.0, float(restart_delay))
        self._shutdown_timeout = max(1.0, float(shutdown_timeout))
        self._popen_factory = popen_factory
        self._monotonic = monotonic
        self._sleep_fn = sleep_fn
        self._children: dict[str, ManagedWorker] = {}
        self._last_spawn_attempt: dict[str, float] = {}
        self._stopping = False

    @property
    def children(self) -> dict[str, ManagedWorker]:
        return self._children

    def request_stop(self) -> None:
        self._stopping = True

    def run(self) -> None:
        logger.info(
            "Starting worker manager (discovery_interval=%ss restart_delay=%ss filter=%s)",
            self._discovery_interval,
            self._restart_delay,
            sorted(self._project_filter) if self._project_filter else "all",
        )
        try:
            while not self._stopping:
                self.sync_once()
                self._sleep_fn(self._discovery_interval)
        finally:
            self.shutdown_all()

    def sync_once(self) -> list[str] | None:
        """Discover desired project workers and reconcile child processes."""
        try:
            desired_ids = self._desired_project_ids()
        except Exception as exc:
            logger.warning("Project discovery failed; keeping current workers unchanged: %s", exc)
            return None

        now = self._monotonic()
        self._reap_exited_children()

        for project_id in tuple(self._children):
            if project_id not in desired_ids:
                self._stop_child(project_id, reason="project removed from desired set")

        for project_id in desired_ids:
            if project_id in self._children:
                continue

            last_attempt = self._last_spawn_attempt.get(project_id)
            if last_attempt is not None and (now - last_attempt) < self._restart_delay:
                continue

            self._spawn_child(project_id, now=now)

        return desired_ids

    def shutdown_all(self) -> None:
        for project_id in tuple(self._children):
            self._stop_child(project_id, reason="manager shutdown")

    def _desired_project_ids(self) -> list[str]:
        project_ids = sorted({str(pid) for pid in self._discover_projects()})
        if self._project_filter is not None:
            project_ids = [pid for pid in project_ids if pid in self._project_filter]
        return project_ids

    def _spawn_child(self, project_id: str, *, now: float) -> None:
        cmd = [
            sys.executable,
            "-m",
            "core.entrypoints.cli.worker_process",
            "--project-id",
            str(project_id),
        ]
        logger.info("Starting dedicated worker for project_id=%s: %s", project_id, " ".join(cmd))
        process = self._popen_factory(
            cmd,
            env=_runtime_env.copy(),
            start_new_session=True,
        )
        self._children[project_id] = ManagedWorker(
            project_id=project_id,
            process=process,
            started_at=now,
        )
        self._last_spawn_attempt[project_id] = now

    def _reap_exited_children(self) -> None:
        for project_id, child in tuple(self._children.items()):
            returncode = child.process.poll()
            if returncode is None:
                continue

            logger.warning(
                "Dedicated worker for project_id=%s exited with returncode=%s; scheduling restart",
                project_id,
                returncode,
            )
            self._children.pop(project_id, None)

    def _stop_child(self, project_id: str, *, reason: str) -> None:
        child = self._children.pop(project_id, None)
        if child is None:
            return

        process = child.process
        if process.poll() is not None:
            return

        logger.info("Stopping dedicated worker for project_id=%s (%s)", project_id, reason)
        _signal_process_tree(process, signal.SIGTERM)
        try:
            process.wait(timeout=self._shutdown_timeout)
            return
        except subprocess.TimeoutExpired:
            logger.warning(
                "Dedicated worker for project_id=%s did not stop within %ss; killing",
                project_id,
                self._shutdown_timeout,
            )

        _signal_process_tree(process, signal.SIGKILL)
        with contextlib.suppress(Exception):
            process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Flow Steward per-project worker supervisor")
    parser.add_argument(
        "--project-ids",
        type=str,
        default=_runtime_env.get("FS_WORKER_PROJECT_IDS", ""),
        help="Optional comma-separated allowlist of project IDs to supervise.",
    )
    parser.add_argument(
        "--discovery-interval",
        type=float,
        default=float(_runtime_env.get("FS_WORKER_DISCOVERY_INTERVAL", "10")),
        help="Seconds between project discovery scans.",
    )
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=float(_runtime_env.get("FS_WORKER_RESTART_DELAY", "2")),
        help="Minimum seconds between restart attempts for the same project worker.",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=float(_runtime_env.get("FS_WORKER_SHUTDOWN_TIMEOUT", "20")),
        help="Seconds to wait for a child worker to stop before killing it.",
    )
    args = parser.parse_args()

    try:
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.observability.runtime_logging import configure_runtime_logging
        from core.infrastructure.observability.sentry import init_sentry

        configure_runtime_logging(component="worker_manager", config=CONFIG)
        init_sentry(config=CONFIG, component="worker_manager")
    except Exception as exc:
        logger.warning("Sentry bootstrap failed (non-fatal): %s", exc)

    manager = ProjectWorkerManager(
        project_filter=_parse_project_ids(args.project_ids),
        discovery_interval=args.discovery_interval,
        restart_delay=args.restart_delay,
        shutdown_timeout=args.shutdown_timeout,
    )

    def _handle_signal(signum, _frame):
        signame = signal.Signals(signum).name
        logger.info("Received %s; stopping worker manager", signame)
        manager.request_stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    manager.run()


if __name__ == "__main__":
    main()
