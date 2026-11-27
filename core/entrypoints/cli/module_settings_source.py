"""Where the operator CLI reads the settings the active platform modules declare.

ADR 0082 (module settings): ``config set`` and ``./install.sh --setting`` accept a
``FS_MODULE_*`` name only while an active module declares it. The active list lives
in the runtime directory:

* Scalable, or a console inside the Compact runtime container: this process can read
  that directory, so the module store reads ``modules.json`` itself;
* Compact from the host CLI or the installer: the directory is inside the
  ``compact-runtime`` data volume, so the in-image ``settings`` command runs in a
  disposable container of the installed runtime image, as the release workflow's
  module commands do.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from core.infrastructure.platform_modules.loader import platform_modules_root
from core.infrastructure.platform_modules.module_settings import (
    ModuleSettingDeclaration,
    declarations_from_listing,
)
from core.infrastructure.platform_modules.module_store import (
    ModuleStoreError,
    PlatformModuleStore,
)

if TYPE_CHECKING:
    from core.application.lifecycle.configuration_service import ConfigurationService
    from core.application.lifecycle.contracts import DeploymentConfig
    from core.application.lifecycle.module_settings import ModuleSettingSource
    from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway


def filesystem_module_settings(root: Path | None = None) -> tuple[ModuleSettingDeclaration, ...]:
    """The settings the active list under *root* declares; this process reads it."""
    modules_root = platform_modules_root() if root is None else Path(root)
    try:
        present = modules_root.parent.is_dir()
    except OSError as exc:
        raise RuntimeError(
            f"The runtime directory {modules_root.parent} cannot be read: {exc}"
        ) from exc
    if not present:
        raise RuntimeError(
            f"The runtime directory {modules_root.parent} is not available in this process."
        )
    try:
        return PlatformModuleStore(modules_root).declared_settings()
    except ModuleStoreError as exc:
        raise RuntimeError(f"{exc.code}: {exc.cause}") from exc


def module_settings_source(
    service: ConfigurationService,
    *,
    docker: Callable[[], DockerLifecycleGateway],
    compact_from_host: bool,
    root: Path | None = None,
) -> ModuleSettingSource:
    """The ``ConfigurationService.module_settings`` port for this process."""

    def read(config: DeploymentConfig) -> tuple[ModuleSettingDeclaration, ...]:
        if config.deployment_profile != "compact" or not compact_from_host:
            return filesystem_module_settings(root)
        result = docker().platform_module_command(
            service.derive_target(config),
            ("settings",),
            environment=service.store.read_durable_environment(config),
            timeout_seconds=120,
        )
        return declarations_from_listing(result)

    return read


__all__ = ["filesystem_module_settings", "module_settings_source"]
