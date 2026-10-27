"""Process runtime, serialization, and heartbeat helpers for graph nodes."""

from __future__ import annotations

import contextlib
import logging
import multiprocessing as mp
import signal
import traceback
from types import FrameType
from typing import TYPE_CHECKING, Any

from core.application.orchestration.models import normalize_phase_status
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

if TYPE_CHECKING:
    from core.application.orchestration.agents.base import BaseAgent
    from core.application.orchestration.state import OrchestratorState


logger = logging.getLogger(__name__)
_OBS_LOGGER_CACHE: dict[str, Any] = {}


class _AgentProcessCancellation(KeyboardInterrupt):
    """Unwind agent work to its process entry without treating cancellation as a result."""


def _cancel_agent_process(_signum: int, _frame: FrameType | None) -> None:
    # Exception handlers cannot turn cancellation into a successful agent output.
    raise _AgentProcessCancellation


def _agent_process_entry(agent: BaseAgent, agent_input, result_queue) -> None:
    """Child-process entrypoint for hard-killable agent execution."""
    # Fork inherits the worker's shutdown-only handler, which cannot stop a blocked agent.
    signal.signal(signal.SIGTERM, _cancel_agent_process)
    try:
        output = agent.execute(agent_input)
        result_queue.put({"ok": True, "output": _serialize_agent_output(output)})
    except _AgentProcessCancellation:
        return
    except Exception as exc:
        result_queue.put(
            {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "stderr": str(getattr(exc, "stderr", "") or ""),
            }
        )


def _serialize_agent_output(output: Any) -> dict:
    """Serialize agent output to builtin types for IPC queue transport."""
    if isinstance(output, dict):
        data = dict(output)
    elif hasattr(output, "model_dump"):
        data = dict(output.model_dump())
    elif hasattr(output, "__dict__"):
        data = dict(vars(output))
    else:
        data = {}
    return {
        "status": normalize_phase_status(data.get("status")),
        "artifacts": _to_builtin(
            data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
        ),
        "summary": str(data.get("summary") or ""),
        "confidence": _coerce_float(data.get("confidence"), default=0.0),
        "metadata": _to_builtin(
            data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        ),
        "operation_retry_signal": _to_builtin(
            data.get("operation_retry_signal")
            if isinstance(data.get("operation_retry_signal"), dict)
            else None
        ),
    }


def _to_builtin(value: Any) -> Any:
    """Best-effort conversion to queue-safe builtin types."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_builtin(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_builtin(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _to_builtin(value.model_dump())
        except Exception:
            return repr(value)
    if hasattr(value, "__dict__"):
        try:
            return _to_builtin(vars(value))
        except Exception:
            return repr(value)
    return repr(value)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _heartbeat_interval_seconds() -> float:
    try:
        value = float(_runtime_env.get("FS_AGENT_HEARTBEAT_SEC", "10"))
    except (TypeError, ValueError):
        value = 10.0
    return max(1.0, value)


def _cancel_poll_interval_seconds() -> float:
    try:
        value = float(_runtime_env.get("FS_CANCEL_POLL_SEC", "1"))
    except (TypeError, ValueError):
        value = 1.0
    return max(0.2, value)


def _process_join_grace_seconds() -> float:
    try:
        value = float(_runtime_env.get("FS_AGENT_KILL_GRACE_SEC", "2"))
    except (TypeError, ValueError):
        value = 2.0
    return max(0.2, value)


def _terminate_agent_process(proc: mp.Process, reason: str) -> None:
    """Terminate child process and force kill if it does not stop."""
    if not proc.is_alive():
        return
    proc.terminate()
    proc.join(timeout=_process_join_grace_seconds())
    if proc.is_alive():
        with contextlib.suppress(Exception):
            proc.kill()
        proc.join(timeout=1.0)
    logger.warning("Hard-stopped agent process pid=%s (%s)", proc.pid, reason)


def _get_observability_logger():
    """Return a cached Flow Steward logger for cancellation polling (best effort)."""
    try:
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.db.platform_stores import (
            PlatformJobStore,
            resolve_observability_dsn,
        )

        dsn = resolve_observability_dsn(
            CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or ""
        )
        if not dsn:
            return None
        cached = _OBS_LOGGER_CACHE.get(dsn)
        if cached is not None:
            return cached
        logger_obj = PlatformJobStore(dsn)
        _OBS_LOGGER_CACHE[dsn] = logger_obj
        return logger_obj
    except Exception:
        return None


def _is_job_cancelled(job_id: str) -> bool:
    """Return True when job status is currently cancelled."""
    if not str(job_id or "").strip():
        return False
    bl = _get_observability_logger()
    if bl is None:
        return False
    try:
        row = bl.get_job(job_id)
        return bool(row and str(row.get("status") or "").lower() == "cancelled")
    except Exception:
        return False


def _emit_agent_heartbeat(state: OrchestratorState, *, agent_id: str) -> None:
    """Persist runtime heartbeat while long-running agent execution is active."""
    try:
        from core.application.orchestration.agents.common import update_runtime_truth

        update_runtime_truth(
            state,
            current_agent=agent_id,
            selected_workflow=str(state.get("workflow_id") or ""),
        )
    except Exception:
        # Never fail execution on observability heartbeat errors.
        pass

    if str(state.get("task_source") or "").strip().lower() != "invoke_workflow":
        return

    parent_job_id = str(
        state.get("invoke_parent_job_id") or state.get("parent_job_id") or ""
    ).strip()
    if not parent_job_id:
        return

    parent_agent = str(state.get("invoke_parent_step_agent_id") or "").strip()
    child_job_id = str(state.get("job_id") or "").strip()
    child_workflow_id = str(state.get("workflow_id") or "").strip() or "child"
    bl = _get_observability_logger()
    if bl is None or not hasattr(bl, "update_job_runtime"):
        return
    with contextlib.suppress(Exception):
        bl.update_job_runtime(
            parent_job_id,
            current_agent=parent_agent or None,
            heartbeat_now=True,
            last_response_excerpt=(
                f"Inline child workflow '{child_workflow_id}' is active"
                f"{f' (job {child_job_id})' if child_job_id else ''}"
            ),
        )
