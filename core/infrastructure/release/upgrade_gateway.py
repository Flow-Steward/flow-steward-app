"""Run database tooling from the prepared target's product image."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_service import StackGatewayError
from core.infrastructure.lifecycle.docker_gateway_contracts import ComposeCommandRunner

_SCALABLE_WRITERS = ("fs-app", "fs-worker", "control-plane", "fs-mcp", "fs-extension-installer")
#: Owner 2026-11-21 (D3): what runs code from a development checkout -- the Core
#: services bind-mount it and the frontend dev server serves it.
_CHECKOUT_WRITERS = (*_SCALABLE_WRITERS, "fs-frontend")


def _stop_running(
    target: ComposeTarget,
    services: tuple[str, ...],
    *,
    run_compose: ComposeCommandRunner,
    environment: Mapping[str, str],
    action: str,
) -> None:
    running = run_compose(
        target,
        ("ps", "--status", "running", "--services"),
        action=action,
        environment=environment,
        timeout_seconds=30,
    )
    present = set(str(running.stdout or "").splitlines())
    stopping = tuple(service for service in services if service in present)
    if stopping:
        run_compose(
            target,
            ("stop", *stopping),
            action=action,
            environment=environment,
            timeout_seconds=120,
        )


def stop_checkout_writers(
    target: ComposeTarget,
    *,
    run_compose: ComposeCommandRunner,
    environment: Mapping[str, str],
) -> None:
    """Stop what runs code from a development checkout before the handoff replaces it.

    Owner 2026-11-21 (D3): a development installation upgrades like production, and
    the official handoff checks the release out in place. Services that reload code
    from that checkout would otherwise run the next release's code against a database
    that is not migrated yet. The installer starts them again with the new images.
    """
    _stop_running(
        target,
        _CHECKOUT_WRITERS,
        run_compose=run_compose,
        environment=environment,
        action="release_quiesce",
    )


def upgrade_target_database(
    target: ComposeTarget,
    *,
    skip_db_backup: bool,
    timeout_seconds: int,
    run_compose: ComposeCommandRunner,
    environment: Mapping[str, str],
) -> None:
    """Stop only runtime writers; data services and the retained CLI keep running."""
    compact = target.deployment_profile == "compact"
    _stop_running(
        target,
        ("compact-runtime",) if compact else _SCALABLE_WRITERS,
        run_compose=run_compose,
        environment=environment,
        action="database_upgrade",
    )
    module = (
        "core.entrypoints.cli.compact_container_cli"
        if compact
        else "core.entrypoints.cli.click_app"
    )
    arguments = (
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
        "compact-runtime" if compact else "fs-app",
        "-m",
        module,
        "database",
        "upgrade",
        "--maintenance-confirmed",
        "--confirm",
        "--json",
        *(("--skip-db-backup",) if skip_db_backup else ()),
    )
    outcome = run_compose(
        target,
        arguments,
        action="database_upgrade",
        environment=environment,
        timeout_seconds=timeout_seconds,
        separate_stderr=True,
    )
    try:
        payload = json.loads(outcome.stdout)
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise StackGatewayError(
            action="database_upgrade",
            detail="The target database upgrade returned an invalid or unsuccessful result.",
        )


#: The in-image platform-module command (ADR 0082 §5) and the bound on its answer.
PLATFORM_MODULE_COMMAND = "core.entrypoints.cli.ops.platform_modules"
_PLATFORM_MODULE_OUTPUT_LIMIT = 1024 * 1024


def run_platform_module_command(
    target: ComposeTarget,
    arguments: tuple[str, ...],
    *,
    timeout_seconds: int,
    run_compose: ComposeCommandRunner,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Run one platform-module command in a disposable container of the Compact image.

    The same invocation as the Compact database upgrade above: ``compose run --rm
    --no-deps -T --entrypoint python compact-runtime`` -- the installed image, the
    service's own data volume at ``/data``, the image's default user (root) and the
    installed environment -- with the module command instead of the migrator. It
    neither stops nor starts the runtime container; callers do that first when the
    command writes. stdout is one JSON document; a non-zero exit raises
    ``StackGatewayError`` with the command's own reported error.
    """
    if target.deployment_profile != "compact":
        raise StackGatewayError(
            action="platform_modules",
            detail="Platform modules are delegated to the runtime image only on Compact.",
        )
    outcome = run_compose(
        target,
        (
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "compact-runtime",
            "-m",
            PLATFORM_MODULE_COMMAND,
            *arguments,
        ),
        action="platform_modules",
        environment=environment,
        timeout_seconds=timeout_seconds,
        separate_stderr=True,
    )
    raw = str(outcome.stdout or "")
    try:
        if len(raw.encode("utf-8")) > _PLATFORM_MODULE_OUTPUT_LIMIT:
            raise ValueError("oversized platform-module result")
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StackGatewayError(
            action="platform_modules",
            detail="The platform-module command returned an invalid result.",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or not isinstance(payload.get("result"), dict)
    ):
        raise StackGatewayError(
            action="platform_modules",
            detail="The platform-module command returned an unsuccessful result.",
        )
    return dict(payload["result"])


