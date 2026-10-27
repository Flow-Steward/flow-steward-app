"""Thread and subprocess helpers for graph resume execution."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any


def set_job_status_by_thread_id(
    thread_id: str,
    status: str,
    *,
    get_logger_only,
    job_thread_store,
    logger,
) -> None:
    bl = get_logger_only()
    if not bl or not thread_id:
        return
    try:
        job_thread_store.update_job_status_by_thread_id(
            bl.conn,
            thread_id=thread_id,
            status=status,
            commit=True,
        )
    except Exception as exc:
        logger.debug(
            "set_job_status_by_thread_id failed for thread=%s status=%s: %s",
            thread_id,
            status,
            exc,
        )


def unregister_resume_thread(
    thread: threading.Thread,
    *,
    resume_threads: set[threading.Thread],
    resume_threads_lock,
) -> None:
    with resume_threads_lock:
        resume_threads.discard(thread)


def snapshot_resume_threads(
    *,
    resume_threads: set[threading.Thread],
    resume_threads_lock,
) -> list[threading.Thread]:
    with resume_threads_lock:
        alive = [thread for thread in resume_threads if thread.is_alive()]
        stale = [thread for thread in resume_threads if not thread.is_alive()]
        for thread in stale:
            resume_threads.discard(thread)
        return alive


def join_resume_threads(
    *,
    resume_threads: set[threading.Thread],
    resume_threads_lock,
    timeout_seconds: float,
    monotonic=time.monotonic,
) -> int:
    deadline = monotonic() + max(0.0, float(timeout_seconds))
    while True:
        threads = snapshot_resume_threads(
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
        )
        if not threads:
            return 0
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        join_timeout = max(0.05, min(1.0, remaining))
        for thread in threads:
            thread.join(timeout=join_timeout)
    lingering = snapshot_resume_threads(
        resume_threads=resume_threads,
        resume_threads_lock=resume_threads_lock,
    )
    return len(lingering)


def _project_resume_threads(
    project_id: str,
    *,
    resume_threads: set[threading.Thread],
    resume_threads_lock,
) -> list[threading.Thread]:
    normalized_project_id = str(project_id or "").strip()
    with resume_threads_lock:
        stale = [thread for thread in resume_threads if not thread.is_alive()]
        for thread in stale:
            resume_threads.discard(thread)
        return [
            thread
            for thread in resume_threads
            if thread.is_alive()
            and str(getattr(thread, "flow_steward_project_id", "") or "").strip()
            == normalized_project_id
        ]


def fence_project_resume_threads(
    project_id: str,
    *,
    resume_threads: set[threading.Thread],
    resume_threads_lock,
    fenced_project_ids: set[str],
    timeout_seconds: float,
    monotonic=time.monotonic,
) -> int:
    """Fence new project resumes and wait for already-started runners."""
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return 0
    with resume_threads_lock:
        fenced_project_ids.add(normalized_project_id)
    deadline = monotonic() + max(0.0, float(timeout_seconds))
    while True:
        threads = _project_resume_threads(
            normalized_project_id,
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
        )
        if not threads:
            return 0
        remaining = deadline - monotonic()
        if remaining <= 0:
            return len(threads)
        join_timeout = max(0.05, min(1.0, remaining))
        for thread in threads:
            thread.join(timeout=join_timeout)


def unfence_project_resume_threads(
    project_id: str,
    *,
    resume_threads_lock,
    fenced_project_ids: set[str],
) -> None:
    normalized_project_id = str(project_id or "").strip()
    with resume_threads_lock:
        fenced_project_ids.discard(normalized_project_id)


def resume_thread_entry(
    thread_id: str,
    resume_value: dict[str, Any],
    approved: bool,
    action: str | None,
    comment: str,
    interrupt_payload: dict[str, Any],
    *,
    actor_username: str = "operator",
    approval_id: str | None = None,
    run_graph_resume,
    unregister_resume_thread,
    logger,
    current_thread=threading.current_thread,
) -> None:
    try:
        if approval_id:
            run_graph_resume(
                thread_id,
                resume_value,
                approved,
                action,
                comment,
                interrupt_payload,
                actor_username=actor_username,
                approval_id=approval_id,
            )
        else:
            run_graph_resume(
                thread_id,
                resume_value,
                approved,
                action,
                comment,
                interrupt_payload,
                actor_username=actor_username,
            )
    except Exception as exc:
        logger.exception(
            "resume_thread_entry: unexpected resume failure for thread %s: %s",
            thread_id,
            exc,
        )
    finally:
        unregister_resume_thread(current_thread())


def _write_resume_payload_file(payload: dict[str, Any], *, payload_dir: str | None = None) -> str:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="fs-graph-resume-",
        suffix=".json",
        dir=payload_dir or None,
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.write("\n")
        return str(handle.name)


def launch_graph_resume_subprocess(
    *,
    thread_id: str,
    resume_value: dict[str, Any],
    approved: bool,
    action: str | None,
    comment: str,
    interrupt_payload: dict[str, Any],
    actor_username: str = "operator",
    approval_id: str | None = None,
    python_executable: str | None = None,
    payload_dir: str | None = None,
    popen_factory=subprocess.Popen,
    logger,
) -> int:
    payload = {
        "thread_id": thread_id,
        "resume_value": resume_value,
        "approved": approved,
        "action": action,
        "comment": comment,
        "interrupt_payload": interrupt_payload,
        "actor_username": actor_username,
    }
    if approval_id:
        payload["approval_id"] = approval_id
    payload_path = _write_resume_payload_file(payload, payload_dir=payload_dir)
    command = [
        python_executable or sys.executable,
        "-m",
        "core.infrastructure.runtime.graph_resume_subprocess",
        payload_path,
    ]
    try:
        process = popen_factory(command)
        return int(process.wait())
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(payload_path)


def resume_subprocess_entry(
    thread_id: str,
    resume_value: dict[str, Any],
    approved: bool,
    action: str | None,
    comment: str,
    interrupt_payload: dict[str, Any],
    *,
    actor_username: str = "operator",
    approval_id: str | None = None,
    launch_subprocess=launch_graph_resume_subprocess,
    unregister_resume_thread,
    logger,
    current_thread=threading.current_thread,
) -> None:
    try:
        kwargs = {
            "thread_id": thread_id,
            "resume_value": resume_value,
            "approved": approved,
            "action": action,
            "comment": comment,
            "interrupt_payload": interrupt_payload,
            "actor_username": actor_username,
            "logger": logger,
        }
        if approval_id:
            kwargs["approval_id"] = approval_id
        return_code = launch_subprocess(**kwargs)
        if return_code:
            logger.error(
                "resume_subprocess_entry: resume subprocess failed for thread %s with exit code %s",
                thread_id,
                return_code,
            )
    except Exception as exc:
        logger.exception(
            "resume_subprocess_entry: unexpected launcher failure for thread %s: %s",
            thread_id,
            exc,
        )
    finally:
        unregister_resume_thread(current_thread())


def start_graph_resume_thread(
    thread_id: str,
    resume_value: dict[str, Any],
    approved: bool,
    action: str | None,
    comment: str,
    interrupt_payload: dict[str, Any],
    *,
    project_id: str | None = None,
    actor_username: str = "operator",
    approval_id: str | None = None,
    run_graph_resume,
    unregister_resume_thread,
    resume_shutdown_in_progress,
    resume_threads: set[threading.Thread],
    resume_threads_lock,
    fenced_project_ids: set[str] | None = None,
    logger,
    thread_factory=threading.Thread,
    execution_mode: str | Callable[[], str] = "thread",
    launch_subprocess=launch_graph_resume_subprocess,
) -> threading.Thread | None:
    if resume_shutdown_in_progress():
        return None
    mode_value = execution_mode() if callable(execution_mode) else execution_mode
    normalized_mode = str(mode_value or "thread").strip().lower()
    if normalized_mode in {"process", "subprocess"}:
        target = resume_subprocess_entry
        kwargs = {
            "thread_id": thread_id,
            "resume_value": resume_value,
            "approved": approved,
            "action": action,
            "comment": comment,
            "interrupt_payload": interrupt_payload,
            "actor_username": actor_username,
            "launch_subprocess": launch_subprocess,
            "unregister_resume_thread": unregister_resume_thread,
            "logger": logger,
        }
        if approval_id:
            kwargs["approval_id"] = approval_id
    else:
        target = resume_thread_entry
        kwargs = {
            "thread_id": thread_id,
            "resume_value": resume_value,
            "approved": approved,
            "action": action,
            "comment": comment,
            "interrupt_payload": interrupt_payload,
            "actor_username": actor_username,
            "run_graph_resume": run_graph_resume,
            "unregister_resume_thread": unregister_resume_thread,
            "logger": logger,
        }
        if approval_id:
            kwargs["approval_id"] = approval_id
    resume_thread = thread_factory(
        target=target,
        kwargs=kwargs,
        daemon=True,
        name=f"graph-resume-{thread_id[:8]}",
    )
    normalized_project_id = str(project_id or "").strip()
    resume_thread.flow_steward_project_id = normalized_project_id
    with resume_threads_lock:
        if resume_shutdown_in_progress():
            return None
        if normalized_project_id and normalized_project_id in (fenced_project_ids or set()):
            return None
        resume_threads.add(resume_thread)
    try:
        resume_thread.start()
    except Exception:
        unregister_resume_thread(resume_thread)
        raise
    return resume_thread


def build_start_graph_resume_thread_handler(
    *,
    run_graph_resume,
    unregister_resume_thread,
    resume_shutdown_in_progress,
    resume_threads: set[threading.Thread],
    resume_threads_lock,
    fenced_project_ids: set[str] | None = None,
    resolve_project_id: Callable[[str], str | None] | None = None,
    logger,
    thread_factory=threading.Thread,
    execution_mode: str | Callable[[], str] = "thread",
    launch_subprocess=launch_graph_resume_subprocess,
):
    def _handler(*args, **kwargs) -> threading.Thread | None:
        if not str(kwargs.get("project_id") or "").strip() and resolve_project_id is not None:
            thread_id = str(kwargs.get("thread_id") or (args[0] if args else "")).strip()
            kwargs["project_id"] = resolve_project_id(thread_id)
        return start_graph_resume_thread(
            *args,
            run_graph_resume=run_graph_resume,
            unregister_resume_thread=unregister_resume_thread,
            resume_shutdown_in_progress=resume_shutdown_in_progress,
            resume_threads=resume_threads,
            resume_threads_lock=resume_threads_lock,
            fenced_project_ids=fenced_project_ids,
            logger=logger,
            thread_factory=thread_factory,
            execution_mode=execution_mode,
            launch_subprocess=launch_subprocess,
            **kwargs,
        )

    return _handler


__all__ = [
    "build_start_graph_resume_thread_handler",
    "fence_project_resume_threads",
    "join_resume_threads",
    "launch_graph_resume_subprocess",
    "resume_subprocess_entry",
    "resume_thread_entry",
    "set_job_status_by_thread_id",
    "snapshot_resume_threads",
    "start_graph_resume_thread",
    "unfence_project_resume_threads",
    "unregister_resume_thread",
]
