"""Local password and authenticator rules hold only sessions issued by a local password.

A session issued by external sign-in (ADR 0083) is not logged out when the local
password grows old, and is not refused on an installation that requires a local
authenticator: the sign-in method authenticated that person.
"""

from __future__ import annotations

import pytest

from core.application.auth import local_credentials
from core.application.auth.local_credentials import local_session_credential_filter


class _Store:
    def __init__(self, *, auth_method: bool) -> None:
        self._auth_method = auth_method

    def _column_exists(self, table: str, column: str) -> bool:
        if (table, column) == ("auth_sessions", "auth_method"):
            return self._auth_method
        return (table, column) in {
            ("users", "password_changed_at"),
            ("users", "authenticator_required"),
            ("users", "authenticator_reset_pending"),
        }

    def _table_exists(self, table: str) -> bool:
        return table in {"auth_totp_factors", "auth_mfa_recovery_codes"}


@pytest.fixture(autouse=True)
def _mfa_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_credentials, "installation_requires_mfa", lambda: True)


def test_the_local_rules_apply_only_to_local_sessions() -> None:
    condition = local_session_credential_filter(_Store(auth_method=True))

    assert condition.startswith(" AND (s.auth_method <> 'local' OR (TRUE AND ")
    assert "u.password_changed_at > now()" in condition
    assert "auth_totp_factors" in condition
    assert "NOT u.authenticator_reset_pending" in condition
    assert condition.count("(") == condition.count(")")


def test_a_database_without_the_column_keeps_the_previous_conditions() -> None:
    condition = local_session_credential_filter(_Store(auth_method=False))

    assert condition.startswith(" AND u.password_changed_at > now()")
    assert "auth_method" not in condition


def test_the_session_alias_follows_the_query() -> None:
    condition = local_session_credential_filter(_Store(auth_method=True), session_alias="sess")

    assert "sess.auth_method <> 'local'" in condition


def test_the_session_alias_must_be_an_identifier() -> None:
    with pytest.raises(ValueError):
        local_session_credential_filter(_Store(auth_method=True), session_alias="s; DROP")


def test_nothing_to_check_adds_no_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_credentials, "installation_requires_mfa", lambda: False)

    class _Empty:
        def _column_exists(self, table: str, column: str) -> bool:
            return (table, column) == ("auth_sessions", "auth_method")

        def _table_exists(self, table: str) -> bool:
            return False

    assert local_session_credential_filter(_Empty()) == ""
