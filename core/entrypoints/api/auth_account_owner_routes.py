"""Routes of a company's ownership (ADR 0085, "Company owner").

The owner of the acting company offers ownership to another of its active
administrators, who accepts or declines; the installation administrator reassigns the
owner of any company when the owner has lost access. Every rule is decided by
:class:`~core.application.auth.user_admin.account_ownership.AccountOwnershipService`;
these handlers only name the caller and the company.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.auth.installation_admin import (
    installation_admin_refusal,
    is_installation_admin,
)
from core.application.auth.principal import is_token_scoped_principal
from core.application.auth.user_admin.account_ownership import AccountOwnershipService

#: A transfer is offered and answered by a person in the browser, never by a token.
OWNERSHIP_NEEDS_SESSION_MESSAGE = "Sign in to the browser to transfer or accept ownership."


class AccountOwnerTransferRequest(BaseModel):
    """Offer ownership of the acting company to another of its administrators."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID


class AccountOwnerTransferAnswerRequest(BaseModel):
    """Accept or decline the open ownership offer made to the caller."""

    model_config = ConfigDict(extra="forbid")

    transfer_id: UUID


class AccountOwnerReassignRequest(BaseModel):
    """Installation administrator recovery: a new owner and the reason."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    reason: str = Field(min_length=1, max_length=500)


def register_account_owner_routes(
    router: APIRouter,
    *,
    member_admin: Callable[[Request], dict[str, Any] | JSONResponse],
    ownership_service: AccountOwnershipService,
    service_error: Callable[[Exception], JSONResponse],
) -> None:
    """Add the ownership routes; ``member_admin`` names the acting administrator."""

    def _person(request: Request) -> dict[str, Any] | JSONResponse:
        admin_user = member_admin(request)
        if isinstance(admin_user, JSONResponse):
            return admin_user
        if is_token_scoped_principal(admin_user) or not str(admin_user.get("id") or "").strip():
            return JSONResponse({"error": OWNERSHIP_NEEDS_SESSION_MESSAGE}, status_code=403)
        return admin_user

    def _account(user: dict[str, Any]) -> str:
        return str(user.get("account_id") or "").strip()

    def _actor(user: dict[str, Any]) -> str:
        return str(user.get("username") or "admin")

    @router.get("/api/auth/users/owner")
    def api_auth_account_owner(request: Request):
        """Who owns the acting company and the open ownership offer."""
        admin_user = member_admin(request)
        if isinstance(admin_user, JSONResponse):
            return admin_user
        try:
            return ownership_service.state(
                account_id=_account(admin_user), viewer_id=str(admin_user.get("id") or "")
            )
        except Exception as exc:
            return service_error(exc)

    @router.post("/api/auth/users/owner/transfer")
    async def api_auth_account_owner_transfer(request: Request, body: AccountOwnerTransferRequest):
        """The owner offers ownership to another active administrator of the company."""
        user = _person(request)
        if isinstance(user, JSONResponse):
            return user
        try:
            return ownership_service.offer(
                account_id=_account(user),
                owner_id=str(user.get("id") or ""),
                to_user_id=str(body.user_id),
                actor=_actor(user),
            )
        except Exception as exc:
            return service_error(exc)

    @router.post("/api/auth/users/owner/transfer/cancel")
    async def api_auth_account_owner_transfer_cancel(request: Request):
        """The owner withdraws the open offer."""
        user = _person(request)
        if isinstance(user, JSONResponse):
            return user
        try:
            return ownership_service.cancel(
                account_id=_account(user), owner_id=str(user.get("id") or ""), actor=_actor(user)
            )
        except Exception as exc:
            return service_error(exc)

    def _answer(request: Request, body: AccountOwnerTransferAnswerRequest, *, accept: bool):
        user = _person(request)
        if isinstance(user, JSONResponse):
            return user
        try:
            return ownership_service.decide(
                account_id=_account(user),
                user_id=str(user.get("id") or ""),
                transfer_id=str(body.transfer_id),
                accept=accept,
                actor=_actor(user),
            )
        except Exception as exc:
            return service_error(exc)

    @router.post("/api/auth/users/owner/transfer/accept")
    async def api_auth_account_owner_transfer_accept(
        request: Request, body: AccountOwnerTransferAnswerRequest
    ):
        """The recipient becomes the company's owner."""
        return _answer(request, body, accept=True)

    @router.post("/api/auth/users/owner/transfer/decline")
    async def api_auth_account_owner_transfer_decline(
        request: Request, body: AccountOwnerTransferAnswerRequest
    ):
        """The recipient declines; the owner stays."""
        return _answer(request, body, accept=False)

    @router.post("/api/auth/accounts/{account_id}/owner")
    async def api_auth_account_owner_reassign(
        account_id: UUID, request: Request, body: AccountOwnerReassignRequest
    ):
        """Installation administrator recovery: give a company a new owner, with a reason."""
        admin_user = member_admin(request)
        if isinstance(admin_user, JSONResponse):
            return admin_user
        if not is_installation_admin(admin_user):
            return JSONResponse(installation_admin_refusal(), status_code=403)
        try:
            return ownership_service.reassign(
                account_id=str(account_id),
                to_user_id=str(body.user_id),
                reason=body.reason,
                actor=_actor(admin_user),
            )
        except Exception as exc:
            return service_error(exc)


__all__ = [
    "AccountOwnerReassignRequest",
    "AccountOwnerTransferAnswerRequest",
    "AccountOwnerTransferRequest",
    "register_account_owner_routes",
]
