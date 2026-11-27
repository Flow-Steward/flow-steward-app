"""Standalone worker process composition entrypoint."""

from __future__ import annotations

# Must stay the first import: fixes the OTEL_* environment before libraries read it.
import core.infrastructure.observability.otel_bootstrap  # noqa: F401  # isort: split
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
    build_background_poll_ports,
    build_provider_event_dispatch_sweep,
    build_worker_inbound_poll_response_processor,
)
from core.infrastructure.config.compact_runtime import validate_compact_only_settings
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.worker.worker import main as run_worker

#: Set by :func:`main` before modules register; ``None`` (nothing entitled) until then.
_WORKER_ENTITLEMENT_SERVICE = None


def _register_worker_platform_modules(config) -> list[dict[str, str]]:
    """Load the worker hooks of platform modules with the worker's own ports (ADR 0082).

    A module receives the worker's entitlement decider, which only the module that
    declares ``entitlement_source`` registers into, a ``db`` factory for the worker's
    connection store and the installation id. The decider is sealed afterwards.

    Returns the failure record of each module that failed to load (ADR 0082 §8); the
    worker carries them in every heartbeat.
    """
    from core.application.lifecycle.runtime_installation_context import (
        projected_installation_id,
    )
    from core.application.settings.platform_contracts import CoreEntitlementService
    from core.infrastructure.platform_modules.loader import register_platform_modules_for_role
    from core.infrastructure.runtime.entrypoint_db_adapters import (
        module_db_factory,
        resolve_platform_dsn,
    )
    from core.infrastructure.security.secret_backends import SecretBackendPort

    global _WORKER_ENTITLEMENT_SERVICE
    _WORKER_ENTITLEMENT_SERVICE = CoreEntitlementService()
    # Where a module registers its secret backends; sealed with the decider.
    secret_backends = SecretBackendPort()
    # A module that fails to load is skipped (ADR 0082 §5); its failure goes to the
    # log, to this role's status for the release workflow and to every heartbeat.
    module_failures: list[dict[str, str]] = []
    loaded_modules = register_platform_modules_for_role(
        "worker",
        config=config,
        services={
            "entitlement_service": _WORKER_ENTITLEMENT_SERVICE,
            "secret_backends": secret_backends,
            "db": module_db_factory(resolve_platform_dsn(str(config.get("fs_db_dsn") or ""))),
        },
        failures=module_failures,
        installation_id=projected_installation_id(dict(_runtime_env)),
    )
    _WORKER_ENTITLEMENT_SERVICE.seal()
    secret_backends.seal()
    if loaded_modules or module_failures:
        from core.infrastructure.platform_modules.role_status import (
            module_status_records,
            write_role_module_status,
        )

        write_role_module_status(
            "worker", loaded=module_status_records(loaded_modules), failures=module_failures
        )
    return module_failures


def main() -> None:
    from core.infrastructure.config.session_identity import set_session_role

    set_session_role("worker")
    validate_compact_only_settings(_runtime_env)
    from core.infrastructure.config.runtime_config import CONFIG

    module_failures = _register_worker_platform_modules(CONFIG)
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
        sweep_provider_event_dispatches=build_provider_event_dispatch_sweep(),
        background_poll_ports=build_background_poll_ports(),
        maintenance_active=public_submission_pause_active,
        curate_terminal_job=curate_completed_job_to_memory,
        job_terminal_statuses=JOB_TERMINAL_STATUSES,
        start_runtime_reporting=lambda worker_id, inherited_sink: start_optional_worker_reporting(
            runtime_values=dict(_runtime_env),
            worker_id=worker_id,
            inherited_sink=inherited_sink,
            platform_module_failures=module_failures,
        ),
    )


if __name__ == "__main__":
    main()
