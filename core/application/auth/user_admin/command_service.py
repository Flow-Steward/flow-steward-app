"""Mutation service for local-auth user administration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeAlias

from core.application.auth import mfa_repository
from core.application.auth.account_memberships import normalize_membership_account_ids
from core.application.auth.audit import log_auth_audit_event
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.email import normalize_email_address
from core.application.auth.enforce import normalize_requested_roles
from core.application.auth.local_credentials import (
    authenticator_policy_available,
    authenticator_storage_available,
)
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
    UserAuthenticatorAction,
    UserAuthenticatorResult,
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
from core.application.auth.user_admin.repository import FirstUserMustBeAdminError
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.infrastructure.db.db_pool import transaction_connection

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
        password: str | None,
        password_hasher: PasswordHasher,
        must_change_password: bool | None,
        roles: list[str] | None,
        requested_account_ids: list[str] | None,
        primary_account_id: str | None,
        revoke_sessions: bool,
        on_scope_limit_lifted: Callable[[], None] | None = None,
        actor: str = "",
        role_account_id: str | None = None,
    ) -> int: ...

    def force_password_reset(self, bl: Any, *, user_id: Any) -> int: ...

    def delete_auth_user(
        self,
        bl: Any,
        *,
        user_id: Any,
        deleted_by: str,
    ) -> int: ...


def _require_update_access(
    user: dict[str, Any],
    command: UserUpdateInput,
    role_account_id: str | None,
    allowed_account_ids: list[str] | None,
) -> None:
    """Who may make the change (ADR 0085).

    A person never changes their own role. A change of the role alone needs the
    account whose role changes to be one the administrator manages, even when the
    person also belongs to other accounts; every other change needs every account of
    the person to be managed by the administrator.
    """
    if command.role is not None:
        actor_user_id = str(command.actor_user_id or "").strip().casefold()
        if actor_user_id and actor_user_id == str(user.get("id") or "").strip().casefold():
            raise ServiceForbiddenError("You cannot change your own role")
    role_only = command.role is not None and all(
        value is None
        for value in (
            command.display_name,
            command.password,
            command.is_active,
            command.must_change_password,
            command.account_ids,
            command.account_id,
        )
    )
    if role_only and allowed_account_ids is not None:
        account = role_account_id or str(user.get("account_id") or "").strip()
        require_account_access([account], allowed_account_ids)
        members_of = {str(item or "").strip() for item in user.get("account_ids") or []}
        if account not in members_of:
            # Not a member of the administrator's account: out of reach, as before.
            raise ServiceForbiddenError("account access denied")
        return
    require_user_access(user, allowed_account_ids)
    if role_account_id is not None:
        require_account_access([role_account_id], allowed_account_ids)


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
        # No role means the default the repository decides under its lock: admin
        # for an installation's first user, viewer after that.
        roles = (
            normalized_roles([command.role], normalize_roles_fn=self._normalize_requested_roles)
            if command.role
            else []
        )
        if self._get_user_by_username(bl, email):
            raise ServiceConflictError("Email already exists")

        primary_account_id = str(command.account_id or "").strip() or DEFAULT_ACCOUNT_ID
        requested_account_ids = normalize_membership_account_ids(
            command.account_ids,
            fallback_account_id=primary_account_id,
        )
        require_account_access(requested_account_ids, allowed_account_ids)

        try:
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
        except FirstUserMustBeAdminError as exc:
            raise ServiceValidationError(str(exc)) from exc
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
                "roles": list(created.get("roles") or roles),
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

    def update_authenticator(
        self,
        *,
        user_id: str,
        actor_user_id: str,
        actor: str,
        action: UserAuthenticatorAction,
        allowed_account_ids: list[str],
    ) -> UserAuthenticatorResult:
        if not actor_user_id or actor_user_id.casefold() == user_id.casefold():
            raise ServiceForbiddenError("An administrator must manage another user's authenticator")
        bl = self._logger()
        if not (authenticator_storage_available(bl) and authenticator_policy_available(bl)):
            raise ServiceUnavailableError("Authenticator storage is unavailable")

        revoked_sessions = 0
        with transaction_connection(bl) as conn, conn.cursor() as cur:
            target = mfa_repository.lock_admin_target(cur, user_id)
            if target is None:
                raise ServiceNotFoundError(f"Local user id={user_id} not found")
            username, primary_account_id, required, reset_pending = target
            membership_ids = mfa_repository.admin_target_membership_ids(cur, user_id)
            require_account_access([primary_account_id, *membership_ids], allowed_account_ids)

            if action == "reset":
                mfa_repository.delete_factor(cur, user_id)
                mfa_repository.mark_reset_pending(cur, user_id)
                revoked_sessions = mfa_repository.revoke_sessions(cur, user_id)
            else:
                new_requirement = action == "require"
                if required != new_requirement:
                    mfa_repository.set_authenticator_required(cur, user_id, new_requirement)
                    if not reset_pending and not mfa_repository.active_factor_exists(cur, user_id):
                        mfa_repository.clear_pending_factor(cur, user_id)
                        if new_requirement:
                            revoked_sessions = mfa_repository.revoke_sessions(cur, user_id)
                required = new_requirement

        self._log_auth_audit_event(
            bl,
            action=f"user_authenticator_{action}",
            username=username,
            actor=actor,
            status="ok",
            detail={"source": "api", "revoked_sessions": revoked_sessions},
        )
        status = (
            "authenticator_reset"
            if action == "reset"
            else ("authenticator_required" if required else "authenticator_optional")
        )
        return UserAuthenticatorResult(
            status=status, authenticator_required=required, revoked_sessions=revoked_sessions
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
                role=command.role,
                role_account_id=command.role_account_id,
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
        role_account_id = str(command.role_account_id or "").strip() or None
        _require_update_access(user, command, role_account_id, allowed_account_ids)
        if all(
            value is None
            for value in (
                command.display_name,
                command.password,
                command.is_active,
                command.must_change_password,
                command.role,
                command.account_ids,
                command.account_id,
            )
        ):
            raise ServiceValidationError("No changes requested")

        password_value = (
            require_password(command.password) if command.password is not None else None
        )
        roles = (
            normalized_roles([command.role], normalize_roles_fn=self._normalize_requested_roles)
            if command.role is not None
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
        lifted_scope_limit: list[bool] = []
        revoked_sessions = self._repository.update_auth_user(
            bl,
            user_id=str(user["id"]),
            existing_user=user,
            display_name=command.display_name,
            is_active=command.is_active,
            password=password_value,
            password_hasher=self._password_hash,
            must_change_password=command.must_change_password,
            roles=roles,
            requested_account_ids=requested_account_ids,
            primary_account_id=primary_account_id,
            revoke_sessions=revoke_sessions,
            on_scope_limit_lifted=lambda: lifted_scope_limit.append(True),
            actor=resolve_audit_context(command.actor, audit_context).actor,
            role_account_id=role_account_id,
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
                "changed_roles": command.role is not None,
                "role_account_id": role_account_id or str(user.get("account_id") or "").strip(),
                "changed_is_active": command.is_active is not None,
                "changed_must_change_password": command.must_change_password is not None,
                "changed_accounts": command.account_ids is not None
                or command.account_id is not None,
            }
        if lifted_scope_limit:
            detail["lifted_project_scope_limit"] = True
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
