"""CLI approval command service composition."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from core.application.management.command_service_runtime import create_approval_command_service
from core.infrastructure.runtime.control import state_reader
from core.infrastructure.runtime.workflow_resume_wiring import workflow_resume_command_ports


def create_runtime_approval_command_service(*, get_logger: Callable[[], Any]) -> Any:
    """Create the canonical approval command service for the CLI runtime.

    A decision is stored with its workflow resume intent and published to the
    worker queue; the CLI process never resumes a graph itself.
    """

    def resume_shutdown_in_progress() -> bool:
        is_finalizing = getattr(sys, "is_finalizing", None)
        return bool(is_finalizing and is_finalizing())

    return create_approval_command_service(
        resume_shutdown_in_progress=resume_shutdown_in_progress,
        get_checkpointer=state_reader.get_checkpointer,
        get_logger=get_logger,
        get_thread_state_snapshot=lambda thread_id: state_reader.get_thread_state_snapshot(
            thread_id
        ),
        **workflow_resume_command_ports(),
    )
