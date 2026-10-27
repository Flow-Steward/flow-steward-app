"""Read service for local-auth user administration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeAlias

from core.application.auth.account_memberships import get_user_membership_map
from core.application.auth.resolve import (
    auth_tables_available,
    get_user_by_id,
    get_user_by_username,
)
from core.application.auth.user_admin import repository as user_admin_repository
from core.application.auth.user_admin.contracts import (
    AccountChoiceView,
    AccountListView,
    PublicUserView,
    UserListView,
)
from core.application.auth.user_admin.policies import resolve_user
from core.application.auth.user_admin.projection import normalize_user_payload
from core.application.management.errors import ServiceUnavailableError

LoggerFactory: TypeAlias = Callable[[], Any]
AuthTablesAvailable: TypeAlias = Callable[[Any], bool]
GetUserById: TypeAlias = Callable[[Any, Any], dict[str, Any] | None]
GetUserByUsername: TypeAlias = Callable[[Any, str], dict[str, Any] | None]


class UserAdminQueryRepository(Protocol):
    def list_auth_users(
        self,
        bl: Any,
        *,
        search: str,
        include_inactive: bool,
        include_deleted: bool,
        limit: int,
        offset: int,
        role: str,
        active_filter: bool | None,
        managed_account_ids: list[str] | None,
    ) -> tuple[list[dict[str, Any]], int, int, int]: ...


class UserAdminQueryService:
    def __init__(
        self,
        *,
        get_logger: LoggerFactory,
        auth_tables_available_fn: AuthTablesAvailable | None = None,
        get_user_by_id_fn: GetUserById | None = None,
        get_user_by_username_fn: GetUserByUsername | None = None,
        repository: UserAdminQueryRepository | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._auth_tables_available = auth_tables_available_fn or auth_tables_available
        self._get_user_by_id = get_user_by_id_fn or get_user_by_id
        self._get_user_by_username = get_user_by_username_fn or get_user_by_username
        self._repository: UserAdminQueryRepository = repository or user_admin_repository

    def _logger(self) -> Any:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        if not self._auth_tables_available(bl):
            raise ServiceUnavailableError("Auth tables are unavailable in this database schema")
        return bl

    def get_user(self, *, user_id: str | None = None, email: str = "") -> PublicUserView:
        bl = self._logger()
        payload = resolve_user(
            bl,
            user_id=user_id,
            email=email,
            get_user_by_id_fn=self._get_user_by_id,
            get_user_by_username_fn=self._get_user_by_username,
        )
        return PublicUserView.model_validate(normalize_user_payload(payload))

    def list_users(
        self,
        *,
        search: str = "",
        include_inactive: bool = False,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
        role: str = "",
        active_filter: bool | None = None,
        managed_account_ids: list[str] | None = None,
    ) -> UserListView:
        bl = self._logger()
        rows, total, normalized_limit, normalized_offset = self._repository.list_auth_users(
            bl,
            search=search,
            include_inactive=include_inactive,
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
            role=role,
            active_filter=active_filter,
            managed_account_ids=managed_account_ids,
        )
        membership_map = get_user_membership_map(
            bl,
            [str(row.get("id") or "").strip() for row in rows if str(row.get("id") or "").strip()],
        )
        return UserListView(
            users=[
                PublicUserView.model_validate(
                    normalize_user_payload(row, membership_map=membership_map)
                )
                for row in rows
            ],
            total=total,
            limit=normalized_limit,
            offset=normalized_offset,
            has_more=normalized_offset + len(rows) < total,
        )

    def list_accounts(self, *, limit: int = 200) -> AccountListView:
        bl = self._logger()
        rows = bl.list_accounts(
            include_inactive=False,
            search="",
            limit=max(1, min(int(limit), 200)),
            offset=0,
        )
        accounts = [
            AccountChoiceView(
                id=account_id,
                display_name=str(row.get("display_name") or account_id),
                is_active=bool(row.get("is_active", True)),
            )
            for row in rows
            if (account_id := str(row.get("account_id") or row.get("id") or "").strip())
            and bool(row.get("is_active", True))
        ]
        return AccountListView(accounts=accounts, total=len(accounts))
