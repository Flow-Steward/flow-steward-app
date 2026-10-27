"""Fail-closed health gate shared by local extension install and enable paths."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.management.errors import ServiceValidationError

HealthRunner = Callable[[dict[str, Any]], dict[str, Any]]


def has_declared_health(extension: dict[str, Any]) -> bool:
    health = extension.get("healthcheck") or extension.get("health") or {}
    return bool(isinstance(health, dict) and health.get("command"))


def require_extension_health_ready(
    extension: dict[str, Any],
    *,
    run_health: HealthRunner | None = None,
) -> None:
    if not has_declared_health(extension):
        return
    if run_health is None:
        from core.infrastructure.extension_runtime.extension_runtime import (
            run_extension_health,
        )

        run_health = run_extension_health
    result = dict(run_health(extension) or {})
    if str(result.get("status") or "").strip().lower() == "healthy":
        return
    detail = str(result.get("detail") or "extension health did not become ready").strip()
    raise ServiceValidationError(
        f"Extension health check failed: {detail[:1000]}",
        payload={
            "reason_code": "extension_health_failed",
            "extension_id": str(
                extension.get("extension_key") or extension.get("extension_id") or ""
            ).strip(),
        },
    )


__all__ = ["has_declared_health", "require_extension_health_ready"]
