"""Self-service authenticator endpoints require the current local session."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth import mfa_repository, mfa_service
from core.entrypoints.api import auth_mfa

_BASE = "/api/auth/multi-factor"
_USER_ID = "8f223319-f856-463a-9e78-e53a8415c28d"
_SESSION_ID = "a37721ff-bacf-44d1-a32a-bafb57b1926c"


@pytest.fixture
def harness(monkeypatch):
    app = FastAPI()
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    logger = MagicMock()
    state = SimpleNamespace(
        required=False,
        per_user_required=False,
        enabled=False,
        csrf_valid=True,
        session_id=_SESSION_ID,
        auth_source="local",
        principal_type="user",
        locked=(False, 0),
    )

    @contextmanager
    def transaction(_logger):
        yield SimpleNamespace(cursor=lambda: cursor)

    monkeypatch.setattr(mfa_service, "transaction_connection", transaction)

    @app.middleware("http")
    async def inject_auth(request: Request, call_next):
        request.state.auth_source = state.auth_source
        request.state.auth_user = {
            "id": _USER_ID,
            "username": "owner@example.test",
            "principal_type": state.principal_type,
        }
        return await call_next(request)

    begin = MagicMock(return_value={"secret": "demo-secret", "uri": "otpauth://example"})
    confirm = MagicMock(return_value=["example-recovery-code"])
    revoke = MagicMock()
    check_factor = MagicMock(return_value=False)
    disable = MagicMock()
    audit = MagicMock()
    lock_user = MagicMock(return_value=True)
    lock_state = MagicMock(side_effect=lambda *_: state.locked)
    record_failure = MagicMock(return_value=(False, 0))
    cookie_session = MagicMock(side_effect=lambda *_args, **_kwargs: state.session_id)
    monkeypatch.setattr(auth_mfa, "authenticator_storage_available", lambda _logger: True)
    monkeypatch.setattr(mfa_service, "installation_requires_mfa", lambda: state.required)
    monkeypatch.setattr(mfa_service, "authenticator_policy_available", lambda _logger: True)
    cursor.fetchone.side_effect = lambda: (state.per_user_required, False)
    monkeypatch.setattr(mfa_service, "lock_local_user", lock_user)
    monkeypatch.setattr(mfa_repository, "active_cookie_session_id", cookie_session)
    monkeypatch.setattr(mfa_repository, "revoke_sessions", revoke)
    monkeypatch.setattr(mfa_service, "mfa_enabled", lambda *_: state.enabled)
    monkeypatch.setattr(mfa_service, "verify_mfa", check_factor)
    monkeypatch.setattr(mfa_service, "begin_mfa_enrollment", begin)
    monkeypatch.setattr(mfa_service, "complete_mfa_enrollment", confirm)
    monkeypatch.setattr(mfa_service, "disable_mfa", disable)

    app.include_router(
        auth_mfa.create_auth_mfa_router(
            local_auth_enabled=lambda: True,
            get_logger_only=lambda: logger,
            require_authenticated_user=lambda request: request.state.auth_user,
            get_user_by_id=lambda _logger, _id: {
                "id": _USER_ID,
                "username": "owner@example.test",
                # pragma: allowlist nextline secret
                "password_hash": "hash",
                "is_active": True,
            },
            verify_password=lambda plaintext, _hash: plaintext == "correct-password",
            session_token_hash=lambda value: f"hash:{value}",
            auth_cookie_name="fs_session",
            csrf_request_valid=lambda _request: state.csrf_valid,
            request_client_ip=lambda _request: "198.51.100.8",
            auth_login_lock_state=lock_state,
            auth_login_record_failure=record_failure,
            api_error_response=lambda _error: JSONResponse(
                {"error": "Internal error"}, status_code=500
            ),
            log_auth_audit_event=audit,
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("fs_session", "cookie-session-token")
    return SimpleNamespace(
        client=client,
        state=state,
        cursor=cursor,
        begin=begin,
        confirm=confirm,
        revoke=revoke,
        check_factor=check_factor,
        disable=disable,
        audit=audit,
        lock_user=lock_user,
        lock_state=lock_state,
        record_failure=record_failure,
        cookie_session=cookie_session,
    )


@pytest.mark.parametrize("source", ["pat", "machine", "external", ""])
def test_rejects_non_local_principal_despite_cookie(harness, source):
    harness.state.auth_source = source
    response = harness.client.get(f"{_BASE}/status")

    assert response.status_code == 401
    harness.cookie_session.assert_not_called()


def test_rejects_authorization_header_even_with_local_cookie(harness):
    response = harness.client.get(f"{_BASE}/status", headers={"Authorization": "Bearer other"})

    assert response.status_code == 401
    harness.cookie_session.assert_not_called()


def test_rejects_missing_or_revoked_browser_session(harness):
    harness.state.session_id = None
    response = harness.client.post(f"{_BASE}/verify", json={"code": "123456"})

    assert response.status_code == 401
    harness.confirm.assert_not_called()


def test_status_shows_installation_policy_and_enrollment_state(harness):
    harness.state.required = True
    response = harness.client.get(f"{_BASE}/status")

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "required": True}
    assert response.headers["Cache-Control"] == "no-store"


def test_requires_csrf_for_changes(harness):
    harness.state.csrf_valid = False
    response = harness.client.post(f"{_BASE}/verify", json={"code": "123456"})

    assert response.status_code == 403
    harness.confirm.assert_not_called()
    harness.cookie_session.assert_not_called()


def test_enrollment_stays_pending_and_cannot_be_cached(harness):
    response = harness.client.post(f"{_BASE}/enroll", json={"current_password": "correct-password"})

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert set(response.json()) == {"secret", "uri"}
    assert harness.begin.call_args.kwargs["session_id"] == _SESSION_ID
    harness.revoke.assert_not_called()
    assert "demo-secret" not in str(harness.audit.call_args_list)


def test_enrolled_user_must_prove_factor_to_replace_it(harness):
    harness.state.enabled = True
    response = harness.client.post(
        f"{_BASE}/enroll",
        # pragma: allowlist nextline secret
        json={"current_password": "correct-password", "current_code": "123456"},
    )

    assert response.status_code == 403
    harness.begin.assert_not_called()
    harness.check_factor.assert_called_once_with(harness.cursor, _USER_ID, "123456")
    harness.record_failure.assert_called_once()
    assert "123456" not in str(harness.audit.call_args_list)


def test_confirmation_returns_recovery_codes_and_revokes_only_other_sessions(harness):
    response = harness.client.post(f"{_BASE}/verify", json={"code": "123456"})

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"recovery_codes": ["example-recovery-code"]}
    assert harness.confirm.call_args.kwargs["session_id"] == _SESSION_ID
    harness.revoke.assert_called_once_with(harness.cursor, _USER_ID, keep_session_id=_SESSION_ID)
    assert "example-recovery-code" not in str(harness.audit.call_args_list)


@pytest.mark.parametrize("policy", ["required", "per_user_required"])
def test_disable_enforces_authenticator_policy(harness, policy):
    setattr(harness.state, policy, True)
    harness.state.enabled = True
    assert harness.client.get(f"{_BASE}/status").json() == {"enabled": True, "required": True}
    response = harness.client.post(
        f"{_BASE}/disable",
        # pragma: allowlist nextline secret
        json={"current_password": "correct-password", "current_code": "123456"},
    )

    assert response.status_code == 403
    harness.disable.assert_not_called()
    harness.check_factor.assert_not_called()


def test_optional_disable_consumes_proof_and_revokes_other_sessions(harness):
    harness.state.enabled = True
    harness.check_factor.return_value = True
    response = harness.client.post(
        f"{_BASE}/disable",
        # pragma: allowlist nextline secret
        json={"current_password": "correct-password", "current_code": "123456"},
    )

    assert response.status_code == 200
    assert response.json() == {"enabled": False}
    harness.check_factor.assert_called_once_with(harness.cursor, _USER_ID, "123456")
    harness.disable.assert_called_once_with(harness.cursor, _USER_ID)
    harness.revoke.assert_called_once_with(harness.cursor, _USER_ID, keep_session_id=_SESSION_ID)


def test_throttling_stops_before_verifying_and_uses_separate_keys(harness):
    harness.state.locked = (True, 45)
    response = harness.client.post(f"{_BASE}/verify", json={"code": "123456"})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "45"
    harness.lock_state.assert_called_once_with(
        f"local-authenticator:{_USER_ID}", "local-authenticator:198.51.100.8"
    )
    harness.confirm.assert_not_called()
