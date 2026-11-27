from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from core.application.auth import user_profile_repository
from core.application.auth.account_memberships import normalize_membership_account_ids
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.csrf import csrf_request_valid as default_csrf_request_valid
from core.application.auth.permissions import ADMIN_ROLE
from core.application.auth.principal import is_token_scoped_principal
from core.application.auth.rate_limit import ClearUserLoginFailures
from core.application.auth.serve_auth_runtime import public_base_url
from core.application.auth.user_admin.account_invitations import (
    AccountInvitationService,
)
from core.application.auth.user_admin.account_members import AccountMemberService
from core.application.auth.user_admin.command_service import UserAdminCommandService
from core.application.auth.user_admin.contracts import (
    UserAuditContext,
    UserAuthenticatorResult,
    UserCreateInput,
    UserDeleteInput,
    UserSelectorInput,
    UserUpdateInput,
)
from core.application.auth.user_admin.member_lookup_throttle import LookupThrottledError
from core.application.auth.user_admin.projection import limit_view_to_accounts
from core.application.auth.user_admin.query_service import UserAdminQueryService
from core.application.extensions.system_email import (
    send_signup_notification_email,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.requests import (
    AuthAccountInvitationRequest,
    AuthAccountMemberAddRequest,
    AuthInvitationAcceptRequest,
    AuthInvitationTokenRequest,
    AuthUserAuthenticatorRequest,
    AuthUserCreateRequest,
    AuthUserProfileUpdateRequest,
    AuthUserUpdateRequest,
)
from core.application.settings.ui_i18n import (
    resolve_ui_locale,
)
from core.entrypoints.api import auth_users_introspection as _introspection
from core.entrypoints.api import auth_users_preferences as _preferences
from core.entrypoints.api.auth_users_email_change_routes import (
    EMAIL_CHANGE_SCHEMA_NOT_APPLIED as EMAIL_CHANGE_SCHEMA_NOT_APPLIED,
)
from core.entrypoints.api.auth_users_email_change_routes import register_email_change_routes
from core.entrypoints.api.auth_users_timezone_locale_routes import (
    register_timezone_locale_routes,
)

#: The conflict ``create_user`` raises for an address someone already has.
EMAIL_ALREADY_EXISTS_MESSAGE = "Email already exists"

#: Refusal of a profile write from a session of external sign-in (ADR 0085, F).
EXTERNAL_PROFILE_REFUSED_CODE = "external_session_profile_read_only"
EXTERNAL_PROFILE_REFUSED_MESSAGE = (
    "Your profile cannot be changed from this sign-in. Sign in with your password to change it."
)
#: Refusal of an invitation answer from a token: only the person, signed in, answers.
INVITATIONS_NEED_SESSION_MESSAGE = (
    "Open the invitation link in your browser while signed in to answer it."
)


def _external_session(request: Request, user: dict[str, Any] | None) -> bool:
    """Whether the request runs in a session issued by external sign-in (ADR 0083)."""
    sources = {
        str(getattr(request.state, "auth_source", "") or "").strip().lower(),
        str((user or {}).get("auth_source") or "").strip().lower(),
    }
    return "external" in sources


def admin_managed_account_ids(admin_user: dict[str, Any] | None) -> list[str]:
    """The accounts an administrator's user management may reach.

    Roles are held per account (ADR 0085): only accounts where the person's role is
    admin, never every account they are a member of. The acting account counts when
    the request's roles (the acting account's) hold admin. An external sign-in is
    pinned to the account that issued it (ADR 0083): its session manages that account
    only, whatever other memberships the person holds. A personal access token or an
    inbound OAuth grant acts in its own account only (ADR 0085 §4), so it manages that
    account and no other.
    """
    if not isinstance(admin_user, dict):
        return []
    acting = str(admin_user.get("account_id") or "").strip()
    acting_roles = {
        str(role or "").strip().lower() for role in admin_user.get("roles") or [] if role
    }
    if str(
        admin_user.get("auth_source") or ""
    ).strip().lower() == "external" or is_token_scoped_principal(admin_user):
        return [acting] if acting and ADMIN_ROLE in acting_roles else []
    seen: set[str] = set()
    account_ids: list[str] = []

    def add(raw: Any) -> None:
        account_id = str(raw or "").strip()
        if account_id and account_id not in seen:
            seen.add(account_id)
            account_ids.append(account_id)

    if ADMIN_ROLE in acting_roles:
        add(acting)
    for membership in admin_user.get("account_memberships") or []:
        if (
            isinstance(membership, dict)
            and str(membership.get("role") or "").strip().lower() == ADMIN_ROLE
        ):
            add(membership.get("account_id"))
    return account_ids


def create_auth_users_router(
    *,
    local_auth_enabled: Callable[[], bool],
    external_auth_enabled: Callable[[], bool],
    get_logger_only: Callable[[], Any],
    auth_tables_available: Callable[[Any], bool],
    require_authenticated_user: Callable[..., dict],
    request_auth_user_payload: Callable[[Request | None], dict | None],
    api_error_response: Callable[[Exception], JSONResponse],
    log_auth_audit_event: Callable[..., None],
    auth_login_clear_user_failures: ClearUserLoginFailures,
    get_user_by_id: Callable[[Any, Any], dict | None],
    get_user_by_username: Callable[[Any, str], dict | None],
    normalize_requested_roles: Callable[[list[str] | None], list[str]],
    seed_default_roles: Callable[[Any], None],
    password_hash: Callable[[str], str],
    auth_user_column_exists: Callable[[Any, str], bool],
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
    auth_admin_roles: list[str] | tuple[str, ...] | set[str],
    err_authentication_required: str,
    err_database_not_configured: str,
    err_auth_tables_not_available: str,
    err_admin_role_required: str,
    err_local_auth_disabled: str,
    err_invalid_json_body: str,
    auth_csrf_cookie_name: str,
    csrf_request_valid: Callable[[Request], bool] = default_csrf_request_valid,
    user_query_service: UserAdminQueryService | None = None,
    user_command_service: UserAdminCommandService | None = None,
    account_member_service: AccountMemberService | None = None,
    account_invitation_service: AccountInvitationService | None = None,
) -> APIRouter:
    del auth_user_column_exists
    router = APIRouter(tags=["auth-users"])
    user_query_service = user_query_service or UserAdminQueryService(
        get_logger=get_logger_only,
        auth_tables_available_fn=auth_tables_available,
        get_user_by_id_fn=get_user_by_id,
        get_user_by_username_fn=get_user_by_username,
    )
    user_command_service = user_command_service or UserAdminCommandService(
        get_logger=get_logger_only,
        signup_notifier=lambda **kwargs: send_signup_notification_email(**kwargs),
        auth_tables_available_fn=auth_tables_available,
        get_user_by_id_fn=get_user_by_id,
        get_user_by_username_fn=get_user_by_username,
        normalize_requested_roles_fn=normalize_requested_roles,
        seed_default_roles_fn=seed_default_roles,
        password_hash_fn=password_hash,
        log_auth_audit_event_fn=log_auth_audit_event,
        auth_login_clear_user_failures_fn=auth_login_clear_user_failures,
    )

    account_member_service = account_member_service or AccountMemberService(
        get_logger=get_logger_only,
        audit=log_auth_audit_event,
    )
    account_invitation_service = account_invitation_service or AccountInvitationService(
        get_logger=get_logger_only,
        audit=log_auth_audit_event,
    )

    service_error_statuses: tuple[tuple[type[ServiceError], int], ...] = (
        (ServiceValidationError, 400),
        (ServiceNotFoundError, 404),
        (ServiceForbiddenError, 403),
        (ServiceConflictError, 409),
        (ServiceUnavailableError, 503),
    )

    def _user_service_error(exc: Exception) -> JSONResponse:
        # A service error's `message` is the text it was raised with for the
        # user; anything else goes through the logging generic responder.
        if isinstance(exc, LookupThrottledError):
            return JSONResponse(
                {"error": exc.message},
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )
        for error_type, status_code in service_error_statuses:
            if isinstance(exc, error_type):
                return JSONResponse({"error": exc.message}, status_code=status_code)
        return api_error_response(exc)

    def _user_input_error(exc: ValidationError) -> JSONResponse:
        messages = [
            str(error.get("msg") or "Invalid input") for error in exc.errors(include_input=False)
        ]
        return JSONResponse(
            {"error": "; ".join(messages) or "Invalid input"},
            status_code=400,
        )

    _admin_managed_account_ids = admin_managed_account_ids

    def _accounts_allowed(account_ids: list[str], managed_account_ids: list[str]) -> bool:
        managed = {str(account_id or "").strip() for account_id in managed_account_ids}
        requested = [str(account_id or "").strip() for account_id in account_ids]
        requested = [account_id for account_id in requested if account_id]
        return bool(requested) and all(account_id in managed for account_id in requested)

    def _account_access_denied() -> JSONResponse:
        return JSONResponse({"error": "account access denied"}, status_code=403)

    @router.get("/api/auth/introspect")
    async def api_auth_introspect(request: Request):
        """Return the bounded effective principal required by transport adapters."""
        principal = getattr(request.state, "auth_user", None)
        if not isinstance(principal, dict):
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        return _introspection.introspection_payload(request, principal)

    @router.get("/api/auth/me")
    def api_auth_me(request: Request):
        """Return current authenticated user."""
        if not (local_auth_enabled() or external_auth_enabled()):
            return {
                "authenticated": False,
                "local_auth_enabled": False,
                "external_auth_enabled": False,
            }
        user = getattr(request.state, "auth_user", None)
        if not isinstance(user, dict):
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        auth_source = (
            str(getattr(request.state, "auth_source", "") or user.get("auth_source") or "local")
            .strip()
            .lower()
        )
        must_change_password = (
            bool(user.get("must_change_password", False)) if auth_source == "local" else False
        )
        preferences = _preferences.session_preferences(user, get_logger_only)
        return {
            "authenticated": True,
            "local_auth_enabled": local_auth_enabled(),
            "external_auth_enabled": external_auth_enabled(),
            "auth_source": auth_source,
            "csrf_token": str(request.cookies.get(auth_csrf_cookie_name) or "")
            if auth_source == "local"
            else "",
            "user": {
                "id": str(user.get("id") or "").strip(),
                "email": str(user.get("email") or user.get("username") or ""),
                "username": str(user.get("username") or ""),
                "display_name": str(user.get("display_name") or ""),
                "roles": list(user.get("roles") or []),
                "account_id": str(user.get("account_id") or ""),
                "account_ids": list(user.get("account_ids") or []),
                "account_memberships": list(user.get("account_memberships") or []),
                "must_change_password": must_change_password,
                **preferences,
            },
        }

    @router.patch("/api/users/me/date-time-format")
    async def api_user_update_date_time_format(request: Request):
        """Update the authenticated user's date pattern and clock."""
        user = request_auth_user_payload(request)
        user_id = str(user.get("id") or "").strip() if isinstance(user, dict) else ""
        if not user_id:
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        return await _preferences.save_date_time_format(
            request,
            user_id,
            get_logger_only=get_logger_only,
            err_invalid_json_body=err_invalid_json_body,
            err_database_not_configured=err_database_not_configured,
        )

    @router.patch("/api/users/me/profile")
    async def api_user_update_profile(request: Request, body: AuthUserProfileUpdateRequest):
        """Update the authenticated user's basic profile fields."""
        if not (local_auth_enabled() or external_auth_enabled()):
            return JSONResponse({"error": "Authentication is disabled"}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            auth_user = require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        user_id = str(auth_user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        if _external_session(request, auth_user):
            # The name is the person's in every account; a session pinned to one
            # account by external sign-in does not change it (ADR 0083, ADR 0085).
            return JSONResponse(
                {"error": EXTERNAL_PROFILE_REFUSED_MESSAGE, "code": EXTERNAL_PROFILE_REFUSED_CODE},
                status_code=403,
            )

        display_name = (
            str(body.display_name or "").strip()
            or str(auth_user.get("username") or "").strip()
            or "User"
        )
        try:
            user_profile_repository.update_user_display_name(bl, user_id, display_name)
        except Exception as e:
            return api_error_response(e)

        auth_user["display_name"] = display_name
        request_auth_user = getattr(request.state, "auth_user", None)
        if isinstance(request_auth_user, dict):
            request_auth_user["display_name"] = display_name

        log_auth_audit_event(
            bl,
            action="profile_update",
            username=str(auth_user.get("username") or ""),
            actor=str(auth_user.get("username") or "auth"),
            status="ok",
            detail={"display_name": display_name},
        )
        return {"status": "ok", "display_name": display_name}

    register_email_change_routes(
        router,
        local_auth_enabled=local_auth_enabled,
        get_logger_only=get_logger_only,
        auth_tables_available=auth_tables_available,
        require_authenticated_user=require_authenticated_user,
        api_error_response=api_error_response,
        log_auth_audit_event=log_auth_audit_event,
        auth_login_clear_user_failures=auth_login_clear_user_failures,
        get_user_by_username=get_user_by_username,
        real_dict_cursor_kwargs=real_dict_cursor_kwargs,
        err_authentication_required=err_authentication_required,
        err_database_not_configured=err_database_not_configured,
        err_auth_tables_not_available=err_auth_tables_not_available,
        err_local_auth_disabled=err_local_auth_disabled,
    )
    register_timezone_locale_routes(
        router,
        request_auth_user_payload=request_auth_user_payload,
        get_logger_only=get_logger_only,
        err_authentication_required=err_authentication_required,
        err_invalid_json_body=err_invalid_json_body,
        err_database_not_configured=err_database_not_configured,
    )

    @router.get("/api/auth/users")
    def api_auth_users(
        request: Request,
        page: Annotated[int, Query()] = 1,
        per_page: Annotated[int, Query()] = 25,
        search: Annotated[str, Query()] = "",
        role: Annotated[str, Query()] = "",
        active: Annotated[str, Query()] = "",
    ):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)
        managed_account_ids = _admin_managed_account_ids(admin_user)
        if not managed_account_ids:
            return _account_access_denied()

        normalized_search = str(search or "").strip()
        normalized_role = str(role or "").strip().lower()
        normalized_active = str(active or "").strip().lower()
        active_filter = None
        if normalized_active in {"1", "true", "active"}:
            active_filter = True
        elif normalized_active in {"0", "false", "inactive"}:
            active_filter = False

        # The page lists the people of the account the administrator acts in, with
        # their role there (ADR 0085); another managed account is listed by switching.
        acting_account_id = str(admin_user.get("account_id") or "").strip() or None
        if acting_account_id is not None and acting_account_id not in managed_account_ids:
            return _account_access_denied()

        try:
            normalized_page = max(1, int(page or 1))
            normalized_per_page = max(1, min(int(per_page or 25), 200))
            result = user_query_service.list_users(
                search=normalized_search,
                include_inactive=True,
                include_deleted=False,
                limit=normalized_per_page,
                offset=(normalized_page - 1) * normalized_per_page,
                role=normalized_role,
                active_filter=active_filter,
                managed_account_ids=managed_account_ids,
                role_account_id=acting_account_id,
                member_account_id=acting_account_id,
            )
        except Exception as e:
            return _user_service_error(e)

        users = [
            limit_view_to_accounts(
                user.model_dump(mode="json"),
                managed_account_ids,
                role_account_id=acting_account_id,
            )
            for user in result.users
        ]
        return {
            "users": users,
            "total": int(result.total),
            "page": normalized_page,
            "per_page": normalized_per_page,
            "total_pages": max(1, -(-int(result.total) // normalized_per_page)),
        }

    def _member_admin(request: Request) -> dict[str, Any] | JSONResponse:
        """The administrator of the acting account, or the refusal to answer."""
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            return require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

    @router.get("/api/auth/users/candidates")
    def api_auth_users_candidates(request: Request, q: Annotated[str, Query()] = ""):
        """People who may be added to the acting account (ADR 0085)."""
        admin_user = _member_admin(request)
        if isinstance(admin_user, JSONResponse):
            return admin_user
        try:
            people = account_member_service.candidates(
                query=q,
                account_id=str(admin_user.get("account_id") or ""),
                managed_account_ids=_admin_managed_account_ids(admin_user),
                admin_user_id=str(admin_user.get("id") or ""),
            )
        except Exception as e:
            return _user_service_error(e)
        return {"people": people}

    @router.post("/api/auth/users/{user_id}/membership")
    async def api_auth_users_membership_add(
        user_id: UUID, request: Request, body: AuthAccountMemberAddRequest
    ):
        """Add an existing person to the acting account with one role."""
        admin_user = _member_admin(request)
        if isinstance(admin_user, JSONResponse):
            return admin_user
        try:
            return account_member_service.add(
                user_id=str(user_id),
                role=body.role,
                account_id=str(admin_user.get("account_id") or ""),
                managed_account_ids=_admin_managed_account_ids(admin_user),
                actor=str(admin_user.get("username") or "admin"),
            )
        except Exception as e:
            return _user_service_error(e)

    @router.post("/api/auth/users/invitations")
    async def api_auth_users_invite(request: Request, body: AuthAccountInvitationRequest):
        """Invite a person to the acting account by their exact email (ADR 0085).

        The answer is the same whether or not the person exists.
        """
        admin_user = _member_admin(request)
        if isinstance(admin_user, JSONResponse):
            return admin_user
        try:
            return account_invitation_service.invite(
                email=body.email,
                role=body.role,
                account_id=str(admin_user.get("account_id") or ""),
                managed_account_ids=_admin_managed_account_ids(admin_user),
                admin_user_id=str(admin_user.get("id") or ""),
                actor=str(admin_user.get("username") or "admin"),
                link_base=_invitation_link_base(request),
                locale=resolve_ui_locale(request),
            )
        except Exception as e:
            return _user_service_error(e)

    @router.get("/api/auth/users/invitations")
    def api_auth_users_invitations(request: Request):
        """The acting account's open invitations, each email as the administrator typed it.

        Every invitation sent is listed, so the list never says whether a person exists.
        """
        admin_user = _member_admin(request)
        if isinstance(admin_user, JSONResponse):
            return admin_user
        try:
            invitations = account_invitation_service.sent(
                account_id=str(admin_user.get("account_id") or ""),
                managed_account_ids=_admin_managed_account_ids(admin_user),
            )
        except Exception as e:
            return _user_service_error(e)
        return {"invitations": invitations}

    @router.post("/api/auth/users/invitations/{invitation_id}/cancel")
    async def api_auth_users_invitation_cancel(invitation_id: UUID, request: Request):
        """Cancel an open invitation of the acting account."""
        admin_user = _member_admin(request)
        if isinstance(admin_user, JSONResponse):
            return admin_user
        try:
            return account_invitation_service.cancel(
                invitation_id=str(invitation_id),
                account_id=str(admin_user.get("account_id") or ""),
                managed_account_ids=_admin_managed_account_ids(admin_user),
                actor=str(admin_user.get("username") or "admin"),
            )
        except Exception as e:
            return _user_service_error(e)

    def _invitation_link_base(request: Request) -> str:
        """Where an invitation link points: the public URL, else this request's origin."""
        return public_base_url() or str(request.base_url or "").strip().rstrip("/")

    def _invited_person(request: Request) -> dict[str, Any] | JSONResponse:
        """The signed-in person who opened an invitation link (ADR 0085, C1).

        Any session that is the person counts: a password sign-in or an external
        sign-in, also one in an account where they have no role (P5). A personal access
        token, an OAuth grant or a service account never answers an invitation.
        """
        if not (local_auth_enabled() or external_auth_enabled()):
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        try:
            user = require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        if is_token_scoped_principal(user) or not str(user.get("id") or "").strip():
            return JSONResponse({"error": INVITATIONS_NEED_SESSION_MESSAGE}, status_code=403)
        return user

    @router.post("/api/auth/invitations/preview")
    async def api_auth_invitation_preview(request: Request, body: AuthInvitationTokenRequest):
        """What the opened link invites to: company, role, who invited and until when."""
        user = _invited_person(request)
        if isinstance(user, JSONResponse):
            return user
        try:
            return account_invitation_service.preview(token=body.token)
        except Exception as e:
            return _user_service_error(e)

    @router.post("/api/auth/invitations/accept")
    async def api_auth_invitation_accept(request: Request, body: AuthInvitationAcceptRequest):
        """Join the account of the opened link with the role the person was shown."""
        user = _invited_person(request)
        if isinstance(user, JSONResponse):
            return user
        try:
            return account_invitation_service.decide(
                token=body.token,
                user_id=str(user.get("id") or ""),
                username=str(user.get("username") or ""),
                accept=True,
                role=body.role,
            )
        except Exception as e:
            return _user_service_error(e)

    @router.post("/api/auth/invitations/decline")
    async def api_auth_invitation_decline(request: Request, body: AuthInvitationTokenRequest):
        """Decline the opened link's invitation; no membership is created."""
        user = _invited_person(request)
        if isinstance(user, JSONResponse):
            return user
        try:
            return account_invitation_service.decide(
                token=body.token,
                user_id=str(user.get("id") or ""),
                username=str(user.get("username") or ""),
                accept=False,
            )
        except Exception as e:
            return _user_service_error(e)

    @router.delete("/api/auth/users/{user_id}/membership")
    async def api_auth_users_membership_remove(user_id: UUID, request: Request):
        """Remove a person from the acting account; the person stays elsewhere."""
        admin_user = _member_admin(request)
        if isinstance(admin_user, JSONResponse):
            return admin_user
        try:
            return account_member_service.remove(
                user_id=str(user_id),
                account_id=str(admin_user.get("account_id") or ""),
                managed_account_ids=_admin_managed_account_ids(admin_user),
                actor_user_id=str(admin_user.get("id") or ""),
                actor=str(admin_user.get("username") or "admin"),
            )
        except Exception as e:
            return _user_service_error(e)

    @router.post("/api/auth/users")
    async def api_auth_users_create(request: Request, body: AuthUserCreateRequest):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        primary_account_id = (
            str(body.primary_account_id or "").strip()
            or str(admin_user.get("account_id") or "").strip()
            or DEFAULT_ACCOUNT_ID
        )
        managed_account_ids = _admin_managed_account_ids(admin_user)
        requested_account_ids = normalize_membership_account_ids(
            body.account_ids,
            fallback_account_id=primary_account_id,
        )
        if not _accounts_allowed(requested_account_ids, managed_account_ids):
            return _account_access_denied()
        try:
            command = UserCreateInput(
                email=body.email,
                password=body.password,
                display_name=body.display_name,
                # Without a role the server's default applies: viewer once users exist.
                role=body.requested_role() or "",
                is_active=bool(body.is_active),
                must_change_password=bool(body.must_change_password),
                account_id=primary_account_id,
                account_ids=requested_account_ids,
                actor=str(admin_user.get("username") or "admin"),
            )
        except ValidationError as exc:
            return _user_input_error(exc)
        try:
            result = user_command_service.create_user(
                command,
                allowed_account_ids=managed_account_ids,
                notification_locale=resolve_ui_locale(request),
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except ServiceConflictError as conflict:
            if conflict.message != EMAIL_ALREADY_EXISTS_MESSAGE:
                return _user_service_error(conflict)
            # ADR 0085, C2: an address someone already has is never confirmed. The
            # person is invited instead, and the answer is the invitation's own.
            try:
                return account_invitation_service.invite(
                    email=body.email,
                    role=body.requested_role() or "viewer",
                    account_id=primary_account_id,
                    managed_account_ids=managed_account_ids,
                    admin_user_id=str(admin_user.get("id") or ""),
                    actor=str(admin_user.get("username") or "admin"),
                    link_base=_invitation_link_base(request),
                    locale=resolve_ui_locale(request),
                )
            except Exception as e:
                return _user_service_error(e)
        except Exception as e:
            return _user_service_error(e)
        return {
            "user": limit_view_to_accounts(
                result.user.model_dump(mode="json"),
                managed_account_ids,
                role_account_id=primary_account_id,
            ),
            "warnings": list(result.warnings),
        }

    @router.put("/api/auth/users/{user_id}")
    async def api_auth_users_update(user_id: UUID, request: Request, body: AuthUserUpdateRequest):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        managed_account_ids = _admin_managed_account_ids(admin_user)

        try:
            command = UserUpdateInput(
                user_id=str(user_id),
                display_name=body.display_name,
                is_active=body.is_active,
                password=body.password,
                must_change_password=body.must_change_password,
                role=body.requested_role(),
                # A role is set in one account (ADR 0085): the named one, else the
                # account the administrator acts in.
                role_account_id=(
                    str(body.role_account_id or "").strip()
                    or str(admin_user.get("account_id") or "").strip()
                    or None
                )
                if body.requested_role() is not None
                else None,
                actor_user_id=str(admin_user.get("id") or "").strip() or None,
                account_ids=body.account_ids,
                account_id=body.primary_account_id,
                actor=str(admin_user.get("username") or "admin"),
            )
        except ValidationError as exc:
            return _user_input_error(exc)
        try:
            result = user_command_service.update_user(
                command,
                allowed_account_ids=managed_account_ids,
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except Exception as e:
            return _user_service_error(e)
        # The role shown is the person's role in the account the change named, or the
        # acting one: never a role of an account the administrator does not manage.
        return {
            "user": limit_view_to_accounts(
                result.user.model_dump(mode="json"),
                managed_account_ids,
                role_account_id=(
                    str(body.role_account_id or "").strip()
                    or str(admin_user.get("account_id") or "").strip()
                ),
            )
        }

    @router.post("/api/auth/users/{user_id}/unlock")
    async def api_auth_users_unlock(user_id: UUID, request: Request):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        managed_account_ids = _admin_managed_account_ids(admin_user)

        try:
            command = UserSelectorInput(
                user_id=str(user_id),
                actor=str(admin_user.get("username") or "admin"),
            )
        except ValidationError as exc:
            return _user_input_error(exc)
        try:
            result = user_command_service.unlock_user(
                command,
                allowed_account_ids=managed_account_ids,
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except Exception as e:
            return _user_service_error(e)
        return {
            "status": result.status,
            "user_id": str(result.user.id or user_id),
            "username": str(result.user.username or ""),
        }

    @router.delete("/api/auth/users/{user_id}")
    async def api_auth_users_delete(user_id: UUID, request: Request):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        try:
            command = UserDeleteInput(
                user_id=str(user_id),
                actor_user_id=str(admin_user.get("id") or ""),
                actor=str(admin_user.get("username") or "admin"),
            )
            result = user_command_service.delete_user(
                command,
                allowed_account_ids=_admin_managed_account_ids(admin_user),
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except (ValidationError, ValueError) as exc:
            return (
                _user_input_error(exc)
                if isinstance(exc, ValidationError)
                else JSONResponse({"error": str(exc)}, status_code=400)
            )
        except Exception as exc:
            return _user_service_error(exc)
        return result.model_dump(mode="json")

    @router.post("/api/auth/users/{user_id}/force-password-reset")
    async def api_auth_users_force_password_reset(user_id: UUID, request: Request):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        managed_account_ids = _admin_managed_account_ids(admin_user)
        try:
            command = UserSelectorInput(
                user_id=str(user_id),
                actor=str(admin_user.get("username") or "admin"),
            )
        except ValidationError as exc:
            return _user_input_error(exc)
        try:
            result = user_command_service.force_password_reset(
                command,
                allowed_account_ids=managed_account_ids,
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except Exception as e:
            return _user_service_error(e)
        return {
            "status": result.status,
            "user_id": str(result.user.id or user_id),
            "username": str(result.user.username or ""),
            "revoked_sessions": result.revoked_sessions,
        }

    @router.post("/api/auth/users/{user_id}/authenticator", response_model=UserAuthenticatorResult)
    def api_auth_users_authenticator(
        user_id: UUID, body: AuthUserAuthenticatorRequest, request: Request
    ):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)
        if (
            getattr(request.state, "auth_source", None) not in {"local", "external"}
            or is_token_scoped_principal(admin_user)
            or "authorization" in request.headers
        ):
            return JSONResponse({"error": err_admin_role_required}, status_code=403)
        if not csrf_request_valid(request):
            return JSONResponse({"error": "CSRF validation failed"}, status_code=403)

        try:
            return user_command_service.update_authenticator(
                user_id=str(user_id),
                actor_user_id=str(admin_user.get("id") or ""),
                actor=str(admin_user.get("username") or "admin"),
                action=body.action,
                allowed_account_ids=_admin_managed_account_ids(admin_user),
            )
        except Exception as exc:
            return _user_service_error(exc)

    return router
