"""User-domain public command specifications."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.auth.bootstrap_tokens import (
    BootstrapTokenIssueInput,
    BootstrapTokenIssueResult,
)
from core.application.auth.user_admin.contracts import (
    UserActiveUpdateInput,
    UserCreateInput,
    UserListView,
    UserMutationResultView,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
    UserSelectorInput,
)
from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    ExactlyOneOfGroup,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)

_TOKEN_USERS_LIST = "users.list"
_TOKEN_USERS_CREATE = "users.create"
_TOKEN_USERS_SET_PASSWORD = "users.set-password"  # pragma: allowlist secret - command name
_TOKEN_USERS_FORCE_RESET = "users.force-reset"
_TOKEN_USERS_UNLOCK = "users.unlock"
_TOKEN_USERS_SET_ACTIVE = "users.set-active"
_TOKEN_USERS_SET_ROLES = "users.set-roles"
_TOKEN_USERS_ISSUE_BOOTSTRAP_TOKEN = "users.issue-bootstrap-token"

ROLE_CHOICES = (
    ChoiceItem(value="admin", label="Admin"),
    ChoiceItem(value="operator", label="Operator"),
    ChoiceItem(value="viewer", label="Viewer"),
)

USER_TARGET_GROUP = ExactlyOneOfGroup(
    key="target",
    label="Target",
    field_keys=("user_id", "email"),
)


def _user_selector_fields() -> tuple[FieldSpec, ...]:
    return (
        FieldSpec(
            key="user_id",
            label="User ID",
            type=FieldType.TEXT,
            description="Stable identifier of the user account to target.",
            examples=("u_123",),
            option_name="--user-id",
        ),
        FieldSpec(
            key="email",
            label="Email",
            type=FieldType.EMAIL,
            description="Email address of the user account to target.",
            examples=("operator@example.com",),
            option_name="--email",
        ),
    )


def _actor_field() -> FieldSpec:
    return FieldSpec(
        key="actor",
        label="Actor",
        type=FieldType.TEXT,
        advanced=True,
        default="cli",
        description="Audit actor recorded for the administrative change.",
        examples=("cli", "support-operator"),
        option_name="--actor",
    )


def _password_field() -> FieldSpec:
    return FieldSpec(
        key="password",
        label="Password",
        type=FieldType.SECRET,
        required=True,
        secret=True,
        description="New local-auth password supplied through an approved secret channel.",
        placeholder="Enter securely via stdin or a protected secret-input file",
        allow_secret_file=True,
        allow_secret_stdin=True,
    )


def _roles_field(*, default: list[str] | None = None) -> FieldSpec:
    return FieldSpec(
        key="roles",
        label="Roles",
        type=FieldType.MULTI_SELECT,
        required=default is None,
        choices=ROLE_CHOICES,
        default=default,
        description="Complete set of local-auth roles to assign to the user.",
        examples=("operator", "viewer"),
        option_name="--role",
    )


class UsersListCommandInput(BaseModel):
    """Validated input for the public users-list command."""

    model_config = ConfigDict(extra="forbid")

    search: str = ""
    include_inactive: bool = False
    include_deleted: bool = False
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @field_validator("limit")
    @classmethod
    def normalize_limit(cls, value: int) -> int:
        return max(1, min(int(value or 50), 200))

    @field_validator("offset")
    @classmethod
    def normalize_offset(cls, value: int) -> int:
        return max(0, int(value or 0))


USERS_LIST_SPEC = CommandSpec(
    command_id=_TOKEN_USERS_LIST,
    path=("users", "list"),
    summary="List users.",
    description="List users visible to the configured management runtime.",
    examples=("flow-steward users list --search operator",),
    operation_type=OperationType.READ,
    interactive_policy=InteractivePolicy.IMMEDIATE,
    fields=(
        FieldSpec(
            key="search",
            label="Search",
            type=FieldType.TEXT,
            description="Text matched against user identity and profile fields.",
            examples=("operator", "alex@example.com"),
            option_name="--search",
        ),
        FieldSpec(
            key="include_inactive",
            label="Include inactive",
            type=FieldType.BOOLEAN,
            option_name="--include-inactive",
            default=False,
            description="Include deactivated user accounts in the result set.",
            examples=("true", "false"),
        ),
        FieldSpec(
            key="include_deleted",
            label="Include deleted",
            type=FieldType.BOOLEAN,
            option_name="--include-deleted",
            default=False,
            description="Include soft-deleted user accounts in the result set.",
            examples=("true", "false"),
        ),
        FieldSpec(
            key="limit",
            label="Limit",
            type=FieldType.INTEGER,
            option_name="--limit",
            default=50,
            description="Maximum rows to return (1-200).",
            examples=("25", "100"),
        ),
        FieldSpec(
            key="offset",
            label="Offset",
            type=FieldType.INTEGER,
            option_name="--offset",
            default=0,
            description="Zero-based row offset for pagination.",
            examples=("0", "50"),
        ),
    ),
    input_model=UsersListCommandInput,
    result_model=UserListView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_USERS_LIST,
    result_renderer_ref=_TOKEN_USERS_LIST,
)


USERS_CREATE_SPEC = CommandSpec(
    command_id=_TOKEN_USERS_CREATE,
    path=("users", "create"),
    summary="Create a user.",
    description="Create a user after an explicit review of the pending change.",
    examples=(
        "flow-steward users create --email operator@example.com --password-stdin",
        "flow-steward users create --email operator@example.com "
        "--password-file .flow-steward/secret-input/operator-password",
    ),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        FieldSpec(
            key="email",
            label="Email",
            type=FieldType.EMAIL,
            required=True,
            description="Unique sign-in email address for the new user.",
            examples=("operator@example.com",),
            option_name="--email",
        ),
        FieldSpec(
            key="password",
            label="Password",
            type=FieldType.SECRET,
            required=True,
            secret=True,
            description="Initial local-auth password supplied through an approved secret channel.",
            placeholder="Enter securely via stdin or a protected secret-input file",
            allow_secret_file=True,
            allow_secret_stdin=True,
        ),
        FieldSpec(
            key="display_name",
            label="Display name",
            type=FieldType.TEXT,
            description="Human-readable name shown for the user in management interfaces.",
            examples=("Alex Morgan",),
            option_name="--display-name",
        ),
        FieldSpec(
            key="roles",
            label="Roles",
            type=FieldType.MULTI_SELECT,
            choices=ROLE_CHOICES,
            default=["viewer"],
            description="Initial local-auth roles granted to the new user.",
            examples=("viewer", "operator"),
            option_name="--role",
        ),
        FieldSpec(
            key="is_active",
            label="Active",
            type=FieldType.BOOLEAN,
            default=True,
            description="Whether the new user can authenticate immediately after creation.",
            examples=("true", "false"),
            option_name="--is-active",
        ),
        FieldSpec(
            key="must_change_password",
            label="Require password change",
            type=FieldType.BOOLEAN,
            default=False,
            description="Require the user to replace the initial password at next sign-in.",
            examples=("true", "false"),
            option_name="--must-change-password",
        ),
        FieldSpec(
            key="account_id",
            label="Primary account",
            type=FieldType.SINGLE_SELECT,
            choice_provider="accounts.available",
            advanced=True,
            description="Primary account associated with the new user.",
            examples=("acct_primary",),
            option_name="--account-id",
        ),
        FieldSpec(
            key="account_ids",
            label="Account memberships",
            type=FieldType.MULTI_SELECT,
            choice_provider="accounts.available",
            advanced=True,
            description="Additional accounts in which the new user receives membership.",
            examples=("acct_europe", "acct_support"),
            option_name="--account-membership",
        ),
        FieldSpec(
            key="actor",
            label="Actor",
            type=FieldType.TEXT,
            advanced=True,
            default="cli",
            description="Audit actor recorded as creating the user.",
            examples=("cli", "support-operator"),
            option_name="--actor",
        ),
    ),
    input_model=UserCreateInput,
    result_model=UserMutationResultView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_USERS_CREATE,
    result_renderer_ref=_TOKEN_USERS_CREATE,
    accepts_input_json=True,
)


USERS_SET_PASSWORD_SPEC = CommandSpec(
    command_id=_TOKEN_USERS_SET_PASSWORD,
    path=("users", "set-password"),
    summary="Set a user's password.",
    description="Set a local-auth user's password through approved secret channels.",
    examples=(
        "flow-steward users set-password --email operator@example.com --password-stdin",
        "flow-steward users set-password --user-id u_123 "
        "--password-file .flow-steward/secret-input/operator-password",
    ),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        *_user_selector_fields(),
        _password_field(),
        FieldSpec(
            key="must_change_password",
            label="Require password change",
            type=FieldType.BOOLEAN,
            description="Require the user to replace this password at next sign-in.",
            examples=("true", "false"),
            option_name="--must-change-password",
        ),
        FieldSpec(
            key="revoke_sessions",
            label="Revoke sessions",
            type=FieldType.BOOLEAN,
            default=False,
            description="Revoke the user's active sessions after setting the password.",
            examples=("true", "false"),
            option_name="--revoke-sessions",
        ),
        _actor_field(),
    ),
    input_model=UserPasswordUpdateInput,
    result_model=UserMutationResultView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_USERS_SET_PASSWORD,
    result_renderer_ref=_TOKEN_USERS_SET_PASSWORD,
    exactly_one_of=(USER_TARGET_GROUP,),
)


USERS_FORCE_RESET_SPEC = CommandSpec(
    command_id=_TOKEN_USERS_FORCE_RESET,
    path=("users", "force-reset"),
    summary="Force a password reset.",
    description="Require password reset on next login and revoke active sessions.",
    examples=("flow-steward users force-reset --email operator@example.com",),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(*_user_selector_fields(), _actor_field()),
    input_model=UserSelectorInput,
    result_model=UserMutationResultView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_USERS_FORCE_RESET,
    result_renderer_ref=_TOKEN_USERS_FORCE_RESET,
    exactly_one_of=(USER_TARGET_GROUP,),
)


USERS_UNLOCK_SPEC = CommandSpec(
    command_id=_TOKEN_USERS_UNLOCK,
    path=("users", "unlock"),
    summary="Unlock a user.",
    description="Clear local-auth login lockout counters for a user.",
    examples=("flow-steward users unlock --email operator@example.com",),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(*_user_selector_fields(), _actor_field()),
    input_model=UserSelectorInput,
    result_model=UserMutationResultView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_USERS_UNLOCK,
    result_renderer_ref=_TOKEN_USERS_UNLOCK,
    exactly_one_of=(USER_TARGET_GROUP,),
)


USERS_SET_ACTIVE_SPEC = CommandSpec(
    command_id=_TOKEN_USERS_SET_ACTIVE,
    path=("users", "set-active"),
    summary="Activate or deactivate a user.",
    description="Set a local-auth user's active state after review.",
    examples=(
        "flow-steward users set-active --email operator@example.com --is-active",
        "flow-steward users set-active --email operator@example.com --no-is-active",
    ),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        *_user_selector_fields(),
        FieldSpec(
            key="is_active",
            label="Active",
            type=FieldType.BOOLEAN,
            required=True,
            description="Desired authentication state for the targeted user account.",
            examples=("true", "false"),
            option_name="--is-active",
        ),
        _actor_field(),
    ),
    input_model=UserActiveUpdateInput,
    result_model=UserMutationResultView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_USERS_SET_ACTIVE,
    result_renderer_ref=_TOKEN_USERS_SET_ACTIVE,
    exactly_one_of=(USER_TARGET_GROUP,),
)


USERS_SET_ROLES_SPEC = CommandSpec(
    command_id=_TOKEN_USERS_SET_ROLES,
    path=("users", "set-roles"),
    summary="Replace user roles.",
    description="Replace all local-auth roles assigned to a user.",
    examples=("flow-steward users set-roles --email operator@example.com --role operator",),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(*_user_selector_fields(), _roles_field(), _actor_field()),
    input_model=UserRolesUpdateInput,
    result_model=UserMutationResultView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_USERS_SET_ROLES,
    result_renderer_ref=_TOKEN_USERS_SET_ROLES,
    exactly_one_of=(USER_TARGET_GROUP,),
)


USERS_ISSUE_BOOTSTRAP_TOKEN_SPEC = CommandSpec(
    command_id=_TOKEN_USERS_ISSUE_BOOTSTRAP_TOKEN,
    path=("users", "issue-bootstrap-token"),
    summary="Issue a one-time first-admin setup token.",
    description="Replace the active 60-minute setup token while the installation has zero users.",
    examples=("flow-steward users issue-bootstrap-token --confirm",),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(),
    input_model=BootstrapTokenIssueInput,
    result_model=BootstrapTokenIssueResult,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_USERS_ISSUE_BOOTSTRAP_TOKEN,
    result_renderer_ref=_TOKEN_USERS_ISSUE_BOOTSTRAP_TOKEN,
)


USER_COMMAND_SPECS = (
    USERS_LIST_SPEC,
    USERS_CREATE_SPEC,
    USERS_SET_PASSWORD_SPEC,
    USERS_FORCE_RESET_SPEC,
    USERS_UNLOCK_SPEC,
    USERS_SET_ACTIVE_SPEC,
    USERS_SET_ROLES_SPEC,
    USERS_ISSUE_BOOTSTRAP_TOKEN_SPEC,
)
