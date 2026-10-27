"""Resolve monitoring scope from durable installation-owned configuration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from uuid import UUID

from core.application.lifecycle.contracts import DeploymentConfig
from core.application.monitoring.contracts import RuntimeInstallationContext


def resolve_runtime_installation_context(
    *,
    runtime_values: Mapping[str, str],
    installed_identity: str | None,
    journal_identity: str | None,
) -> RuntimeInstallationContext:
    """Preserve journal-backed Compose identity or require a persisted platform UUID."""
    explicit = runtime_values.get("FS_INSTALLATION_ID", "").strip()
    compose = runtime_values.get("FS_COMPOSE_PROJECT_NAME", "").strip()
    if installed_identity:
        if journal_identity and journal_identity != installed_identity:
            raise ValueError("installed and journal identities conflict")
        if compose and compose != installed_identity:
            raise ValueError("runtime and installed Compose identities conflict")
        if explicit and explicit != installed_identity:
            raise ValueError("platform and installed identities conflict")
        identity = installed_identity
    else:
        if not explicit:
            raise ValueError("persistent installation identity is missing")
        try:
            identity = str(UUID(explicit))
        except ValueError as exc:
            raise ValueError("platform installation identity must be a UUID") from exc
        if journal_identity and journal_identity != identity:
            raise ValueError("platform and journal identities conflict")
    raw = runtime_values.get("FS_MONITORING_EXPECTATIONS", "").strip() or "{}"
    if len(raw.encode()) > 16384:
        raise ValueError("monitoring expectations exceed size limit")
    expectations = json.loads(raw)
    if not isinstance(expectations, dict):
        raise ValueError("monitoring expectations must be an object")
    dependencies_raw = runtime_values.get("FS_MONITORING_DEPENDENCIES", "").strip() or "[]"
    if len(dependencies_raw.encode()) > 16384:
        raise ValueError("monitoring dependency inventory exceeds size limit")
    dependencies = json.loads(dependencies_raw)
    if not isinstance(dependencies, list):
        raise ValueError("monitoring dependencies must be a list")
    return RuntimeInstallationContext.model_validate(
        {
            "installation_id": identity,
            "deployment_profile": runtime_values.get("FS_DEPLOYMENT_PROFILE", ""),
            "expectations": tuple(sorted(expectations.items())),
            "dependencies": tuple(dependencies),
        }
    )


def deployment_monitoring_expectations(config: DeploymentConfig) -> str:
    """Project this deployment owner's known roles, not a monitoring-owned catalog."""
    expectations = {
        "app": None,
        "installer": 1,
        "control": 1,
        "mcp": 1,
        "worker": config.worker_processes
        if config.deployment_profile == "compact"
        else config.worker_replicas,
    }
    if config.deployment_profile == "compact":
        expectations["router"] = 1
    return json.dumps(expectations, sort_keys=True, separators=(",", ":"))


def deployment_monitoring_dependencies(config: DeploymentConfig) -> str:
    """The dependencies something actually checks; monitoring does not discover them.

    Compact's supervisor probes PostgreSQL and RabbitMQ and its router role
    stands for ingress; in Scalable the status receiver proves both. Nothing
    probes ingress or bundled object storage on its own, and an expected
    dependency nobody checks only ever reads "not observed".
    """
    del config
    return json.dumps(["postgres", "rabbitmq"], separators=(",", ":"))


def resolve_projected_runtime_installation_context(
    runtime_values: Mapping[str, str],
    *,
    journal_identity: str | None = None,
) -> RuntimeInstallationContext:
    """Normalize the deployment owner's projection, never a discovered container ID.

    Runtime roles receive the validated installed Compose identity through their
    deployment environment. Installation owners with access to the private journal
    supply its identity too; managed platforms instead supply their persisted UUID.
    """
    return resolve_runtime_installation_context(
        runtime_values=runtime_values,
        installed_identity=runtime_values.get("FS_COMPOSE_PROJECT_NAME", "").strip() or None,
        journal_identity=journal_identity,
    )
