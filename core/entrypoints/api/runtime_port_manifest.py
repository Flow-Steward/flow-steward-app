"""Authoritative production runtime-port contracts for API composition."""

from __future__ import annotations

from dataclasses import dataclass

_TOKEN_EXTENSION_CALLBACKS_PY = "extension_callbacks.py"


@dataclass(frozen=True, slots=True)
class RuntimePortContract:
    consumer: str
    dependency: str
    required_methods: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeGuardClassification:
    source_file: str
    receiver: str
    method: str
    dependency: str | None
    mandatory: bool
    rationale: str = ""


RUNTIME_GUARD_CLASSIFICATIONS: tuple[RuntimeGuardClassification, ...] = (
    RuntimeGuardClassification(
        _TOKEN_EXTENSION_CALLBACKS_PY,
        "project_repository",
        "get_project",
        "project_store",
        True,
    ),
    RuntimeGuardClassification(
        _TOKEN_EXTENSION_CALLBACKS_PY,
        "bl",
        "list_extension_callback_registrations",
        "identity_store",
        True,
    ),
    RuntimeGuardClassification(
        _TOKEN_EXTENSION_CALLBACKS_PY,
        "bl",
        "get_extension_callback_registration_by_id",
        "identity_store",
        True,
    ),
    RuntimeGuardClassification(
        _TOKEN_EXTENSION_CALLBACKS_PY,
        "bl",
        "mark_extension_callback_registration_seen",
        None,
        False,
        "Delivery status enrichment is best-effort after the callback dispatch result is durable.",
    ),
)


RUNTIME_SERVICE_PORT_CONTRACTS: tuple[RuntimePortContract, ...] = (
    RuntimePortContract(
        "api.project_routes.active_and_trashed_project_scope",
        "project_store",
        ("get_project", "get_project_workflows", "list_project_connections"),
    ),
    RuntimePortContract(
        "api.extension_callbacks.project_authorization",
        "project_store",
        ("get_project",),
    ),
    RuntimePortContract(
        "api.trash_lifecycle.dataset_projection",
        "project_dataset_catalog",
        ("list_project_dataset_catalog", "get_dataset"),
    ),
    RuntimePortContract(
        "api.job_routes_and_command_dispatch",
        "job_store",
        ("get_job", "get_active_jobs", "update_job", "log_command_execution"),
    ),
    RuntimePortContract(
        "api.job_trace_projection",
        "observability_store",
        ("build_job_trace", "get_logs", "get_events", "get_agent_messages"),
    ),
    RuntimePortContract(
        "api.extension_and_connector_routes",
        "integration_store",
        ("get_extension", "list_extensions", "list_project_connections", "get_project"),
    ),
    RuntimePortContract(
        "api.extension_oauth.project_and_install_scope",
        "integration_store",
        ("get_project", "get_extension_install_for_operator"),
    ),
    RuntimePortContract(
        "api.extension_callbacks.registration_store",
        "identity_store",
        (
            "create_extension_callback_registration",
            "list_extension_callback_registrations",
            "get_extension_callback_registration_by_id",
            "get_extension_callback_registration_by_token_hash",
            "update_extension_callback_registration_token",
            "claim_extension_callback_delivery",
            "lock_extension_callback_registration_scope",
            "save_project_secret",
            "get_project_secret",
        ),
    ),
    RuntimePortContract(
        "api.provider_webhook_authorization",
        "provider_webhook_store",
        ("get_project", "get_provider", "get_project_connection"),
    ),
    RuntimePortContract(
        "api.workflow_entrypoint_creation_execution_and_audit",
        "project_store",
        ("get_project", "get_project_run_config", "save_project_secret", "log_action"),
    ),
    RuntimePortContract(
        "api.workflow_and_job_reactions",
        "event_store",
        ("list_workflow_reactions", "save_workflow_reactions", "list_job_reaction_events"),
    ),
    RuntimePortContract(
        "api.readiness_identity_surface",
        "identity_store",
        ("list_extensions",),
    ),
    RuntimePortContract(
        "api.extension_lifecycle.oauth_uninstall_audit",
        "identity_store",
        ("get_extension_install_for_operator", "log_command_execution"),
    ),
)


__all__ = [
    "RUNTIME_GUARD_CLASSIFICATIONS",
    "RUNTIME_SERVICE_PORT_CONTRACTS",
    "RuntimeGuardClassification",
    "RuntimePortContract",
]
