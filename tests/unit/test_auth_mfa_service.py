"""Regression coverage for local authenticator enrollment and verification."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from core.application.auth import mfa_codes, mfa_service
from core.infrastructure.security.secret_codec import encrypt_secret

# Public test vector from RFC 6238, Appendix B.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")
RFC_INSTANT = datetime.fromtimestamp(59, UTC)
RFC_CODE = "287082"
USER_ID = "9e8f424a-86b2-4f21-878b-4df2cb335df9"
SESSION_ID = "ed27e7ff-aa6f-4493-8fd3-d1caa174f9e7"


@pytest.fixture
def configured_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "unit-test-encryption-key")


@pytest.mark.parametrize(
    ("timestamp", "code", "step"),
    [
        (59, "287082", 1),
        (1_111_111_109, "081804", 37_037_036),
        (90, "287082", None),
        (59, "\uff12\uff18\uff17\uff10\uff18\uff12", None),
    ],
)
def test_totp_vectors_and_out_of_window_codes(timestamp, code, step):
    assert (
        mfa_codes.matching_totp_step(RFC_SECRET, code, now=datetime.fromtimestamp(timestamp, UTC))
        == step
    )


def test_pending_enrollment_uses_expiration_and_preserves_active_factor(
    monkeypatch, configured_key
):
    monkeypatch.setattr(mfa_service, "_now", lambda: RFC_INSTANT)
    monkeypatch.setattr(mfa_codes, "new_totp_secret", lambda: RFC_SECRET)
    cur = MagicMock()

    enrollment = mfa_service.begin_mfa_enrollment(
        cur, user_id=USER_ID, username="alex+ops@example.test", session_id=SESSION_ID
    )
    query, params = cur.execute.call_args.args

    assert set(enrollment) == {"secret", "uri"}
    assert enrollment["secret"] == RFC_SECRET
    uri = urlsplit(enrollment["uri"])
    assert uri.scheme == "otpauth"
    assert uri.netloc == "totp"
    assert parse_qs(uri.query)["secret"] == [RFC_SECRET]
    assert params[0] == USER_ID
    assert RFC_SECRET not in params[1]
    assert params[2] == RFC_INSTANT + timedelta(minutes=10)
    assert "SET pending_secret_enc" in query
    assert "DELETE" not in query


def test_confirmed_factor_starts_with_used_step_and_rotates_recovery_codes(
    monkeypatch, configured_key
):
    monkeypatch.setattr(mfa_service, "_now", lambda: RFC_INSTANT)
    monkeypatch.setattr(mfa_codes, "new_totp_secret", lambda: RFC_SECRET)
    cur = MagicMock()
    mfa_service.begin_mfa_enrollment(cur, user_id=USER_ID, username="alex", session_id=SESSION_ID)
    _, params = cur.execute.call_args.args
    cur.fetchone.return_value = {"pending_secret_enc": params[1]}

    codes = mfa_service.complete_mfa_enrollment(
        cur, user_id=USER_ID, code=RFC_CODE, session_id=SESSION_ID
    )

    assert codes is not None
    assert len(set(codes)) == 10
    enrollment_query, query_params = cur.execute.call_args_list[1].args
    assert "pending_expires_at >" in enrollment_query
    assert query_params == (USER_ID, RFC_INSTANT)
    activate_query, activate_params = cur.execute.call_args_list[2].args
    assert "SET secret_enc = %s" in activate_query
    assert activate_params[0] != params[1]
    assert activate_params[1:] == (1, USER_ID)
    stored = [mfa_codes.recovery_code_hash(code.upper().replace("-", "")) for code in codes]
    assert all(stored)
    _, inserted = cur.executemany.call_args.args
    assert inserted == [(USER_ID, code_hash) for code_hash in stored]

    cur.reset_mock()
    cur.fetchone.return_value = (activate_params[0], 1)
    assert not mfa_service.verify_mfa(cur, USER_ID, RFC_CODE)
    cur.execute.assert_called_once()


def test_enrollment_cannot_be_confirmed_from_a_different_session(monkeypatch, configured_key):
    monkeypatch.setattr(mfa_service, "_now", lambda: RFC_INSTANT)
    monkeypatch.setattr(mfa_codes, "new_totp_secret", lambda: RFC_SECRET)
    cur = MagicMock()
    mfa_service.begin_mfa_enrollment(cur, user_id=USER_ID, username="alex", session_id=SESSION_ID)
    _, params = cur.execute.call_args.args
    cur.reset_mock()
    cur.fetchone.return_value = {"pending_secret_enc": params[1]}

    confirmed = mfa_service.complete_mfa_enrollment(
        cur, user_id=USER_ID, code=RFC_CODE, session_id="d48636dd-b99b-49e2-b6cb-2de885ea700f"
    )

    assert confirmed is None
    cur.execute.assert_called_once()
    cur.executemany.assert_not_called()


def test_expired_or_absent_enrollment_has_no_side_effects(monkeypatch):
    monkeypatch.setattr(mfa_service, "_now", lambda: RFC_INSTANT)
    cur = MagicMock()
    cur.fetchone.return_value = None

    assert (
        mfa_service.complete_mfa_enrollment(
            cur, user_id=USER_ID, code=RFC_CODE, session_id=SESSION_ID
        )
        is None
    )
    assert "pending_expires_at >" in cur.execute.call_args.args[0]
    cur.execute.assert_called_once()
    cur.executemany.assert_not_called()


def test_verifies_totp_and_advances_before_returning(monkeypatch, configured_key):
    monkeypatch.setattr(mfa_service, "_now", lambda: RFC_INSTANT)
    cur = MagicMock()
    secret_enc = encrypt_secret(RFC_SECRET, mfa_service._encryption_key(USER_ID))
    cur.fetchone.return_value = {"secret_enc": secret_enc, "last_used_step": None}
    cur.rowcount = 1

    assert mfa_service.verify_mfa(cur, USER_ID, RFC_CODE)
    assert "FOR UPDATE" in cur.execute.call_args_list[0].args[0]
    assert cur.execute.call_args.args[1] == (1, USER_ID, 1)

    cur.fetchone.return_value = (secret_enc, 1)
    assert not mfa_service.verify_mfa(cur, USER_ID, RFC_CODE)
    assert "FOR UPDATE" in cur.execute.call_args.args[0]


def test_recovery_code_is_consumed_with_one_conditional_delete():
    cur = MagicMock()
    cur.rowcount = 1
    recovery_code = "01234567-abCDef89-01234567-12345678"
    expected_hash = mfa_codes.recovery_code_hash(recovery_code)

    assert mfa_service.verify_mfa(cur, USER_ID, recovery_code)
    query, params = cur.execute.call_args.args
    cur.execute.assert_called_once()
    assert "DELETE FROM auth_mfa_recovery_codes" in query
    assert "auth_totp_factors" in query
    assert "secret_enc IS NOT NULL" in query
    assert params == (USER_ID, expected_hash)
    assert expected_hash and expected_hash != recovery_code

    cur.rowcount = 0
    assert not mfa_service.verify_mfa(cur, USER_ID, recovery_code)
