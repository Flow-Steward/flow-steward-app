"""Provider-neutral event ingress composition for the worker process."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_service_runtime import create_event_command_service
from core.application.orchestration.agents.common import sanitize_external_input
from core.application.workflows import domain_event_runtime, reaction_runtime
from core.application.workflows.reaction_service_runtime import (
    build_worker_inbound_reaction_evaluator,
    require_worker_inbound_reaction_contract,
    resume_job_via_approval_command_service,
)
from core.application.workflows.serve_workflow_helpers import workflow_exists
from core.infrastructure.config.runtime_config import CONFIG
from core.infrastructure.runtime.worker.event_ingress_wiring import WorkerEventIngressPorts
from core.infrastructure.runtime.worker.worker_common import _to_json_obj, logger


def worker_event_ingress_ports() -> WorkerEventIngressPorts:
    """Compose application services and queue adapters at the worker entrypoint."""
    from core.application.extensions.provider_attachments import fetch_provider_attachments
    from core.application.notifications.dispatch import dispatch_project_notification_event
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_task

    return WorkerEventIngressPorts(
        dispatch_project_notification_event=dispatch_project_notification_event,
        fetch_provider_attachments=fetch_provider_attachments,
        enqueue_task=enqueue_task,
    )


def _event_command_service_factory(
    *,
    bl: Any,
    approval_command_service_factory: Callable[[], Any],
) -> Callable[[], Any]:
    ports = worker_event_ingress_ports()

    evaluate_reactions = build_worker_inbound_reaction_evaluator(
        to_json_obj=_to_json_obj,
        resume_job_from_reaction=lambda *args, **kwargs: resume_job_via_approval_command_service(
            *args,
            **kwargs,
            get_job_store=lambda: bl,
            approval_command_service_factory=approval_command_service_factory,
        ),
        logger=logger,
    )

    def factory():
        return create_event_command_service(
            config=CONFIG,
            get_logger=lambda: bl,
            get_job_store=lambda: bl,
            get_project_run_config=lambda project_id: (
                bl.get_project_run_config(project_id)
                if hasattr(bl, "get_project_run_config")
                else {}
            ),
            resolve_domain_event_by_ref=lambda store, event_ref, **kwargs: (
                domain_event_runtime.resolve_domain_event_by_ref(
                    store,
                    event_ref,
                    default_account_id=DEFAULT_ACCOUNT_ID,
                    **kwargs,
                )
            ),
            to_json_obj=_to_json_obj,
            serialize_domain_event_datetime=domain_event_runtime.serialize_domain_event_datetime,
            sanitize_external_input=sanitize_external_input,
            enqueue_task=ports.enqueue_task,
            workflow_exists=workflow_exists,
            evaluate_workflow_reactions=evaluate_reactions,
            reaction_action_result_executed=reaction_runtime.reaction_action_result_executed,
            dispatch_project_notification_event=ports.dispatch_project_notification_event,
            fetch_provider_attachments=ports.fetch_provider_attachments,
        )

    return factory


def build_worker_inbound_poll_response_processor(
    *,
    process_extension_response_envelope: Callable[..., Any],
    approval_command_service_builder: Callable[..., Any],
) -> Callable[..., Any]:
    """Inject worker event and approval services into the shared envelope handler."""

    def process(*args, **kwargs):
        bl = kwargs.get("bl")
        invocation = args[0] if args else kwargs.get("invocation")
        response = invocation.get("response") if isinstance(invocation, dict) else None
        if isinstance(response, dict):
            require_worker_inbound_reaction_contract(
                response,
                bl=bl,
                to_json_obj=_to_json_obj,
            )

        def approval_factory():
            return approval_command_service_builder(get_logger=lambda: bl)

        kwargs["event_command_service_factory"] = _event_command_service_factory(
            bl=bl,
            approval_command_service_factory=approval_factory,
        )
        return process_extension_response_envelope(*args, **kwargs)

    return process


__all__ = ["build_worker_inbound_poll_response_processor"]
