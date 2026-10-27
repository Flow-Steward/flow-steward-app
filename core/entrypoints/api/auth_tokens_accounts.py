"""Auth token/account/service-account routes extracted from serve.py."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import (
    TokenAuthorityError,
    enforce_token_scope_within_caller_authority,
)
from core.application.management.requests import (
    AuthAccountCreateRequest,
    AuthAccountDisableRequest,
    AuthCurrentAccountUpdateRequest,
    AuthMachineTokenCreateRequest,
    AuthMachineTokenUpdateRequest,
    AuthServiceAccountCreateRequest,
    AuthServiceAccountUpdateRequest,
)
from core.entrypoints.api.auth_personal_token_routes import (
    CURRENT_ACCOUNT_NOT_FOUND_MESSAGE,
    PersonalTokenRouteDependencies,
    register_personal_token_routes,
)

_MESSAGE_SERVICE_ACCOUNT_TABLES_ARE_NOT_AVAILABLE = "Service account tables are not available"


def create_auth_tokens_accounts_router(
    *,
    local_auth_enabled: Callable[[], bool],
    get_logger_only: Callable[[], Any],
    auth_tables_available: Callable[[Any], bool],
    auth_pat_tables_available: Callable[[Any], bool],
    auth_account_tables_available: Callable[[Any], bool],
    auth_service_account_tables_available: Callable[[Any], bool],
    auth_machine_token_tables_available: Callable[[Any], bool],
    require_authenticated_user: Callable[..., dict],
    api_error_response: Callable[[Exception], JSONResponse],
    log_auth_audit_event: Callable[..., None],
    auth_admin_roles: list[str] | tuple[str, ...] | set[str],
    err_local_auth_disabled: str,
    err_database_not_configured: str,
    err_auth_tables_not_available: str,
    err_pat_tables_not_available: str,
    err_authentication_required: str,
    err_token_not_found: str,
    err_account_tables_not_available: str,
    err_admin_role_required: str,
    err_service_account_key_required: str,
    err_machine_token_tables_not_available: str,
) -> APIRouter:
    router = APIRouter(tags=["auth-tokens-accounts"])

    _local_auth_enabled = local_auth_enabled
    _get_logger_only = get_logger_only
    _auth_tables_available = auth_tables_available
    _auth_pat_tables_available = auth_pat_tables_available
    _auth_account_tables_available = auth_account_tables_available
    _auth_service_account_tables_available = auth_service_account_tables_available
    _auth_machine_token_tables_available = auth_machine_token_tables_available
    _require_authenticated_user = require_authenticated_user
    _api_error_response = api_error_response
    _log_auth_audit_event = log_auth_audit_event

    _AUTH_ADMIN_ROLES = auth_admin_roles
    ERR_LOCAL_AUTH_DISABLED = err_local_auth_disabled
    ERR_DATABASE_NOT_CONFIGURED = err_database_not_configured
    ERR_AUTH_TABLES_NOT_AVAILABLE = err_auth_tables_not_available
    ERR_PAT_TABLES_NOT_AVAILABLE = err_pat_tables_not_available
    ERR_AUTHENTICATION_REQUIRED = err_authentication_required
    ERR_TOKEN_NOT_FOUND = err_token_not_found
    ERR_ACCOUNT_TABLES_NOT_AVAILABLE = err_account_tables_not_available
    ERR_ADMIN_ROLE_REQUIRED = err_admin_role_required
    ERR_SERVICE_ACCOUNT_KEY_REQUIRED = err_service_account_key_required
    ERR_MACHINE_TOKEN_TABLES_NOT_AVAILABLE = err_machine_token_tables_not_available

    def _resolve_current_account_payload(
        auth_user: dict[str, Any], bl: Any
    ) -> dict[str, Any] | None:
        current_account_id = str(auth_user.get("account_id") or "").strip()
        if not current_account_id:
            return None
        memberships = [
            dict(item or {})
            for item in (auth_user.get("account_memberships") or [])
            if isinstance(item, dict)
        ]
        membership = next(
            (
                item
                for item in memberships
                if str(item.get("account_id") or "").strip() == current_account_id
            ),
            None,
        )
        account = bl.get_account(current_account_id) if hasattr(bl, "get_account") else None
        if isinstance(account, dict):
            return account
        return {
            "account_id": current_account_id,
            "display_name": str(
                (membership or {}).get("display_name") or current_account_id
            ).strip()
            or current_account_id,
            "is_active": bool((membership or {}).get("is_active", True)),
        }

    def _account_id_for_admin(auth_user: dict[str, Any]) -> str:
        return str(auth_user.get("account_id") or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID

    register_personal_token_routes(
        router,
        PersonalTokenRouteDependencies(
            local_auth_enabled=_local_auth_enabled,
            get_logger_only=_get_logger_only,
            auth_tables_available=_auth_tables_available,
            auth_pat_tables_available=_auth_pat_tables_available,
            require_authenticated_user=_require_authenticated_user,
            api_error_response=_api_error_response,
            log_auth_audit_event=_log_auth_audit_event,
            err_local_auth_disabled=ERR_LOCAL_AUTH_DISABLED,
            err_database_not_configured=ERR_DATABASE_NOT_CONFIGURED,
            err_auth_tables_not_available=ERR_AUTH_TABLES_NOT_AVAILABLE,
            err_pat_tables_not_available=ERR_PAT_TABLES_NOT_AVAILABLE,
            err_authentication_required=ERR_AUTHENTICATION_REQUIRED,
            err_token_not_found=ERR_TOKEN_NOT_FOUND,
        ),
    )

    @router.get("/api/auth/accounts")
    async def api_auth_accounts_list(
        request: Request,
        include_inactive: Annotated[bool, Query()] = True,
        search: Annotated[str, Query()] = "",
        limit: Annotated[int, Query()] = 200,
        offset: Annotated[int, Query()] = 0,
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_account_tables_available(bl):
            return JSONResponse({"error": ERR_ACCOUNT_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        try:
            accounts = (
                bl.list_accounts(
                    include_inactive=bool(include_inactive),
                    search=str(search or "").strip(),
                    limit=max(1, min(int(limit or 200), 2000)),
                    offset=max(0, int(offset or 0)),
                )
                if hasattr(bl, "list_accounts")
                else []
            )
        except Exception as e:
            return _api_error_response(e)
        return {"accounts": list(accounts or [])}

    @router.get("/api/auth/accounts/current")
    async def api_auth_accounts_current(request: Request):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_account_tables_available(bl):
            return JSONResponse({"error": ERR_ACCOUNT_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            auth_user = _require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status_code=401)

        account = _resolve_current_account_payload(auth_user, bl)
        if not account:
            return JSONResponse({"error": CURRENT_ACCOUNT_NOT_FOUND_MESSAGE}, status_code=404)
        return {"account": account}

    @router.patch("/api/auth/accounts/current")
    async def api_auth_accounts_current_update(
        request: Request,
        body: AuthCurrentAccountUpdateRequest,
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_account_tables_available(bl):
            return JSONResponse({"error": ERR_ACCOUNT_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            auth_user = _require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status_code=401)

        current_account_id = str(auth_user.get("account_id") or "").strip()
        if not current_account_id:
            return JSONResponse({"error": CURRENT_ACCOUNT_NOT_FOUND_MESSAGE}, status_code=404)

        memberships = [
            dict(item or {})
            for item in (auth_user.get("account_memberships") or [])
            if isinstance(item, dict)
        ]
        if memberships and not any(
            str(item.get("account_id") or "").strip() == current_account_id for item in memberships
        ):
            return JSONResponse(
                {"error": "Current account is not available for this user"}, status_code=403
            )

        try:
            account = (
                bl.update_account(
                    current_account_id,
                    display_name=str(body.display_name or "").strip(),
                )
                if hasattr(bl, "update_account")
                else None
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return _api_error_response(e)

        if not account:
            return JSONResponse({"error": CURRENT_ACCOUNT_NOT_FOUND_MESSAGE}, status_code=404)

        updated_display_name = str(account.get("display_name") or current_account_id).strip()
        for item in memberships:
            if str(item.get("account_id") or "").strip() == current_account_id:
                item["display_name"] = updated_display_name
        auth_user["account_memberships"] = memberships

        _log_auth_audit_event(
            bl,
            action="account_update",
            username=current_account_id,
            actor=str(auth_user.get("username") or "auth"),
            status="ok",
            detail={
                "account_id": current_account_id,
                "display_name": updated_display_name,
            },
        )
        return {"status": "ok", "account": account}

    @router.post("/api/auth/accounts")
    async def api_auth_accounts_create(request: Request, body: AuthAccountCreateRequest):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_account_tables_available(bl):
            return JSONResponse({"error": ERR_ACCOUNT_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        normalized_account_id = str(body.account_id or "").strip()
        if not normalized_account_id:
            return JSONResponse({"error": "account_id is required"}, status_code=400)
        try:
            account = (
                bl.create_account(
                    account_id=normalized_account_id,
                    display_name=str(body.display_name or "").strip(),
                    is_active=bool(body.is_active),
                )
                if hasattr(bl, "create_account")
                else None
            )
        except ValueError as e:
            message = str(e)
            status = 409 if "already exists" in message.lower() else 400
            return JSONResponse({"error": message}, status_code=status)
        except Exception as e:
            return _api_error_response(e)

        if not account:
            return JSONResponse({"error": "Failed to create account"}, status_code=500)

        _log_auth_audit_event(
            bl,
            action="account_create",
            username=normalized_account_id,
            actor=str(admin_user.get("username") or "admin"),
            status="ok",
            detail={
                "account_id": str(account.get("account_id") or normalized_account_id),
                "is_active": bool(account.get("is_active", True)),
            },
        )
        return {"account": account}

    @router.post("/api/auth/accounts/{account_id}/disable")
    async def api_auth_accounts_disable(
        account_id: str, request: Request, body: AuthAccountDisableRequest | None = None
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_account_tables_available(bl):
            return JSONResponse({"error": ERR_ACCOUNT_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            return JSONResponse({"error": "account_id is required"}, status_code=400)
        reason = str((body.reason if body is not None else "") or "").strip()
        try:
            account = (
                bl.set_account_active(
                    normalized_account_id,
                    is_active=False,
                )
                if hasattr(bl, "set_account_active")
                else None
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return _api_error_response(e)

        if not account:
            return JSONResponse({"error": "Account not found"}, status_code=404)

        _log_auth_audit_event(
            bl,
            action="account_disable",
            username=normalized_account_id,
            actor=str(admin_user.get("username") or "admin"),
            status="ok",
            detail={
                "account_id": str(account.get("account_id") or normalized_account_id),
                "reason": reason,
            },
        )
        return {"status": "disabled", "account": account}

    @router.get("/api/auth/service-accounts")
    async def api_auth_service_accounts_list(request: Request):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_service_account_tables_available(bl):
            return JSONResponse(
                {"error": _MESSAGE_SERVICE_ACCOUNT_TABLES_ARE_NOT_AVAILABLE}, status_code=503
            )
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        try:
            service_accounts = bl.list_service_accounts(
                account_id=_account_id_for_admin(admin_user),
                include_inactive=True,
            )
        except Exception as e:
            return _api_error_response(e)
        return {"service_accounts": service_accounts}

    @router.post("/api/auth/service-accounts")
    async def api_auth_service_accounts_create(
        request: Request, body: AuthServiceAccountCreateRequest
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_service_account_tables_available(bl):
            return JSONResponse(
                {"error": _MESSAGE_SERVICE_ACCOUNT_TABLES_ARE_NOT_AVAILABLE}, status_code=503
            )
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        service_account_key = str(body.service_account_key or "").strip().lower()
        if not service_account_key:
            return JSONResponse({"error": ERR_SERVICE_ACCOUNT_KEY_REQUIRED}, status_code=400)

        try:
            enforce_token_scope_within_caller_authority(
                admin_user,
                requested_scopes=list(body.scopes or []),
                requested_project_ids=list(body.allowed_project_ids or []),
                requested_workflow_ids=list(body.allowed_workflow_ids or []),
            )
        except TokenAuthorityError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status_code)

        try:
            service_account = bl.create_service_account(
                service_account_key=service_account_key,
                display_name=str(body.display_name or "").strip(),
                description=str(body.description or "").strip(),
                scopes=list(body.scopes or []),
                allowed_project_ids=list(body.allowed_project_ids or []),
                allowed_workflow_ids=list(body.allowed_workflow_ids or []),
                account_id=_account_id_for_admin(admin_user),
                created_by=str(admin_user.get("username") or "admin"),
                is_active=bool(body.is_active),
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return _api_error_response(e)
        if not service_account:
            return JSONResponse({"error": "Failed to create service account"}, status_code=500)

        _log_auth_audit_event(
            bl,
            action="service_account_create",
            username=service_account_key,
            actor=str(admin_user.get("username") or "admin"),
            status="ok",
            detail={
                "service_account_key": service_account_key,
                "is_active": bool(service_account.get("is_active", True)),
                "scopes": list(service_account.get("scopes") or []),
            },
        )
        return {"service_account": service_account}

    @router.patch("/api/auth/service-accounts/{service_account_key}")
    async def api_auth_service_accounts_update(
        service_account_key: str,
        request: Request,
        body: AuthServiceAccountUpdateRequest,
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_service_account_tables_available(bl):
            return JSONResponse(
                {"error": _MESSAGE_SERVICE_ACCOUNT_TABLES_ARE_NOT_AVAILABLE}, status_code=503
            )
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            return JSONResponse({"error": ERR_SERVICE_ACCOUNT_KEY_REQUIRED}, status_code=400)

        try:
            enforce_token_scope_within_caller_authority(
                admin_user,
                requested_scopes=list(body.scopes or []),
                requested_project_ids=list(body.allowed_project_ids or []),
                requested_workflow_ids=list(body.allowed_workflow_ids or []),
            )
        except TokenAuthorityError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status_code)

        try:
            service_account = bl.update_service_account(
                normalized_key,
                display_name=str(body.display_name or "").strip(),
                description=str(body.description or "").strip(),
                scopes=list(body.scopes or []),
                allowed_project_ids=list(body.allowed_project_ids or []),
                allowed_workflow_ids=list(body.allowed_workflow_ids or []),
                account_id=_account_id_for_admin(admin_user),
                is_active=bool(body.is_active),
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return _api_error_response(e)
        if not service_account:
            return JSONResponse({"error": "Service account not found"}, status_code=404)

        _log_auth_audit_event(
            bl,
            action="service_account_update",
            username=normalized_key,
            actor=str(admin_user.get("username") or "admin"),
            status="ok",
            detail={
                "service_account_key": normalized_key,
                "is_active": bool(service_account.get("is_active", True)),
                "scopes": list(service_account.get("scopes") or []),
            },
        )
        return {"service_account": service_account}

    @router.delete("/api/auth/service-accounts/{service_account_key}")
    async def api_auth_service_accounts_delete(service_account_key: str, request: Request):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_service_account_tables_available(bl):
            return JSONResponse(
                {"error": _MESSAGE_SERVICE_ACCOUNT_TABLES_ARE_NOT_AVAILABLE}, status_code=503
            )
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            return JSONResponse({"error": ERR_SERVICE_ACCOUNT_KEY_REQUIRED}, status_code=400)

        try:
            service_account = bl.delete_service_account(
                normalized_key,
                account_id=_account_id_for_admin(admin_user),
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return _api_error_response(e)
        if not service_account:
            return JSONResponse({"error": "Service account not found"}, status_code=404)

        _log_auth_audit_event(
            bl,
            action="service_account_delete",
            username=normalized_key,
            actor=str(admin_user.get("username") or "admin"),
            status="ok",
            detail={"service_account_key": normalized_key},
        )
        return {"service_account": service_account}

    @router.get("/api/auth/service-accounts/{service_account_key}/tokens")
    async def api_auth_service_account_tokens_list(service_account_key: str, request: Request):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_machine_token_tables_available(bl):
            return JSONResponse({"error": ERR_MACHINE_TOKEN_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            return JSONResponse({"error": ERR_SERVICE_ACCOUNT_KEY_REQUIRED}, status_code=400)

        try:
            tokens = bl.list_service_account_tokens(
                normalized_key,
                account_id=_account_id_for_admin(admin_user),
                include_revoked=True,
            )
        except Exception as e:
            return _api_error_response(e)
        return {"tokens": tokens}

    @router.post("/api/auth/service-accounts/{service_account_key}/tokens")
    async def api_auth_service_account_tokens_create(
        service_account_key: str,
        request: Request,
        body: AuthMachineTokenCreateRequest,
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_machine_token_tables_available(bl):
            return JSONResponse({"error": ERR_MACHINE_TOKEN_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        expires_in_days = body.expires_in_days
        if expires_in_days is not None and int(expires_in_days) <= 0:
            return JSONResponse(
                {"error": "expires_in_days must be greater than 0"}, status_code=400
            )

        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            return JSONResponse({"error": ERR_SERVICE_ACCOUNT_KEY_REQUIRED}, status_code=400)
        label = str(body.label or body.name or "").strip()

        try:
            enforce_token_scope_within_caller_authority(
                admin_user,
                requested_scopes=list(body.scopes or []),
                requested_project_ids=list(body.allowed_project_ids or []),
                requested_workflow_ids=list(body.allowed_workflow_ids or []),
            )
        except TokenAuthorityError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status_code)

        try:
            token = bl.create_machine_token(
                service_account_key=normalized_key,
                label=label,
                scopes=list(body.scopes or []),
                allowed_project_ids=list(body.allowed_project_ids or []),
                allowed_workflow_ids=list(body.allowed_workflow_ids or []),
                expires_in_days=int(expires_in_days) if expires_in_days is not None else None,
                account_id=_account_id_for_admin(admin_user),
                created_by=str(admin_user.get("username") or "admin"),
            )
        except ValueError as e:
            message = str(e)
            status = 404 if message.lower().startswith("unknown service account") else 400
            return JSONResponse({"error": message}, status_code=status)
        except Exception as e:
            return _api_error_response(e)

        _log_auth_audit_event(
            bl,
            action="machine_token_create",
            username=normalized_key,
            actor=str(admin_user.get("username") or "admin"),
            status="ok",
            detail={
                "service_account_key": normalized_key,
                "token_id": str(token.get("id") or "").strip(),
                "label": str(token.get("label") or ""),
                "expires_at": token.get("expires_at"),
            },
        )
        return {
            "id": str(token.get("id") or "").strip(),
            "token_id": str(token.get("id") or "").strip(),
            "token": token,
        }

    @router.patch("/api/auth/service-accounts/{service_account_key}/tokens/{token_id}")
    async def api_auth_service_account_tokens_update(
        service_account_key: str,
        token_id: str,
        request: Request,
        body: AuthMachineTokenUpdateRequest,
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_machine_token_tables_available(bl):
            return JSONResponse({"error": ERR_MACHINE_TOKEN_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            return JSONResponse({"error": ERR_SERVICE_ACCOUNT_KEY_REQUIRED}, status_code=400)

        try:
            admin_account_id = _account_id_for_admin(admin_user)
            existing_tokens = bl.list_service_account_tokens(
                normalized_key,
                account_id=admin_account_id,
                include_revoked=True,
            )
        except Exception as e:
            return _api_error_response(e)

        existing = next(
            (item for item in existing_tokens if str(item.get("id") or "") == str(token_id)),
            None,
        )
        if not existing:
            return JSONResponse({"error": ERR_TOKEN_NOT_FOUND}, status_code=404)
        if bool(existing.get("revoked", False)):
            return JSONResponse({"error": "Cannot update a revoked machine token"}, status_code=400)

        scopes = list(existing.get("scopes") or []) if body.scopes is None else list(body.scopes)
        allowed_project_ids = (
            list(existing.get("allowed_project_ids") or [])
            if body.allowed_project_ids is None
            else list(body.allowed_project_ids)
        )
        allowed_workflow_ids = (
            list(existing.get("allowed_workflow_ids") or [])
            if body.allowed_workflow_ids is None
            else list(body.allowed_workflow_ids)
        )
        try:
            enforce_token_scope_within_caller_authority(
                admin_user,
                requested_scopes=scopes,
                requested_project_ids=allowed_project_ids,
                requested_workflow_ids=allowed_workflow_ids,
            )
        except TokenAuthorityError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status_code)

        label = (
            str(body.label or body.name or existing.get("label") or "").strip()
            or f"{normalized_key}-token"
        )

        try:
            updated = bl.update_machine_token(
                str(token_id),
                label=label,
                scopes=None if body.scopes is None else scopes,
                allowed_project_ids=(
                    None if body.allowed_project_ids is None else allowed_project_ids
                ),
                allowed_workflow_ids=(
                    None if body.allowed_workflow_ids is None else allowed_workflow_ids
                ),
                account_id=admin_account_id,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return _api_error_response(e)
        if not updated:
            return JSONResponse({"error": ERR_TOKEN_NOT_FOUND}, status_code=404)

        _log_auth_audit_event(
            bl,
            action="machine_token_update",
            username=normalized_key,
            actor=str(admin_user.get("username") or "admin"),
            status="ok",
            detail={
                "service_account_key": normalized_key,
                "token_id": str(updated.get("id") or token_id),
                "label": str(updated.get("label") or ""),
                "scopes": list(updated.get("scopes") or []),
            },
        )
        return {
            "status": "updated",
            "id": str(updated.get("id") or token_id),
            "token": updated,
        }

    @router.post("/api/auth/service-accounts/{service_account_key}/tokens/{token_id}/revoke")
    async def api_auth_service_account_tokens_revoke(
        service_account_key: str, token_id: str, request: Request
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_machine_token_tables_available(bl):
            return JSONResponse({"error": ERR_MACHINE_TOKEN_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            return JSONResponse({"error": ERR_SERVICE_ACCOUNT_KEY_REQUIRED}, status_code=400)

        try:
            admin_account_id = _account_id_for_admin(admin_user)
            existing_tokens = bl.list_service_account_tokens(
                normalized_key,
                account_id=admin_account_id,
                include_revoked=True,
            )
        except Exception as e:
            return _api_error_response(e)

        existing = next(
            (item for item in existing_tokens if str(item.get("id") or "") == str(token_id)),
            None,
        )
        if not existing:
            return JSONResponse({"error": ERR_TOKEN_NOT_FOUND}, status_code=404)

        try:
            revoked = bl.revoke_machine_token(str(token_id), account_id=admin_account_id)
        except Exception as e:
            return _api_error_response(e)
        if not revoked:
            return JSONResponse({"error": ERR_TOKEN_NOT_FOUND}, status_code=404)

        _log_auth_audit_event(
            bl,
            action="machine_token_revoke",
            username=normalized_key,
            actor=str(admin_user.get("username") or "admin"),
            status="ok",
            detail={
                "service_account_key": normalized_key,
                "token_id": str(revoked.get("id") or token_id),
            },
        )
        return {
            "status": "revoked",
            "id": str(revoked.get("id") or token_id),
            "token": revoked,
        }

    @router.delete("/api/auth/service-accounts/{service_account_key}/tokens/{token_id}")
    async def api_auth_service_account_tokens_delete(
        service_account_key: str, token_id: str, request: Request
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_machine_token_tables_available(bl):
            return JSONResponse({"error": ERR_MACHINE_TOKEN_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            admin_user = _require_authenticated_user(request, required_roles=_AUTH_ADMIN_ROLES)
        except PermissionError:
            return JSONResponse({"error": ERR_ADMIN_ROLE_REQUIRED}, status_code=403)

        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            return JSONResponse({"error": ERR_SERVICE_ACCOUNT_KEY_REQUIRED}, status_code=400)

        try:
            admin_account_id = _account_id_for_admin(admin_user)
            existing_tokens = bl.list_service_account_tokens(
                normalized_key,
                account_id=admin_account_id,
                include_revoked=True,
            )
        except Exception as e:
            return _api_error_response(e)

        existing = next(
            (item for item in existing_tokens if str(item.get("id") or "") == str(token_id)),
            None,
        )
        if not existing:
            return JSONResponse({"error": ERR_TOKEN_NOT_FOUND}, status_code=404)

        try:
            deleted = bl.delete_machine_token(str(token_id), account_id=admin_account_id)
        except Exception as e:
            return _api_error_response(e)
        if not deleted:
            return JSONResponse({"error": ERR_TOKEN_NOT_FOUND}, status_code=404)

        _log_auth_audit_event(
            bl,
            action="machine_token_delete",
            username=normalized_key,
            actor=str(admin_user.get("username") or "admin"),
            status="ok",
            detail={
                "service_account_key": normalized_key,
                "token_id": str(deleted.get("id") or token_id),
            },
        )
        return {
            "status": "deleted",
            "id": str(deleted.get("id") or token_id),
            "token": deleted,
        }

    return router
