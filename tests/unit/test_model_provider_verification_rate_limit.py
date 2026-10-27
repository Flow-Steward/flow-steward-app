from __future__ import annotations

import contextlib
import json

from core.application.settings import model_provider_verification_rate_limit as rate_limit


class _Cursor:
    def __init__(self, state: dict[str, list[float]]):
        self._state = state
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, params: tuple):
        normalized = " ".join(sql.split())
        if "SELECT attempts_jsonb" in normalized:
            self._row = {"attempts_jsonb": list(self._state.get(params[0], []))}
        elif "INSERT INTO public.auth_rate_limits" in normalized:
            self._state[params[0]] = json.loads(params[1])

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self):
        self.state: dict[str, list[float]] = {}

    def cursor(self):
        return _Cursor(self.state)


def test_paid_verification_limit_is_shared_per_account_and_isolates_other_accounts(
    monkeypatch,
) -> None:
    conn = _Connection()
    monkeypatch.setattr(
        rate_limit,
        "transaction_connection",
        lambda connection: contextlib.nullcontext(connection),
    )
    monkeypatch.setattr(rate_limit, "MODEL_PROVIDER_VERIFY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(rate_limit, "MODEL_PROVIDER_VERIFY_RATE_WINDOW_SECONDS", 60)

    assert rate_limit.consume_model_provider_verification_limit(
        conn, account_id="account-a", provider_id="provider-1", now=100
    ) == (True, 0)
    assert rate_limit.consume_model_provider_verification_limit(
        conn, account_id="account-a", provider_id="provider-1", now=101
    ) == (True, 0)

    assert rate_limit.consume_model_provider_verification_limit(
        conn, account_id="account-a", provider_id="provider-2", now=102
    ) == (False, 58)
    assert rate_limit.consume_model_provider_verification_limit(
        conn, account_id="account-b", provider_id="provider-1", now=102
    ) == (True, 0)
