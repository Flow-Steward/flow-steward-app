"""Exact-command management composition; other users commands remain product-owned."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

from core.application.management.errors import ServicePreconditionError
from core.entrypoints.cli.platform.click_adapter import build_click_app
from core.entrypoints.cli.platform.service_registry import ServiceRegistry
from core.infrastructure.runtime_env import runtime_env


class BootstrapRecoveryRuntime:
    @cached_property
    def bootstrap_token_service(self) -> Any:
        from core.application.auth.bootstrap_recovery import BootstrapRecoveryService
        from core.application.auth.bootstrap_recovery_gateway import (
            BootstrapRecoveryGateway,
        )
        from core.application.lifecycle.configuration_service import ConfigurationService
        from core.infrastructure.installer.gateway import DockerInstallerGateway
        from core.infrastructure.installer.state_store import InstallerStateStore
        from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

        root = Path(runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT", ""))
        state_root = Path(runtime_env.get("FS_INSTALLER_STATE_DIR", ""))
        if not root.is_absolute() or not state_root.is_absolute():
            raise ServicePreconditionError("The installed host recovery descriptor is required.")
        state = InstallerStateStore(state_root, lifecycle_state_root=root / ".flow-steward")
        configuration = ConfigurationService(
            store=LifecycleConfigurationStore(root, state_root=state_root)
        )
        installer = DockerInstallerGateway(
            repo_root=root, state_store=state, configuration_service=configuration
        )
        return BootstrapRecoveryService(
            gateway=BootstrapRecoveryGateway(installer=installer, selection=runtime_env)
        )


class BootstrapRecoveryRegistry(ServiceRegistry):
    def service(self, key: str) -> Any:
        if key != "users.issue-bootstrap-token":
            raise ServicePreconditionError(
                "This private management entrypoint serves only users issue-bootstrap-token."
            )
        return _issue_bootstrap_token


def _issue_bootstrap_token(runtime: Any, _value: object, **_kwargs: object) -> object:
    from core.application.installer.errors import InstallerFailure
    from core.application.lifecycle.errors import LifecycleError
    from core.application.management.command_specs.contracts import ExitCode

    try:
        return runtime.bootstrap_token_service.issue()
    except InstallerFailure as failure:
        recovery_needed = failure.code == "bootstrap_recovery_needed"
        raise LifecycleError(
            code=failure.code,
            summary="Bootstrap recovery requires reconciliation."
            if recovery_needed
            else "Bootstrap recovery failed.",
            cause=failure.cause,
            remediation=failure.remediation,
            retryable=False if recovery_needed else failure.retryable,
            step=failure.step,
            exit_code=ExitCode.RECOVERY_REQUIRED
            if recovery_needed
            else ExitCode(failure.exit_code),
        ) from None


_runtime = BootstrapRecoveryRuntime()
click_app = build_click_app(BootstrapRecoveryRegistry(_runtime), _runtime)

if __name__ == "__main__":
    click_app(prog_name="flow-steward")
