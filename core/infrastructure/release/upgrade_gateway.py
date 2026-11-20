"""Run database tooling from the prepared target's product image."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_service import StackGatewayError
from core.infrastructure.lifecycle.docker_gateway_contracts import ComposeCommandRunner


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
    writers = (
        ("compact-runtime",)
        if compact
        else ("fs-app", "fs-worker", "control-plane", "fs-mcp", "fs-extension-installer")
    )
    running = run_compose(
        target,
        ("ps", "--status", "running", "--services"),
        action="database_upgrade",
        environment=environment,
        timeout_seconds=30,
    )
    services = set(str(running.stdout or "").splitlines())
    stopping = tuple(service for service in writers if service in services)
    if stopping:
        run_compose(
            target,
            ("stop", *stopping),
            action="database_upgrade",
            environment=environment,
            timeout_seconds=120,
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
