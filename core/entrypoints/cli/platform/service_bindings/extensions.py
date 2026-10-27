"""Extension-domain CLI bindings to shared services and typed tooling operations."""

from __future__ import annotations

from pathlib import Path

from core.application.extensions.scaffold import ExtensionScaffoldCreateInput
from core.application.management.command_specs.extensions import (
    ExtensionInstallCommandInput,
    ExtensionShowCommandInput,
    ExtensionsListCommandInput,
    ExtensionToggleCommandInput,
    ExtensionUninstallCommandInput,
    ExtensionUninstallRequestInput,
)
from core.application.management.errors import ServiceValidationError
from core.application.tooling.extensions.bundle_extension import (
    ExtensionBundleRequest,
    bundle_extension,
)
from core.application.tooling.extensions.deps import (
    ExtensionDependencyLockRequest,
    lock_extension_dependencies,
)
from core.application.tooling.extensions.test_extension import ExtensionTestRequest, test_extension
from core.application.tooling.extensions.validate_extensions import (
    ExtensionValidationRequest,
    validate_extensions,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    PreparedCommand,
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    cli_admin_scope,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


async def extensions_uninstall_preview(runtime: RuntimeServices, value: object) -> PreparedCommand:
    command = (
        value
        if isinstance(value, ExtensionUninstallRequestInput)
        else ExtensionUninstallRequestInput.model_validate(value)
    )
    plan = await await_maybe(
        runtime.extension_binding_service.preview_extension_uninstall(
            command.extension_id,
            scope=cli_admin_scope(),
        )
    )
    review = dict(plan) if isinstance(plan, dict) else plan.model_dump()
    revision = str(review.get("revision") or "").strip()
    if not revision:
        raise ServiceValidationError("Extension uninstall preview returned no revision")
    return PreparedCommand(
        execution_input=ExtensionUninstallCommandInput(
            extension_id=command.extension_id,
            plan_revision=revision,
        ),
        review=review,
    )


async def extensions_list(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionsListCommandInput)
        else ExtensionsListCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.extension_query_service.list_extensions(
            filters={"state": [] if command.state == "all" else [command.state]},
            scope=cli_admin_scope(),
        )
    )


async def extensions_show(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionShowCommandInput)
        else ExtensionShowCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.extension_query_service.get_extension(
            command.extension_id,
            scope=cli_admin_scope(),
        )
    )


async def extensions_health(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionShowCommandInput)
        else ExtensionShowCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.extension_query_service.get_health(
            command.extension_id,
            scope=cli_admin_scope(),
        )
    )


async def extensions_install(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionInstallCommandInput)
        else ExtensionInstallCommandInput.model_validate(value)
    )
    archive_path = Path(command.archive)
    host_project_root = str(_runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT") or "").strip()
    if not archive_path.is_absolute() and host_project_root:
        archive_path = Path(host_project_root) / archive_path
    if command.approved_browser_authority_impact_hash:
        return await await_maybe(
            runtime.extension_binding_service.install_extension_archive_path(
                archive_path=archive_path,
                scope=cli_admin_scope(),
                approved_browser_authority_impact_hash=(
                    command.approved_browser_authority_impact_hash
                ),
            )
        )
    return await await_maybe(
        runtime.extension_binding_service.install_extension_archive_path(
            archive_path=archive_path,
            scope=cli_admin_scope(),
        )
    )


async def extensions_uninstall(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, ExtensionUninstallCommandInput)
        else ExtensionUninstallCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.extension_binding_service.uninstall_extension(
            command.extension_id,
            plan_revision=command.plan_revision,
            scope=cli_admin_scope(),
        )
    )


async def extensions_enable(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionToggleCommandInput)
        else ExtensionToggleCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.extension_binding_service.set_enabled_state(
            command.extension_id,
            version=command.version or None,
            enabled=True,
            config=command.config,
            scope=cli_admin_scope(),
            approved_browser_authority_impact_hash=(command.approved_browser_authority_impact_hash),
        )
    )


async def extensions_disable(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionToggleCommandInput)
        else ExtensionToggleCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.extension_binding_service.set_enabled_state(
            command.extension_id,
            version=command.version or None,
            enabled=False,
            config=command.config,
            scope=cli_admin_scope(),
        )
    )


async def extensions_create(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionScaffoldCreateInput)
        else ExtensionScaffoldCreateInput.model_validate(value)
    )
    return await await_maybe(runtime.extension_scaffold_service.create(command))


def extensions_validate(_runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionValidationRequest)
        else ExtensionValidationRequest.model_validate(value)
    )
    result = validate_extensions(command)
    if result.exit_code != 0:
        raise ServiceValidationError(result.output.strip() or "Extension validation failed")
    return result


def extensions_test(_runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionTestRequest)
        else ExtensionTestRequest.model_validate(value)
    )
    result = test_extension(command)
    if result.exit_code != 0:
        raise ServiceValidationError(result.output.strip() or "Extension tests failed")
    return result


def extensions_bundle(_runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionBundleRequest)
        else ExtensionBundleRequest.model_validate(value)
    )
    result = bundle_extension(command)
    if result.exit_code != 0:
        raise ServiceValidationError(result.output.strip() or "Extension bundle failed")
    return result


def extensions_lock_deps(_runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ExtensionDependencyLockRequest)
        else ExtensionDependencyLockRequest.model_validate(value)
    )
    result = lock_extension_dependencies(command)
    if result.exit_code != 0:
        raise ServiceValidationError(result.output.strip() or "Extension dependency lock failed")
    return result


EXTENSION_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "extensions.list": extensions_list,
    "extensions.show": extensions_show,
    "extensions.health": extensions_health,
    "extensions.install": extensions_install,
    "extensions.uninstall": extensions_uninstall,
    "extensions.enable": extensions_enable,
    "extensions.disable": extensions_disable,
    "extensions.create": extensions_create,
    "extensions.validate": extensions_validate,
    "extensions.test": extensions_test,
    "extensions.bundle": extensions_bundle,
    "extensions.lock-deps": extensions_lock_deps,
}

EXTENSION_PLAN_BUILDERS = {
    "extensions.uninstall.preview": extensions_uninstall_preview,
}
