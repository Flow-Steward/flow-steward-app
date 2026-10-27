"""Mutation service for local-auth user administration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeAlias

from core.application.auth.account_memberships import normalize_membership_account_ids
from core.application.auth.audit import log_auth_audit_event
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.email import normalize_email_address
from core.application.auth.enforce import normalize_requested_roles
from core.application.auth.password import password_hash
from core.application.auth.rate_limit import (
    ClearUserLoginFailures,
    auth_login_clear_user_failures,
)
from core.application.auth.resolve import (
    auth_tables_available,
    get_user_by_id,
    get_user_by_username,
    seed_default_roles,
)
from core.application.auth.user_admin import repository as user_admin_repository
from core.application.auth.user_admin.contracts import (
    PublicUserView,
    UserActiveUpdateInput,
    UserAuditContext,
    UserCreateInput,
    UserDeleteInput,
    UserDeleteResultView,
    UserMutationResultView,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
    UserSelectorInput,
    UserUpdateInput,
)
from core.application.auth.user_admin.notifications import notify_signup_non_blocking
from core.application.auth.user_admin.policies import (
    normalized_roles,
    require_account_access,
    require_password,
    require_user_access,
    resolve_audit_context,
    resolve_user,
)
from core.application.auth.user_admin.projection import normalize_user_payload
from core.application.management.errors import (
    ServiceConflictError,
    ServiceUnavailableError,
    ServiceValidationError,
)

LoggerFactory: TypeAlias = Callable[[], Any]
AuthTablesAvailable: TypeAlias = Callable[[Any], bool]
GetUserById: TypeAlias = Callable[[Any, Any], dict[str, Any] | None]
GetUserByUsername: TypeAlias = Callable[[Any, str], dict[str, Any] | None]
NormalizeRequestedRoles: TypeAlias = Callable[[list[str] | None], list[str]]
SeedDefaultRoles: TypeAlias = Callable[[Any], None]
PasswordHasher: TypeAlias = Callable[[str], str]


class SignupNotifier(Protocol):
    def __call__(
        self,
        *,
        recipient: str,
        display_name: str,
        locale: str,
    ) -> Any: ...


class AuthAuditLogger(Protocol):
    def __call__(
        self,
        bl: Any,
        *,
        action: str,
        username: str,
        actor: str,
        status: str,
        detail: dict[str, Any],
    ) -> None: ...


class UserAdminCommandRepository(Protocol):
    def create_auth_user(
        self,
        bl: Any,
        *,
        email: str,
        password_hash_value: str,
        display_name: str,
        is_active: bool,
        must_change_password: bool,
        roles: list[str],
        primary_account_id: str,
        requested_account_ids: list[str],
        seed_default_roles: SeedDefaultRoles,
    ) -> str: ...

    def update_auth_user(
        self,
        bl: Any,
        *,
        user_id: Any,
        existing_user: dict[str, Any],
        display_name: str | None,
        is_active: bool | None,
        password_hash_value: str | None,
        must_change_password: bool | None,
        roles: list[str] | None,
        requested_account_ids: list[str] | None,
        primary_account_id: str | None,
        revoke_sessions: bool,
    ) -> int: ...

    def force_password_reset(self, bl: Any, *, user_id: Any) -> int: ...

    def delete_auth_user(
        self,
        bl: Any,
        *,
        user_id: Any,
        deleted_by: str,
    ) -> int: ...


class UserAdminCommandService:
    def __init__(
        self,
        *,
        get_logger: LoggerFactory,
        signup_notifier: SignupNotifier | None = None,
        auth_tables_available_fn: AuthTablesAvailable | None = None,
        get_user_by_id_fn: GetUserById | None = None,
        get_user_by_username_fn: GetUserByUsername | None = None,
        normalize_requested_roles_fn: NormalizeRequestedRoles | None = None,
        seed_default_roles_fn: SeedDefaultRoles | None = None,
        password_hash_fn: PasswordHasher | None = None,
        log_auth_audit_event_fn: AuthAuditLogger | None = None,
        auth_login_clear_user_failures_fn: ClearUserLoginFailures | None = None,
        repository: UserAdminCommandRepository | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._signup_notifier = signup_notifier
        self._auth_tables_available = auth_tables_available_fn or auth_tables_available
        self._get_user_by_id = get_user_by_id_fn or get_user_by_id
        self._get_user_by_username = get_user_by_username_fn or get_user_by_username
        self._normalize_requested_roles = normalize_requested_roles_fn or normalize_requested_roles
        self._seed_default_roles = seed_default_roles_fn or seed_default_roles
        self._password_hash = password_hash_fn or password_hash
        self._log_auth_audit_event = log_auth_audit_event_fn or log_auth_audit_event
        self._clear_login_failures = (
            auth_login_clear_user_failures_fn or auth_login_clear_user_failures
        )
        self._repository: UserAdminCommandRepository = repository or user_admin_repository

    def _logger(self) -> Any:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        if not self._auth_tables_available(bl):
            raise ServiceUnavailableError("Auth tables are unavailable in this database schema")
        return bl

    def _resolve(self, bl: Any, *, user_id: Any = None, email: str = "") -> dict[str, Any]:
        return resolve_user(
            bl,
            user_id=user_id,
            email=email,
            get_user_by_id_fn=self._get_user_by_id,
            get_user_by_username_fn=self._get_user_by_username,
        )

    def create_user(
        self,
        command: UserCreateInput,
        *,
        allowed_account_ids: list[str] | None = None,
        notification_locale: str = "en",
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView:
        bl = self._logger()
        try:
            email = normalize_email_address(command.email, field_name="email")
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

        password = require_password(command.password)
        display_name = str(command.display_name or "").strip() or email
        roles = normalized_roles(command.roles, normalize_roles_fn=self._normalize_requested_roles)
        if self._get_user_by_username(bl, email):
            raise ServiceConflictError("Email already exists")

        primary_account_id = str(command.account_id or "").strip() or DEFAULT_ACCOUNT_ID
        requested_account_ids = normalize_membership_account_ids(
            command.account_ids,
            fallback_account_id=primary_account_id,
        )
        require_account_access(requested_account_ids, allowed_account_ids)

        user_id = self._repository.create_auth_user(
            bl,
            email=email,
            password_hash_value=self._password_hash(password),
            display_name=display_name,
            is_active=bool(command.is_active),
            must_change_password=bool(command.must_change_password),
            roles=roles,
            primary_account_id=primary_account_id,
            requested_account_ids=requested_account_ids,
            seed_default_roles=self._seed_default_roles,
        )
        if not user_id:
            raise ServiceUnavailableError("Failed to create user")

        created = self._resolve(bl, user_id=user_id)
        context = resolve_audit_context(command.actor, audit_context)
        self._log_auth_audit_event(
            bl,
            action="user_create",
            username=email,
            actor=context.actor,
            status="ok",
            detail={
                "roles": roles,
                "is_active": bool(command.is_active),
                "must_change_password": bool(command.must_change_password),
                "account_ids": requested_account_ids,
                "primary_account_id": primary_account_id,
                "source": context.source,
            },
        )
        notification_warning = notify_signup_non_blocking(
            self._signup_notifier,
            user_id=str(user_id),
            recipient=email,
            display_name=display_name,
            locale=str(notification_locale or "en"),
        )
        return UserMutationResultView(
            status="created",
            user=PublicUserView.model_validate(normalize_user_payload(created)),
            warnings=[notification_warning] if notification_warning else [],
        )

    def set_password(
        self,
        command: UserPasswordUpdateInput,
        *,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView:
        result = self._update_user(
            UserUpdateInput(
                user_id=command.user_id,
                email=command.email,
                password=command.password,
                must_change_password=command.must_change_password,
                actor=command.actor,
            ),
            revoke_sessions=bool(command.revoke_sessions),
            allowed_account_ids=None,
            audit_context=audit_context,
            audit_action="user_password_set",
        )
        return UserMutationResultView(
            status="password_updated",
            user=result.user,
            revoked_sessions=result.revoked_sessions,
        )

    def force_password_reset(
        self,
        command: UserSelectorInput,
        *,
        allowed_account_ids: list[str] | None = None,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView:
        bl = self._logger()
        user = self._resolve(bl, user_id=command.user_id, email=command.email)
        require_user_access(user, allowed_account_ids)

        revoked_sessions = self._repository.force_password_reset(
            bl,
            user_id=str(user["id"]),
        )

        username = str(user.get("username") or "")
        context = resolve_audit_context(command.actor, audit_context)
        self._clear_login_failures(username, bl=bl)
        updated = self._resolve(bl, user_id=str(user["id"]))
        self._log_auth_audit_event(
            bl,
            action="user_force_password_reset",
            username=username,
            actor=context.actor,
            status="ok",
            detail={"source": context.source, "revoked_sessions": revoked_sessions},
        )
        return UserMutationResultView(
            status="password_reset_required",
            user=PublicUserView.model_validate(normalize_user_payload(updated)),
            revoked_sessions=revoked_sessions,
        )

    def unlock_user(
        self,
        command: UserSelectorInput,
        *,
        allowed_account_ids: list[str] | None = None,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView:
        bl = self._logger()
        user = self._resolve(bl, user_id=command.user_id, email=command.email)
        require_user_access(user, allowed_account_ids)
        username = str(user.get("username") or "")
        context = resolve_audit_context(command.actor, audit_context)
        self._clear_login_failures(username, bl=bl)
        self._log_auth_audit_event(
            bl,
            action="user_unlock",
            username=username,
            actor=context.actor,
            status="ok",
            detail={
                "source": context.source,
                "cleared_lockout_state": True,
            },
        )
        updated = self._resolve(bl, user_id=str(user["id"]))
        return UserMutationResultView(
            status="unlocked",
            user=PublicUserView.model_validate(normalize_user_payload(updated)),
        )

    def delete_user(
        self,
        command: UserDeleteInput,
        *,
        allowed_account_ids: list[str] | None = None,
        audit_context: UserAuditContext | None = None,
    ) -> UserDeleteResultView:
        bl = self._logger()
        user = self._resolve(bl, user_id=command.user_id, email=command.email)
        require_user_access(user, allowed_account_ids)
        user_id = str(user["id"])
        if user_id == command.actor_user_id:
            raise ServiceConflictError("You cannot delete your own user account")

        context = resolve_audit_context(command.actor, audit_context)
        revoked_sessions = self._repository.delete_auth_user(
            bl,
            user_id=user_id,
            deleted_by=context.actor,
        )
        username = str(user.get("username") or "")
        self._clear_login_failures(username, bl=bl)
        self._log_auth_audit_event(
            bl,
            action="user_delete",
            username=username,
            actor=context.actor,
            status="ok",
            detail={"source": context.source, "revoked_sessions": revoked_sessions},
        )
        return UserDeleteResultView(
            user_id=user_id,
            username=username,
            revoked_sessions=revoked_sessions,
        )

    def set_active(
        self,
        command: UserActiveUpdateInput,
        *,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView:
        return self.update_user(
            UserUpdateInput(
                user_id=command.user_id,
                email=command.email,
                is_active=command.is_active,
                actor=command.actor,
            ),
            audit_context=audit_context,
        )

    def set_roles(
        self,
        command: UserRolesUpdateInput,
        *,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView:
        return self.update_user(
            UserUpdateInput(
                user_id=command.user_id,
                email=command.email,
                roles=command.roles,
                actor=command.actor,
            ),
            audit_context=audit_context,
        )

    def update_user(
        self,
        command: UserUpdateInput,
        *,
        revoke_sessions: bool = True,
        allowed_account_ids: list[str] | None = None,
        audit_context: UserAuditContext | None = None,
    ) -> UserMutationResultView:
        return self._update_user(
            command,
            revoke_sessions=revoke_sessions,
            allowed_account_ids=allowed_account_ids,
            audit_context=audit_context,
            audit_action="user_update",
        )

    def _update_user(
        self,
        command: UserUpdateInput,
        *,
        revoke_sessions: bool,
        allowed_account_ids: list[str] | None,
        audit_context: UserAuditContext | None,
        audit_action: str,
    ) -> UserMutationResultView:
        bl = self._logger()
        user = self._resolve(bl, user_id=command.user_id, email=command.email)
        require_user_access(user, allowed_account_ids)
        if all(
            value is None
            for value in (
                command.display_name,
                command.password,
                command.is_active,
                command.must_change_password,
                command.roles,
                command.account_ids,
                command.account_id,
            )
        ):
            raise ServiceValidationError("No changes requested")

        password_value = (
            require_password(command.password) if command.password is not None else None
        )
        roles = (
            normalized_roles(command.roles, normalize_roles_fn=self._normalize_requested_roles)
            if command.roles is not None
            else None
        )
        requested_account_ids: list[str] | None = None
        primary_account_id: str | None = None
        if command.account_ids is not None or command.account_id is not None:
            requested_account_ids = normalize_membership_account_ids(
                command.account_ids
                if command.account_ids is not None
                else list(user.get("account_ids") or []),
                fallback_account_id=(
                    str(command.account_id or "").strip()
                    or str(user.get("account_id") or "").strip()
                    or DEFAULT_ACCOUNT_ID
                ),
            )
            primary_account_id = str(command.account_id or "").strip() or (
                str(user.get("account_id") or "").strip()
                if str(user.get("account_id") or "").strip() in requested_account_ids
                else requested_account_ids[0]
            )
            require_account_access(requested_account_ids, allowed_account_ids)

        if roles is not None:
            self._seed_default_roles(bl)
        revoked_sessions = self._repository.update_auth_user(
            bl,
            user_id=str(user["id"]),
            existing_user=user,
            display_name=command.display_name,
            is_active=command.is_active,
            password_hash_value=(
                self._password_hash(password_value) if password_value is not None else None
            ),
            must_change_password=command.must_change_password,
            roles=roles,
            requested_account_ids=requested_account_ids,
            primary_account_id=primary_account_id,
            revoke_sessions=revoke_sessions,
        )

        username = str(user.get("username") or "")
        if password_value is not None:
            self._clear_login_failures(username, bl=bl)
        updated = self._resolve(bl, user_id=str(user["id"]))
        context = resolve_audit_context(command.actor, audit_context)
        if audit_action == "user_password_set":
            detail = {
                "source": context.source,
                "revoked_sessions": revoked_sessions,
                "must_change_password": command.must_change_password,
            }
        else:
            detail = {
                "source": context.source,
                "changed_display_name": command.display_name is not None,
                "changed_password": command.password is not None,
                "changed_roles": command.roles is not None,
                "changed_is_active": command.is_active is not None,
                "changed_must_change_password": command.must_change_password is not None,
                "changed_accounts": command.account_ids is not None
                or command.account_id is not None,
            }
        self._log_auth_audit_event(
            bl,
            action=audit_action,
            username=username,
            actor=context.actor,
            status="ok",
            detail=detail,
        )
        return UserMutationResultView(
            status="updated",
            user=PublicUserView.model_validate(normalize_user_payload(updated)),
            revoked_sessions=revoked_sessions,
        )
