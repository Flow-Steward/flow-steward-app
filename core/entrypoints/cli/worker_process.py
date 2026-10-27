"""Standalone worker process composition entrypoint."""

from __future__ import annotations

from core.application.jobs.submission_admission import public_submission_pause_active
from core.application.management.command_services import (  # type: ignore[attr-defined]
    _process_extension_response_envelope,
)
from core.application.memory.runtime import curate_completed_job_to_memory
from core.application.monitoring.runtime_composition import start_optional_worker_reporting
from core.application.observability_vocabulary import JOB_TERMINAL_STATUSES
from core.application.orchestration.tools.mcp_tools import verify_worker_mcp_grounding
from core.application.tooling.db.bootstrap_seed import ensure_canonical_guardrails_seeded
from core.entrypoints.cli.approval_command_runtime import create_runtime_approval_command_service
from core.entrypoints.cli.worker_event_ingress import (
    build_worker_inbound_poll_response_processor,
)
from core.infrastructure.config.compact_runtime import validate_compact_only_settings
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.worker.worker import main as run_worker


def main() -> None:
    validate_compact_only_settings(_runtime_env)
    if str(_runtime_env.get("FS_MCP_GROUNDING_REQUIRED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        verify_worker_mcp_grounding(
            retries=30,
            delay_seconds=1.0,
            # MCP and worker start together in both deployment profiles. The
            # probe verifies retained project-list reachability and authority; MCP
            # can temporarily map an unavailable app introspection endpoint to
            # HTTP 401. The bounded retry still fails closed if the credential
            # remains rejected after the startup window.
            retry_rejected=True,
        )
    process_extension_response = build_worker_inbound_poll_response_processor(
        process_extension_response_envelope=_process_extension_response_envelope,
        approval_command_service_builder=create_runtime_approval_command_service,
    )
    run_worker(
        ensure_guardrails_seeded=ensure_canonical_guardrails_seeded,
        process_extension_response_envelope=process_extension_response,
        maintenance_active=public_submission_pause_active,
        curate_terminal_job=curate_completed_job_to_memory,
        job_terminal_statuses=JOB_TERMINAL_STATUSES,
        start_runtime_reporting=lambda worker_id, inherited_sink: start_optional_worker_reporting(
            runtime_values=dict(_runtime_env), worker_id=worker_id, inherited_sink=inherited_sink
        ),
    )


if __name__ == "__main__":
    main()
