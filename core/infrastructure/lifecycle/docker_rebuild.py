"""`stack rebuild`: build what this installation builds, then recreate the services.

Owner 2026-11-21 (D4): a development installation upgraded like production runs the
official Core images by registry digest. Building those services from the checkout
would replace published images with local ones -- and Compose cannot tag a build with
a digest reference at all. They are recreated, never built; the development extras
(fs-test-runner, fs-audit) and every service of a source installation build as before.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.stack_service import StackGatewayError

from .docker_gateway_contracts import _DOCKER_TIMEOUT_SECONDS


def locally_built_services(
    target: ComposeTarget,
    services: Sequence[str],
    *,
    run_compose: Callable[..., Any],
) -> tuple[str, ...]:
    """The services whose rendered image is not pinned to a registry digest."""
    if target.stack_environment != "development":
        # Production runs no build stanzas; its command stays exactly as it was.
        return tuple(services)
    # Compose JSON includes interpolated secrets: read privately, never streamed.
    outcome = run_compose(
        target,
        ("config", "--format", "json"),
        action="rebuild_images",
        timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
        separate_stderr=True,
    )
    try:
        rendered = json.loads(str(outcome.stdout or ""))["services"]
        if not isinstance(rendered, dict):
            raise TypeError("services is not an object")
    except (ValueError, KeyError, TypeError) as exc:
        raise StackGatewayError(
            action="rebuild_build",
            detail="Docker Compose returned no usable service configuration.",
        ) from exc

    def pinned(service: str) -> bool:
        definition = rendered.get(service)
        image = definition.get("image") if isinstance(definition, dict) else None
        return "@sha256:" in str(image or "")

    return tuple(service for service in services if not pinned(service))


def rebuild(
    target: ComposeTarget,
    services: tuple[str, ...],
    *,
    run_compose: Callable[..., Any],
    timeout_seconds: int,
    output_sink: Callable[[str], None],
    heartbeat: Callable[[str], None],
) -> None:
    buildable = locally_built_services(target, services, run_compose=run_compose)
    if buildable:
        # `compose build` with no service builds every service, so an empty
        # selection must not reach it.
        run_compose(
            target,
            ("build", *buildable),
            action="rebuild_build",
            timeout_seconds=timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        )
    run_compose(
        target,
        (
            "up",
            "--wait",
            "--wait-timeout",
            str(timeout_seconds),
            "--force-recreate",
            "--no-deps",
            *services,
        ),
        action="rebuild_recreate",
        timeout_seconds=timeout_seconds,
        output_sink=output_sink,
        heartbeat=heartbeat,
    )


def build_development_target(
    target: ComposeTarget,
    services: tuple[str, ...],
    *,
    run: Callable[..., Any],
    compose_command: Callable[[tuple[str, ...]], tuple[str, ...]],
    environment: Mapping[str, str],
    input_text: str | None,
    timeout_seconds: int,
) -> None:
    """Pull what is missing and build a development target that is not complete yet.

    An official development target -- an official installation switched to
    development, owner 2026-11-21 -- runs its Core services on release digests, so
    only the services it builds itself are built (D4). A source target builds every
    selected service, exactly as before.
    """

    def run_compose(
        _target: ComposeTarget,
        arguments: Sequence[str],
        *,
        action: str,
        timeout_seconds: float,
        separate_stderr: bool = False,
    ) -> Any:
        return run(
            compose_command(tuple(arguments)),
            action=action,
            timeout_seconds=timeout_seconds,
            environment=environment,
            input_text=input_text,
            separate_stderr=separate_stderr,
        )

    run_compose(
        target,
        ("pull", "--ignore-buildable", "--policy", "missing", *services),
        action="target_image_pull",
        timeout_seconds=timeout_seconds,
    )
    buildable = (
        locally_built_services(target, services, run_compose=run_compose)
        if environment.get("FS_INSTALL_MODE") == "official"
        else services
    )
    if buildable:
        run_compose(
            target,
            ("build", *buildable),
            action="target_image_build",
            timeout_seconds=timeout_seconds,
        )
