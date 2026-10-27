"""Durable public job-admission pause and cooperative idle waiting."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from core.application.lifecycle.contracts import CancellationToken
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.ports import HeartbeatSink
from core.infrastructure.installer.state_store import _atomic_write_text

_PATH_FLOW_STEWARD_ENVIRONMENT_SHOW = "./flow-steward environment show"
_MESSAGE_REPAIR_THE_RUNTIME_MARKER_BEFORE_RETRYING = "Repair the runtime marker before retrying."

_POLL_SECONDS = 1.0
_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class AdmissionPauseOwnership:
    """Secret-free ownership proof for one lifecycle admission pause."""

    owner_operation_id: str
    created: bool


class PostgresActiveJobsReader:
    """Read active jobs through the database adapter owned by infrastructure."""

    def __init__(self, dsn: str) -> None:
        self._dsn = str(dsn)

    def __call__(self) -> list[dict[str, object]]:
        from core.infrastructure.db import job_activity_store
        from core.infrastructure.db.psycopg_runtime import connect

        connection = connect(self._dsn)
        try:
            return [
                row
                for row in job_activity_store.get_active_jobs(conn=connection)
                if str(row.get("status") or "").strip().lower() == "in_progress"
            ]
        finally:
            connection.close()


class LifecycleJobQuiescenceGateway:
    """Pause every public ingress through a shared marker, then wait safely."""

    def __init__(
        self,
        *,
        project_root: Path,
        active_jobs: Callable[[], Sequence[Mapping[str, object]]],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._marker = self._project_root / ".flow-steward/runtime/job-admission-paused"
        self._active_jobs = active_jobs
        self._sleep = sleep

    def stop_accepting(self, operation_id: str) -> AdmissionPauseOwnership:
        owner = self._validated_operation_id(operation_id)
        current = self._read_marker()
        if current is not None:
            if current != owner:
                raise LifecycleError.conflict(
                    operation_id=current,
                    cause="Job admission is paused by another lifecycle operation.",
                )
            return AdmissionPauseOwnership(owner_operation_id=owner, created=False)
        _atomic_write_text(
            self._marker,
            json.dumps(
                {"schema_version": 1, "owner_operation_id": owner},
                sort_keys=True,
            )
            + "\n",
            mode=0o600,
        )
        return AdmissionPauseOwnership(owner_operation_id=owner, created=True)

    def reconcile_pause(
        self,
        unfinished_operation_ids: Sequence[str],
    ) -> AdmissionPauseOwnership | None:
        """Retain a live owner's marker and remove only a proven stale marker."""
        unfinished = tuple(
            dict.fromkeys(map(self._validated_operation_id, unfinished_operation_ids))
        )
        if len(unfinished) > 1:
            raise LifecycleError.recovery_required(
                code="environment_admission_pause_ambiguous",
                cause="Job admission ownership is ambiguous across unfinished operations.",
                remediation="Reconcile the lifecycle journals before retrying.",
                step="recovery",
                recovery_command=_PATH_FLOW_STEWARD_ENVIRONMENT_SHOW,
            )
        current = self._read_marker()
        if current is None:
            return None
        if unfinished and current == unfinished[0]:
            return AdmissionPauseOwnership(owner_operation_id=current, created=False)
        if not self.resume_accepting(current):  # pragma: no cover - same-owner invariant
            raise LifecycleError.recovery_required(
                code="environment_admission_pause_recovery_required",
                cause="The stale job-admission marker could not be reconciled.",
                remediation="Repair the owner-only runtime marker before retrying.",
                step="recovery",
                recovery_command=_PATH_FLOW_STEWARD_ENVIRONMENT_SHOW,
            )
        return None

    def wait_for_idle(
        self,
        *,
        cancellation: CancellationToken,
        heartbeat: HeartbeatSink,
    ) -> None:
        idle_observed = False
        while True:
            if cancellation.requested:
                raise LifecycleError.interrupted(
                    step="quiesce_jobs",
                    recovery_command="flow-steward environment show",
                    forced=cancellation.forced,
                )
            active = tuple(self._active_jobs())
            if not active:
                if idle_observed:
                    return
                idle_observed = True
                heartbeat("Confirming job admission remains idle.")
                self._sleep(_POLL_SECONDS)
                continue
            idle_observed = False
            suffix = "" if len(active) == 1 else "s"
            heartbeat(f"Waiting for {len(active)} active job{suffix} to finish safely.")
            self._sleep(_POLL_SECONDS)

    def pause_owned(self, operation_id: str) -> bool:
        """Return whether the durable pause marker is owned by this operation."""
        owner = self._validated_operation_id(operation_id)
        return self._read_marker() == owner

    def resume_accepting(self, operation_id: str) -> bool:
        owner = self._validated_operation_id(operation_id)
        current = self._read_marker()
        if current is None or current != owner:
            return False
        try:
            self._marker.unlink()
        except FileNotFoundError:
            return False
        return True

    def _read_marker(self) -> str | None:
        if not self._marker.exists():
            return None
        if self._marker.is_symlink() or not self._marker.is_file():
            raise LifecycleError.recovery_required(
                code="environment_admission_pause_invalid",
                cause="The job-admission marker is not a regular owner-controlled file.",
                remediation=_MESSAGE_REPAIR_THE_RUNTIME_MARKER_BEFORE_RETRYING,
                step="recovery",
                recovery_command=_PATH_FLOW_STEWARD_ENVIRONMENT_SHOW,
            )
        try:
            payload = json.loads(self._marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LifecycleError.recovery_required(
                code="environment_admission_pause_invalid",
                cause="The job-admission marker is unreadable.",
                remediation=_MESSAGE_REPAIR_THE_RUNTIME_MARKER_BEFORE_RETRYING,
                step="recovery",
                recovery_command=_PATH_FLOW_STEWARD_ENVIRONMENT_SHOW,
            ) from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "owner_operation_id"}
            or payload.get("schema_version") != 1
        ):
            raise LifecycleError.recovery_required(
                code="environment_admission_pause_invalid",
                cause="The job-admission marker has an invalid schema.",
                remediation=_MESSAGE_REPAIR_THE_RUNTIME_MARKER_BEFORE_RETRYING,
                step="recovery",
                recovery_command=_PATH_FLOW_STEWARD_ENVIRONMENT_SHOW,
            )
        return self._validated_operation_id(str(payload["owner_operation_id"]))

    @staticmethod
    def _validated_operation_id(operation_id: str) -> str:
        normalized = str(operation_id).strip()
        if not _SAFE_OPERATION_ID.fullmatch(normalized):
            raise ValueError("Lifecycle admission owner operation id is invalid")
        return normalized


__all__ = ["AdmissionPauseOwnership", "LifecycleJobQuiescenceGateway"]
