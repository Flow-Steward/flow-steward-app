"""User-domain CLI bindings to shared user admin services."""

from __future__ import annotations

from core.application.auth.user_admin.contracts import (
    UserActiveUpdateInput,
    UserAuditContext,
    UserCreateInput,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
    UserSelectorInput,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    payload,
)


async def users_list(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    return await await_maybe(runtime.user_query_service.list_users(**payload(value)))


async def users_create(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = value if isinstance(value, UserCreateInput) else UserCreateInput.model_validate(value)
    return await await_maybe(
        runtime.user_command_service.create_user(
            command,
            audit_context=_cli_audit_context(command),
        )
    )


async def users_set_password(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, UserPasswordUpdateInput)
        else UserPasswordUpdateInput.model_validate(value)
    )
    return await await_maybe(
        runtime.user_command_service.set_password(
            command,
            audit_context=_cli_audit_context(command),
        )
    )


async def users_force_reset(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value if isinstance(value, UserSelectorInput) else UserSelectorInput.model_validate(value)
    )
    return await await_maybe(
        runtime.user_command_service.force_password_reset(
            command,
            audit_context=_cli_audit_context(command),
        )
    )


async def users_unlock(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value if isinstance(value, UserSelectorInput) else UserSelectorInput.model_validate(value)
    )
    return await await_maybe(
        runtime.user_command_service.unlock_user(
            command,
            audit_context=_cli_audit_context(command),
        )
    )


async def users_set_active(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, UserActiveUpdateInput)
        else UserActiveUpdateInput.model_validate(value)
    )
    return await await_maybe(
        runtime.user_command_service.set_active(
            command,
            audit_context=_cli_audit_context(command),
        )
    )


async def users_set_roles(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, UserRolesUpdateInput)
        else UserRolesUpdateInput.model_validate(value)
    )
    return await await_maybe(
        runtime.user_command_service.set_roles(
            command,
            audit_context=_cli_audit_context(command),
        )
    )


async def users_issue_bootstrap_token(
    runtime: RuntimeServices, _value: object, **_kwargs: object
) -> object:
    return await await_maybe(runtime.bootstrap_token_service.issue())


def _cli_audit_context(command: object) -> UserAuditContext:
    return UserAuditContext(
        actor=str(getattr(command, "actor", "") or "cli"),
        source="cli",
    )


USER_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "users.list": users_list,
    "users.create": users_create,
    "users.set-password": users_set_password,
    "users.force-reset": users_force_reset,
    "users.unlock": users_unlock,
    "users.set-active": users_set_active,
    "users.set-roles": users_set_roles,
    "users.issue-bootstrap-token": users_issue_bootstrap_token,
}
