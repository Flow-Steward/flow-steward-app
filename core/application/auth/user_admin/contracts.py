"""Typed inputs and closed public outputs for user administration."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from core.application.auth.email import normalize_email_address
from core.application.auth.permissions import SYSTEM_ROLE_LEVELS
from core.application.management.query_models import ViewModel

#: The message of a role that is not one of the system roles.
_ROLE_CHOICES_TEXT = "admin, operator or viewer"


def _system_role(value: str) -> str:
    """One system role, normalized (ADR 0076 §7); an unknown one is refused."""
    role = str(value or "").strip().lower()
    if role not in SYSTEM_ROLE_LEVELS:
        raise ValueError(f"role must be one of {_ROLE_CHOICES_TEXT}")
    return role


class _ExistingUserInput(ViewModel):
    user_id: str | None = None
    email: str = ""
    actor: str = "cli"

    @model_validator(mode="after")
    def validate_selector(self) -> Self:
        user_id = str(self.user_id or "").strip()
        email = str(self.email or "").strip()
        if bool(user_id) == bool(email):
            raise ValueError("exactly one of user_id or email is required")
        self.user_id = user_id or None
        self.email = email
        return self


class UserSelectorInput(_ExistingUserInput):
    pass


class UserDeleteInput(_ExistingUserInput):
    actor_user_id: str

    @field_validator("actor_user_id")
    @classmethod
    def validate_actor_user_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("actor_user_id is required")
        return normalized


class UserCreateInput(ViewModel):
    email: str
    password: str
    display_name: str = ""
    #: One system role (ADR 0076 §7). Empty means the default: admin for the first
    #: user of an installation, viewer for everyone after.
    role: str = ""
    is_active: bool = True
    must_change_password: bool = False
    account_id: str = ""
    account_ids: list[str] = Field(default_factory=list)
    actor: str = "cli"

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        try:
            return normalize_email_address(value, field_name="email")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        return _system_role(value) if str(value or "").strip() else ""


class UserPasswordUpdateInput(_ExistingUserInput):
    password: str
    must_change_password: bool | None = None
    revoke_sessions: bool = False


class UserActiveUpdateInput(_ExistingUserInput):
    is_active: bool


class UserRolesUpdateInput(_ExistingUserInput):
    #: The user's one system role (ADR 0076 §7).
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        return _system_role(value)


class UserUpdateInput(_ExistingUserInput):
    display_name: str | None = None
    password: str | None = None
    is_active: bool | None = None
    must_change_password: bool | None = None
    #: The user's one system role (ADR 0076 §7); ``None`` keeps it.
    role: str | None = None
    account_id: str | None = None
    account_ids: list[str] | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str | None) -> str | None:
        return None if value is None else _system_role(value)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("display_name is required")
        return normalized


class UserAuditContext(ViewModel):
    actor: str
    source: str


UserAuthenticatorAction = Literal["require", "unrequire", "reset"]


class UserAuthenticatorResult(ViewModel):
    status: str
    authenticator_required: bool
    revoked_sessions: int


class PublicUserView(ViewModel):
    """Allowlisted user projection safe for every management transport."""

    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    id: str
    username: str = ""
    display_name: str = ""
    is_active: bool = True
    must_change_password: bool = False
    authenticator_required: bool = False
    roles: list[str] = Field(default_factory=list)
    account_id: str = ""
    account_ids: list[str] = Field(default_factory=list)
    account_memberships: list[dict[str, Any]] = Field(default_factory=list)
    account_display_name: str = ""
    deleted_at: Any = None
    email: str = ""
    created_at: Any = None
    updated_at: Any = None


class UserListView(ViewModel):
    users: list[PublicUserView] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0
    has_more: bool = False


class UserMutationResultView(ViewModel):
    status: str
    user: PublicUserView
    revoked_sessions: int = 0
    warnings: list[str] = Field(default_factory=list)


class UserDeleteResultView(ViewModel):
    status: str = "deleted"
    user_id: str
    username: str
    revoked_sessions: int = 0


class AccountChoiceView(ViewModel):
    id: str
    display_name: str
    is_active: bool = True


class AccountListView(ViewModel):
    accounts: list[AccountChoiceView] = Field(default_factory=list)
    total: int = 0
