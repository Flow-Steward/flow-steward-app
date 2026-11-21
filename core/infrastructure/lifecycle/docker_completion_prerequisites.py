"""One-shot services a reconciled service waits on, run before `up --no-deps`.

E2E 2026-11-21: a profile transition reconciles with `--no-deps`, so Compose never ran
the one-shot `traefik-files`. After Scalable to Compact its volume was new and empty,
and Compact's Traefik restarted in a loop (exit 2, render-public-router.sh missing)
until a later `up` ran the copy: the public route was down meanwhile, and a bare
`config apply` would have left it down. What a service waits on with
`service_completed_successfully` is a preparation step, not a dependency to skip.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.stack_service import StackGatewayError

from .docker_gateway_contracts import _DOCKER_TIMEOUT_SECONDS

_COMPLETED = "service_completed_successfully"


def completion_prerequisites(
    target: ComposeTarget,
    services: Sequence[str],
    *,
    run_compose: Callable[..., Any],
) -> tuple[str, ...]:
    """The one-shot services `services` wait on to complete, in first-needed order."""
    # Compose JSON includes interpolated secrets: read privately, never streamed.
    outcome = run_compose(
        target,
        ("config", "--format", "json"),
        action="configuration_prerequisites",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
        separate_stderr=True,
    )
    try:
        rendered = json.loads(str(outcome.stdout or ""))["services"]
        if not isinstance(rendered, dict):
            raise TypeError("services is not an object")
    except (ValueError, KeyError, TypeError) as exc:
        raise StackGatewayError(
            action="configuration_prerequisites",
            detail="Docker Compose returned no usable service configuration.",
        ) from exc
    found: list[str] = []
    for service in services:
        definition = rendered.get(service)
        depends_on = definition.get("depends_on") if isinstance(definition, dict) else None
        if not isinstance(depends_on, dict):
            continue
        found.extend(
            name
            for name, condition in depends_on.items()
            if isinstance(condition, dict) and condition.get("condition") == _COMPLETED
        )
    return tuple(name for name in dict.fromkeys(found) if name not in services)


def run_completion_prerequisites(
    target: ComposeTarget,
    services: Sequence[str],
    *,
    run_compose: Callable[..., Any],
    timeout_seconds: float,
) -> None:
    """Run each one-shot `services` wait on, to completion, before they start."""
    # Production reconciles the immutable set it prepared: never build, never pull.
    production = ("--pull", "never") if target.stack_environment == "production" else ()
    for name in completion_prerequisites(target, services, run_compose=run_compose):
        run_compose(
            target,
            ("run", "--rm", "--no-deps", *production, "-T", name),
            action="configuration_prerequisite",
            timeout_seconds=timeout_seconds,
            separate_stderr=True,
        )