#: Services that may run the in-image load-status command (ADR 0082 §5).
_MODULE_STATUS_SERVICES = frozenset({"compact-runtime", "fs-app", "fs-worker", "control-plane"})


def read_role_module_status(
    target: ComposeTarget,
    service: str,
    *,
    timeout_seconds: int,
    run_compose: ComposeCommandRunner,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Read what the running ``service`` loaded and skipped of its platform modules.

    ``compose exec -T <service> python -m core.entrypoints.cli.ops.platform_modules
    load-status`` in the running container, which reads the status files its roles
    wrote at start (``core.infrastructure.platform_modules.role_status``). Returns the
    command's ``result``: ``{"roles": {role: {"loaded": [...], "failures": [...]}}}``.
    """
    if service not in _MODULE_STATUS_SERVICES:
        raise StackGatewayError(
            action="platform_modules",
            detail=f"The service {service} does not load platform modules.",
        )
    outcome = run_compose(
        target,
        ("exec", "-T", service, "python", "-m", PLATFORM_MODULE_COMMAND, "load-status"),
        action="platform_modules",
        environment=environment,
        timeout_seconds=timeout_seconds,
        separate_stderr=True,
    )
    raw = str(outcome.stdout or "")
    try:
        if len(raw.encode("utf-8")) > _PLATFORM_MODULE_OUTPUT_LIMIT:
            raise ValueError("oversized platform-module result")
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StackGatewayError(
            action="platform_modules",
            detail="The platform-module load status returned an invalid result.",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or not isinstance(payload.get("result"), dict)
    ):
        raise StackGatewayError(
            action="platform_modules",
            detail="The platform-module load status returned an unsuccessful result.",
        )
    return dict(payload["result"])


class TargetReleaseMigration:
    def __init__(
        self,
        *,
        target_provider: Callable[[], ComposeTarget],
        upgrade: Callable[..., Any],
        environment_provider: Callable[[], Mapping[str, str]],
    ) -> None:
        self.target_provider = target_provider
        self._upgrade = upgrade
        self.environment_provider = environment_provider

    def upgrade(self, *, skip_db_backup: bool, timeout_seconds: int) -> None:
        try:
            self._upgrade(
                self.target_provider(),
                skip_db_backup=skip_db_backup,
                timeout_seconds=timeout_seconds,
                environment=self.environment_provider(),
            )
        except StackGatewayError as exc:
            raise LifecycleError.stable_failure(
                code="release_database_upgrade_failed",
                cause=str(exc),
                remediation="Keep maintenance enabled, correct the reported migration problem, then repeat release upgrade.",
                step="database_upgrade",
            ) from exc


class CheckoutWriterQuiesce:
    """Stop the checkout's writers of the installed development target (D3)."""

    def __init__(
        self,
        *,
        target_provider: Callable[[], ComposeTarget],
        stop: Callable[..., Any],
        environment_provider: Callable[[], Mapping[str, str]],
    ) -> None:
        self.target_provider = target_provider
        self._stop = stop
        self.environment_provider = environment_provider

    def __call__(self) -> None:
        try:
            self._stop(self.target_provider(), environment=self.environment_provider())
        except StackGatewayError as exc:
            raise LifecycleError.stable_failure(
                code="release_checkout_quiesce_failed",
                cause=str(exc),
                remediation=(
                    "Keep maintenance enabled, correct the reported Docker problem, then "
                    "repeat release upgrade."
                ),
                step="release_activate",
            ) from exc
