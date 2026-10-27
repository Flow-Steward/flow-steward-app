"""Focused tests for reusable local-auth user admin services."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.user_admin import (
    UserAdminCommandService,
    UserAdminQueryService,
    UserAuditContext,
    UserCreateInput,
    UserDeleteInput,
    UserMutationResultView,
    UserPasswordUpdateInput,
    UserSelectorInput,
    UserUpdateInput,
)


def _clear_user_login_failures(_username: str, *, bl: Any = None) -> None:
    _ = bl


class _AccountLogger:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list_accounts(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(dict(kwargs))
        return [
            {
                "account_id": "acc.prod",
                "display_name": "Production",
                "is_active": True,
            },
            {
                "account_id": "acc.disabled",
                "display_name": "Disabled",
                "is_active": False,
            },
            {
                "id": "legacy",
                "display_name": "",
                "is_active": True,
            },
        ]


class _UserListCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> _UserListCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((str(sql), tuple(params or ())))

    def fetchone(self) -> dict[str, int]:
        return {"total": 3}

    def fetchall(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "u_3",
                "username": "third@example.com",
                "display_name": "Third",
                "is_active": True,
                "must_change_password": False,
                "deleted_at": None,
                "account_id": "default",
                "roles": ["viewer"],
            }
        ]


class _UserListLogger:
    def __init__(self) -> None:
        self.cursor_obj = _UserListCursor()
        self.conn = self

    def cursor(self, *_args: Any, **_kwargs: Any) -> _UserListCursor:
        return self.cursor_obj


class _RecordingUserAdminRepository:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.force_reset_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def list_auth_users(self, _bl: Any, **kwargs: Any):
        self.list_calls.append(dict(kwargs))
        return (
            [
                {
                    "id": "u_repo",
                    "username": "repo@example.com",
                    "roles": ["viewer"],
                }
            ],
            1,
            int(kwargs["limit"]),
            int(kwargs["offset"]),
        )

    def create_auth_user(self, _bl: Any, **kwargs: Any) -> str:
        self.create_calls.append(dict(kwargs))
        return "u_repo"

    def update_auth_user(self, _bl: Any, **kwargs: Any) -> int:
        self.update_calls.append(dict(kwargs))
        return 2

    def force_password_reset(self, _bl: Any, **kwargs: Any) -> int:
        self.force_reset_calls.append(dict(kwargs))
        return 4

    def delete_auth_user(self, _bl: Any, **kwargs: Any) -> int:
        self.delete_calls.append(dict(kwargs))
        return 3


def test_user_command_service_rejects_self_delete_before_persistence() -> None:
    repository = _RecordingUserAdminRepository()
    user = {
        "id": "u_admin",
        "username": "admin@example.com",
        "roles": ["admin"],
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    service = UserAdminCommandService(
        get_logger=lambda: object(),
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_id_fn=lambda *_args: dict(user),
        log_auth_audit_event_fn=lambda *_args, **_kwargs: None,
        repository=repository,
    )
    delete_input = UserDeleteInput(
        user_id="u_admin",
        actor_user_id="u_admin",
        actor="admin@example.com",
    )

    with pytest.raises(Exception, match="own user"):
        service.delete_user(delete_input)

    assert repository.delete_calls == []


def test_user_command_service_delegates_soft_delete_and_audits_revocation() -> None:
    repository = _RecordingUserAdminRepository()
    audits: list[dict[str, Any]] = []
    user = {
        "id": "u_target",
        "username": "target@example.com",
        "roles": ["viewer"],
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    service = UserAdminCommandService(
        get_logger=lambda: object(),
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_id_fn=lambda *_args: dict(user),
        log_auth_audit_event_fn=lambda _bl, **kwargs: audits.append(dict(kwargs)),
        auth_login_clear_user_failures_fn=_clear_user_login_failures,
        repository=repository,
    )

    result = service.delete_user(
        UserDeleteInput(
            user_id="u_target",
            actor_user_id="u_admin",
            actor="admin@example.com",
        ),
        audit_context=UserAuditContext(actor="admin@example.com", source="api"),
    )

    assert result.status == "deleted"
    assert result.user_id == "u_target"
    assert result.revoked_sessions == 3
    assert repository.delete_calls == [{"user_id": "u_target", "deleted_by": "admin@example.com"}]
    assert audits == [
        {
            "action": "user_delete",
            "username": "target@example.com",
            "actor": "admin@example.com",
            "status": "ok",
            "detail": {"source": "api", "revoked_sessions": 3},
        }
    ]


class _DeleteCursor:
    def __init__(self, *, total_users: int, active_admins: int, target_admin: bool) -> None:
        self.total_users = total_users
        self.active_admins = active_admins
        self.target_admin = target_admin
        self.rowcount = 0
        self.current: dict[str, Any] | None = None
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql: str, _params=()) -> None:
        normalized = " ".join(sql.split())
        self.queries.append(normalized)
        self.current = None
        self.rowcount = 0
        if "SELECT u.id::text AS id, u.is_active" in normalized:
            self.current = {"id": "u_target", "is_active": True, "is_admin": self.target_admin}
        elif "SELECT COUNT(*) AS total FROM users WHERE deleted_at IS NULL" in normalized:
            self.current = {"total": self.total_users}
        elif "u.is_active = true" in normalized and "COUNT(*) AS total" in normalized:
            self.current = {"total": self.active_admins}
        elif "UPDATE auth_sessions" in normalized:
            self.rowcount = 2

    def fetchone(self):
        return self.current


class _DeleteLogger:
    def __init__(self, cursor: _DeleteCursor) -> None:
        self.cursor_obj = cursor
        self.commits = 0
        self.rollbacks = 0
        self.autocommit = True
        self.conn = self

    def cursor(self, **_kwargs):
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def _table_exists(self, _name: str) -> bool:
        return True


def test_delete_repository_rejects_last_user_without_mutation() -> None:
    from core.application.auth.user_admin.repository import delete_auth_user
    from core.application.management.errors import ServiceConflictError

    logger = _DeleteLogger(_DeleteCursor(total_users=1, active_admins=1, target_admin=True))

    with pytest.raises(ServiceConflictError, match="last user"):
        delete_auth_user(logger, user_id="u_target", deleted_by="admin@example.com")

    assert logger.commits == 0
    assert logger.rollbacks == 1
    assert not any(
        "UPDATE users SET is_active = false" in query for query in logger.cursor_obj.queries
    )


def test_delete_repository_rejects_last_active_admin() -> None:
    from core.application.auth.user_admin.repository import delete_auth_user
    from core.application.management.errors import ServiceConflictError

    logger = _DeleteLogger(_DeleteCursor(total_users=2, active_admins=1, target_admin=True))

    with pytest.raises(ServiceConflictError, match="last active administrator"):
        delete_auth_user(logger, user_id="u_target", deleted_by="admin@example.com")

    assert logger.rollbacks == 1


def test_delete_repository_soft_deletes_and_revokes_auth_material() -> None:
    from core.application.auth.user_admin.repository import delete_auth_user

    logger = _DeleteLogger(_DeleteCursor(total_users=3, active_admins=2, target_admin=True))

    revoked = delete_auth_user(
        logger,
        user_id="u_target",
        deleted_by="admin@example.com",
    )

    assert revoked == 2
    assert logger.commits == 1
    sql = "\n".join(logger.cursor_obj.queries)
    assert "pg_advisory_xact_lock" in sql
    assert "UPDATE auth_sessions" in sql
    assert "UPDATE personal_access_tokens" in sql
    assert "UPDATE users SET is_active = false, deleted_at = now()" in sql


def test_user_update_input_rejects_blank_display_name() -> None:
    with pytest.raises(ValueError, match="display_name is required"):
        UserUpdateInput(user_id="u_repo", display_name="   ")


def test_user_query_service_lists_bounded_active_account_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.application.auth.user_admin.query_service as services

    logger = _AccountLogger()
    monkeypatch.setattr(services, "auth_tables_available", lambda _logger: True)

    result = UserAdminQueryService(get_logger=lambda: logger).list_accounts(limit=999)

    assert logger.calls == [{"include_inactive": False, "search": "", "limit": 200, "offset": 0}]
    assert result.total == 2
    assert [account.id for account in result.accounts] == ["acc.prod", "legacy"]
    assert result.accounts[0].display_name == "Production"
    assert result.accounts[1].display_name == "legacy"


def test_user_query_service_reports_missing_logger_as_unavailable() -> None:
    from core.application.management.errors import ServiceUnavailableError

    service = UserAdminQueryService(get_logger=lambda: None)

    with pytest.raises(ServiceUnavailableError):
        service.list_accounts()


def test_user_query_service_delegates_list_persistence_to_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.application.auth.user_admin.query_service as services

    repository = _RecordingUserAdminRepository()
    logger = object()
    monkeypatch.setattr(services, "get_user_membership_map", lambda *_args, **_kwargs: {})
    service = UserAdminQueryService(
        get_logger=lambda: logger,
        auth_tables_available_fn=lambda _logger: True,
        repository=repository,
    )

    result = service.list_users(limit=7, offset=3)

    assert result.total == 1
    assert result.users[0].username == "repo@example.com"
    assert repository.list_calls == [
        {
            "search": "",
            "include_inactive": False,
            "include_deleted": False,
            "limit": 7,
            "offset": 3,
            "role": "",
            "active_filter": None,
            "managed_account_ids": None,
        }
    ]


def test_user_command_service_delegates_create_persistence_to_repository() -> None:
    repository = _RecordingUserAdminRepository()
    logger = object()
    resolved = {
        "id": "u_repo",
        "username": "repo@example.com",
        "password_hash": "private",
        "roles": ["viewer"],
    }
    service = UserAdminCommandService(
        get_logger=lambda: logger,
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_username_fn=lambda *_args: None,
        get_user_by_id_fn=lambda *_args: dict(resolved),
        normalize_requested_roles_fn=lambda roles: list(roles or []),
        seed_default_roles_fn=lambda _logger: None,
        password_hash_fn=lambda value: f"hash:{value}",
        log_auth_audit_event_fn=lambda *_args, **_kwargs: None,
        repository=repository,
    )

    result = service.create_user(
        UserCreateInput(
            email="repo@example.com",
            password="Password123!",
            roles=["viewer"],
        )
    )

    assert result.user.id == "u_repo"
    assert "password_hash" not in result.user.model_dump(mode="json")
    assert repository.create_calls[0]["email"] == "repo@example.com"
    assert repository.create_calls[0]["password_hash_value"] == "hash:Password123!"
    assert repository.create_calls[0]["primary_account_id"] == DEFAULT_ACCOUNT_ID
    assert repository.create_calls[0]["requested_account_ids"] == [DEFAULT_ACCOUNT_ID]
    assert not any(key.startswith("sql_") for key in repository.create_calls[0])


@pytest.mark.parametrize("failure_mode", ("raised", "explicit"))
def test_user_creation_logs_redacted_notification_failures_without_failing_creation(
    failure_mode: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _RecordingUserAdminRepository()
    resolved = {
        "id": "u_repo",
        "username": "private-recipient@example.com",
        "roles": ["viewer"],
    }

    def notifier(**_kwargs: Any) -> dict[str, Any]:
        if failure_mode == "raised":
            raise RuntimeError("smtp-password=must-not-be-logged")
        return {"ok": False, "reason": "private-delivery-detail"}

    service = UserAdminCommandService(
        get_logger=lambda: object(),
        signup_notifier=notifier,
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_username_fn=lambda *_args: None,
        get_user_by_id_fn=lambda *_args: dict(resolved),
        normalize_requested_roles_fn=lambda roles: list(roles or []),
        seed_default_roles_fn=lambda _logger: None,
        password_hash_fn=lambda value: f"hash:{value}",
        log_auth_audit_event_fn=lambda *_args, **_kwargs: None,
        repository=repository,
    )

    with caplog.at_level("WARNING"):
        result = service.create_user(
            UserCreateInput(
                email="private-recipient@example.com",
                password="Password123!",
                roles=["viewer"],
            )
        )

    assert result.status == "created"
    assert result.user.id == "u_repo"
    assert result.warnings == [
        "User created, but the welcome email was not delivered. "
        "Check the email extension's connection."
    ]
    assert "user_id=u_repo" in caplog.text
    assert "private-recipient@example.com" not in caplog.text
    assert "smtp-password" not in caplog.text
    assert "private-delivery-detail" not in caplog.text


def test_set_password_delegates_persistence_and_preserves_audit_semantics() -> None:
    repository = _RecordingUserAdminRepository()
    audits: list[dict[str, Any]] = []
    user = {
        "id": "u_repo",
        "username": "repo@example.com",
        "password_hash": "private",
        "roles": ["viewer"],
    }
    service = UserAdminCommandService(
        get_logger=lambda: object(),
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_id_fn=lambda *_args: dict(user),
        password_hash_fn=lambda value: f"hash:{value}",
        log_auth_audit_event_fn=lambda _bl, **kwargs: audits.append(dict(kwargs)),
        auth_login_clear_user_failures_fn=_clear_user_login_failures,
        repository=repository,
    )

    result = service.set_password(
        UserPasswordUpdateInput(
            user_id="u_repo",
            password="Password123!",
            must_change_password=True,
            revoke_sessions=True,
            actor="admin@example.com",
        ),
        audit_context=UserAuditContext(actor="admin@example.com", source="api"),
    )

    assert result.status == "password_updated"
    assert result.revoked_sessions == 2
    assert repository.update_calls[0]["password_hash_value"] == "hash:Password123!"
    assert repository.update_calls[0]["revoke_sessions"] is True
    assert not any(key.startswith("sql_") for key in repository.update_calls[0])
    assert audits == [
        {
            "action": "user_password_set",
            "username": "repo@example.com",
            "actor": "admin@example.com",
            "status": "ok",
            "detail": {
                "source": "api",
                "revoked_sessions": 2,
                "must_change_password": True,
            },
        }
    ]


def test_force_password_reset_delegates_persistence_to_repository() -> None:
    repository = _RecordingUserAdminRepository()
    user = {
        "id": "u_repo",
        "username": "repo@example.com",
        "roles": ["viewer"],
    }
    service = UserAdminCommandService(
        get_logger=lambda: object(),
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_id_fn=lambda *_args: dict(user),
        log_auth_audit_event_fn=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures_fn=_clear_user_login_failures,
        repository=repository,
    )

    result = service.force_password_reset(UserSelectorInput(user_id="u_repo"))

    assert result.revoked_sessions == 4
    assert repository.force_reset_calls == [{"user_id": "u_repo"}]


def test_unlock_clears_shared_login_rate_limit_state() -> None:
    logger = object()
    clear_calls: list[tuple[str, Any]] = []
    user = {
        "id": "u_repo",
        "username": "repo@example.com",
        "roles": ["viewer"],
    }

    def clear_failures(username: str, *, bl: Any = None) -> None:
        clear_calls.append((username, bl))

    service = UserAdminCommandService(
        get_logger=lambda: logger,
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_id_fn=lambda *_args: dict(user),
        log_auth_audit_event_fn=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures_fn=clear_failures,
    )

    result = service.unlock_user(UserSelectorInput(user_id="u_repo"))

    assert result.status == "unlocked"
    assert clear_calls == [("repo@example.com", logger)]


def test_user_mutation_result_drops_private_authentication_fields() -> None:
    result = UserMutationResultView(
        status="updated",
        user={
            "id": "u_1",
            "username": "user@example.com",
            "password_hash": "$argon2id$must-never-leave-application",
            "roles": ["viewer"],
        },
    )

    payload = result.model_dump(mode="json")["user"]

    assert payload["id"] == "u_1"
    assert payload["username"] == "user@example.com"
    assert "password_hash" not in payload


def test_user_query_service_list_users_applies_limit_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.application.auth.user_admin.query_service as services

    logger = _UserListLogger()
    monkeypatch.setattr(services, "auth_tables_available", lambda _logger: True)
    monkeypatch.setattr(services, "get_user_membership_map", lambda *_args, **_kwargs: {})

    result = UserAdminQueryService(get_logger=lambda: logger).list_users(limit=1, offset=2)

    assert result.total == 3
    assert result.limit == 1
    assert result.offset == 2
    assert result.has_more is False
    sql = "\n".join(call[0] for call in logger.cursor_obj.calls)
    assert "COUNT(*)" in sql
    assert "LIMIT %s OFFSET %s" in sql
    assert logger.cursor_obj.calls[-1][1][-2:] == (1, 2)


def test_user_query_service_uses_canonical_projection_and_membership_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.application.auth.user_admin.query_service as services

    logger = _UserListLogger()
    monkeypatch.setattr(services, "auth_tables_available", lambda _logger: True)
    monkeypatch.setattr(services, "get_user_membership_map", lambda *_args, **_kwargs: {})

    UserAdminQueryService(get_logger=lambda: logger).list_users(managed_account_ids=["acc.prod"])

    sql = "\n".join(call[0] for call in logger.cursor_obj.calls)
    assert "u.must_change_password AS must_change_password" in sql
    assert "u.deleted_at AS deleted_at" in sql
    assert "u.account_id AS account_id" in sql
    assert "u.account_id = ANY(%s)" in sql
    assert "FROM user_account_memberships uam_scope" in sql
    assert logger.cursor_obj.calls[0][1] == (["acc.prod"], ["acc.prod"])


def test_user_command_service_uses_fixed_canonical_repository_contract() -> None:
    repository = _RecordingUserAdminRepository()
    resolved = {
        "id": "u_repo",
        "username": "repo@example.com",
        "roles": ["viewer"],
        "account_id": "00000000-0000-0000-0000-000000000002",
        "account_ids": ["00000000-0000-0000-0000-000000000002"],
    }
    service = UserAdminCommandService(
        get_logger=lambda: object(),
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_username_fn=lambda *_args: None,
        get_user_by_id_fn=lambda *_args: dict(resolved),
        normalize_requested_roles_fn=lambda roles: list(roles or []),
        seed_default_roles_fn=lambda _logger: None,
        password_hash_fn=lambda value: f"hash:{value}",
        log_auth_audit_event_fn=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures_fn=_clear_user_login_failures,
        repository=repository,
    )

    service.create_user(
        UserCreateInput(
            email="create@example.com",
            password="Password123!",
            must_change_password=True,
        )
    )
    service.set_password(
        UserPasswordUpdateInput(
            user_id="u_repo",
            password="Password456!",
            must_change_password=True,
        )
    )

    assert "auth_user_column_exists" not in repository.create_calls[0]
    assert "auth_user_column_exists" not in repository.update_calls[0]
    assert repository.update_calls[0]["requested_account_ids"] is None
    assert repository.update_calls[0]["primary_account_id"] is None
