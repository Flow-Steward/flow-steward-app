from __future__ import annotations

from typing import Any

import pytest

from core.application.tooling.testing import fuzz_common


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = "response body"

    def json(self) -> dict[str, Any]:
        return self._payload


class _MultiAccountSession:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.login_payloads: list[dict[str, str]] = []

    def post(self, _url: str, **kwargs: Any) -> _Response:
        payload = dict(kwargs["json"])
        self.login_payloads.append(payload)
        if "account_id" not in payload:
            return _Response(
                409,
                {
                    "requires_account_selection": True,
                    "account_options": [
                        {
                            "account_id": "account-other",
                            "is_active": True,
                            "is_default": False,
                        },
                        {
                            "account_id": "account-default",
                            "is_active": True,
                            "is_default": True,
                        },
                    ],
                },
            )
        self.cookies["fs_session"] = "session-token"
        return _Response(200, {"status": "authenticated"})


def test_fuzz_login_selects_the_single_default_active_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _MultiAccountSession()
    monkeypatch.setattr(fuzz_common.requests, "Session", lambda: session)

    authenticated, token = fuzz_common.login_session(
        base_url="https://flow.test",
        verify=False,
        username="admin@example.test",
        password="password",
        timeout=15,
    )

    assert authenticated is session
    assert token == "session-token"
    assert session.login_payloads == [
        {"email": "admin@example.test", "password": "password"},
        {
            "email": "admin@example.test",
            "password": "password",
            "account_id": "account-default",
        },
    ]
