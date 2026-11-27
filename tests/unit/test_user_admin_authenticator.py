"""Identity-wide authenticator mutations are limited to the administrator's accounts."""

from unittest.mock import MagicMock

import pytest

from core.application.auth.user_admin import command_service
from core.application.auth.user_admin.command_service import UserAdminCommandService
from core.application.management.errors import ServiceForbiddenError


@pytest.fixture(autouse=True)
def _no_company_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scripted cursors here answer no owner query (ADR 0085, "Company owner")."""
    from core.application.auth import account_owner

    monkeypatch.setattr(account_owner, "owned_account_ids", lambda *_args, **_kwargs: [])


_USER_ID = "8f223319-f856-463a-9e78-e53a8415c28d"
_ADMIN_ID = "a37721ff-bacf-44d1-a32a-bafb57b1926c"


@pytest.fixture
def harness(monkeypatch):
    db = MagicMock()
    db._column_exists.return_value = True
    db._table_exists.return_value = True
    transaction = MagicMock()
    conn = transaction.return_value.__enter__.return_value
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [("target@example.test", "primary", True, False)]
    cursor.fetchall.return_value = [("primary",), ("other",)]
    cursor.rowcount = 2
    monkeypatch.setattr(command_service, "transaction_connection", transaction)
    audit = MagicMock()
    service = UserAdminCommandService(
        get_logger=lambda: db,
        auth_tables_available_fn=lambda _: True,
        log_auth_audit_event_fn=audit,
    )
    return service, cursor, audit


def test_reset_revokes_factor_codes_and_sessions_under_the_user_lock(harness):
    service, cursor, audit = harness

    result = service.update_authenticator(
        user_id=_USER_ID,
        actor_user_id=_ADMIN_ID,
        actor="admin@example.test",
        action="reset",
        allowed_account_ids=["primary", "other"],
    )

    assert result.model_dump() == {
        "status": "authenticator_reset",
        "authenticator_required": True,
        "revoked_sessions": 2,
    }
    queries = [call.args[0] for call in cursor.execute.call_args_list]
    assert "FOR UPDATE" in queries[0]
    assert "FROM user_account_memberships" in queries[1]
    assert "DELETE FROM auth_totp_factors" in queries[2]
    assert "DELETE FROM auth_mfa_recovery_codes" in queries[3]
    assert "SET authenticator_reset_pending = true" in queries[4]
    assert "authenticator_required" not in queries[4]
    assert "UPDATE auth_sessions" in queries[5]
    assert cursor.execute.call_args_list[5].args[1] == (_USER_ID, None)
    assert audit.call_args.kwargs["username"] == "target@example.test"
    assert audit.call_args.kwargs["action"] == "user_authenticator_reset"


@pytest.mark.parametrize("actor_id", [_USER_ID, _ADMIN_ID])
def test_guard_checks_self_and_all_target_memberships(harness, actor_id):
    service, cursor, audit = harness

    with pytest.raises(ServiceForbiddenError):
        service.update_authenticator(
            user_id=_USER_ID,
            actor_user_id=actor_id,
            actor="admin@example.test",
            action="reset",
            allowed_account_ids=["primary"],
        )

    queries = [str(call.args[0]).strip() for call in cursor.execute.call_args_list]
    assert all(query.startswith("SELECT") for query in queries)
    audit.assert_not_called()


def test_unrequire_preserves_an_outstanding_reset(harness):
    service, cursor, _ = harness
    cursor.fetchone.side_effect = [("target@example.test", "primary", True, True)]

    result = service.update_authenticator(
        user_id=_USER_ID,
        actor_user_id=_ADMIN_ID,
        actor="admin@example.test",
        action="unrequire",
        allowed_account_ids=["primary", "other"],
    )

    assert result.authenticator_required is False
    assert result.revoked_sessions == 0
    queries = [call.args[0] for call in cursor.execute.call_args_list]
    assert "SET authenticator_required = %s" in queries[2]
    assert cursor.execute.call_args_list[2].args[1] == (False, _USER_ID)
    assert len(queries) == 3


def test_requiring_unenrolled_user_revokes_sessions(harness):
    service, cursor, _ = harness
    cursor.fetchone.side_effect = [("target@example.test", "primary", False, False), None]

    result = service.update_authenticator(
        user_id=_USER_ID,
        actor_user_id=_ADMIN_ID,
        actor="admin@example.test",
        action="require",
        allowed_account_ids=["primary", "other"],
    )

    assert result.authenticator_required is True
    assert result.revoked_sessions == 2
    queries = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("SET authenticator_required = %s" in query for query in queries)
    assert any("UPDATE auth_sessions" in query for query in queries)
