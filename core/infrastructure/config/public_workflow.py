"""Operator-owned public workflow runtime policy."""

from __future__ import annotations

from dataclasses import dataclass

from core.infrastructure.config.env_registry import registry_default_value
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def _bounded_int(name: str, *, fallback: int, minimum: int, maximum: int) -> int:
    try:
        default = int(registry_default_value(name))
    except (KeyError, TypeError, ValueError):
        default = fallback
    try:
        value = int(_runtime_env.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class PublicWorkflowRuntimeSettings:
    sse_connections_per_principal: int
    sse_connections_per_account: int
    idempotency_result_retention_days: int


def public_workflow_runtime_settings() -> PublicWorkflowRuntimeSettings:
    return PublicWorkflowRuntimeSettings(
        sse_connections_per_principal=_bounded_int(
            "FS_PUBLIC_SSE_MAX_CONNECTIONS_PER_PRINCIPAL",
            fallback=10,
            minimum=1,
            maximum=1000,
        ),
        sse_connections_per_account=_bounded_int(
            "FS_PUBLIC_SSE_MAX_CONNECTIONS_PER_ACCOUNT",
            fallback=100,
            minimum=1,
            maximum=10000,
        ),
        idempotency_result_retention_days=_bounded_int(
            "FS_RETENTION_JOBS_DAYS",
            fallback=30,
            minimum=1,
            maximum=3650,
        ),
    )
