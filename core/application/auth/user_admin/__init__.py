"""Shared user-administration contracts and services."""

from core.application.auth.user_admin.command_service import UserAdminCommandService
from core.application.auth.user_admin.contracts import (
    AccountChoiceView,
    AccountListView,
    PublicUserView,
    UserActiveUpdateInput,
    UserAuditContext,
    UserCreateInput,
    UserDeleteInput,
    UserDeleteResultView,
    UserListView,
    UserMutationResultView,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
    UserSelectorInput,
    UserUpdateInput,
)
from core.application.auth.user_admin.query_service import UserAdminQueryService

__all__ = [
    "AccountChoiceView",
    "AccountListView",
    "PublicUserView",
    "UserActiveUpdateInput",
    "UserAdminCommandService",
    "UserAdminQueryService",
    "UserAuditContext",
    "UserCreateInput",
    "UserDeleteInput",
    "UserDeleteResultView",
    "UserListView",
    "UserMutationResultView",
    "UserPasswordUpdateInput",
    "UserRolesUpdateInput",
    "UserSelectorInput",
    "UserUpdateInput",
]
