from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.application.auth.bootstrap_tokens import (
    BootstrapTokenActivation,
    BootstrapTokenService,
    bootstrap_token_request_valid,
)
from core.application.management.errors import ServicePreconditionError


class FakeRuntime:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.expiries: list[datetime] = []

    def activate(self, *, token: str, expires_at: datetime) -> BootstrapTokenActivation:
        self.tokens.append(token)
        self.expiries.append(expires_at)
        return BootstrapTokenActivation(secret_path=".flow-steward/secret-output/bootstrap-token")


def test_issue_token_has_256_bits_expires_in_60_minutes_and_returns_only_path() -> None:
    runtime = FakeRuntime()
    now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
    service = BootstrapTokenService(user_count=lambda: 0, runtime=runtime, now=lambda: now)

    result = service.issue()

    assert len(runtime.tokens) >= 1
    assert len(runtime.tokens[0]) >= 43
    assert runtime.expiries == [now + timedelta(minutes=60)]
    assert result.output_path.endswith("bootstrap-token")
    assert runtime.tokens[0] not in result.model_dump_json()


def test_reissue_replaces_the_single_active_runtime_token() -> None:
    runtime = FakeRuntime()
    service = BootstrapTokenService(user_count=lambda: 0, runtime=runtime)

    service.issue()
    service.issue()

    assert len(runtime.tokens) == 2
    assert runtime.tokens[0] != runtime.tokens[1]


def test_issue_token_requires_zero_users() -> None:
    service = BootstrapTokenService(user_count=lambda: 1, runtime=FakeRuntime())

    with pytest.raises(ServicePreconditionError):
        service.issue()


def test_request_validation_fails_closed_for_expired_or_missing_expiry() -> None:
    now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
    future = (now + timedelta(minutes=1)).isoformat()
    expired = (now - timedelta(seconds=1)).isoformat()

    assert bootstrap_token_request_valid(
        "secret", required="secret", expires_at=future, now=now
    ) == (True, "")
    assert bootstrap_token_request_valid(
        "secret", required="secret", expires_at=expired, now=now
    ) == (False, "Bootstrap setup token has expired")
    assert bootstrap_token_request_valid("secret", required="secret", expires_at="", now=now) == (
        False,
        "Bootstrap setup token expiry is missing or invalid",
    )
    assert bootstrap_token_request_valid(
        "wrong", required="secret", expires_at=future, now=now
    ) == (False, "Invalid bootstrap setup token")


def test_hashed_state_invalidates_previous_token_immediately(tmp_path: Path) -> None:
    from core.application.auth.bootstrap_tokens import bootstrap_token_state_request_valid

    state_path = tmp_path / "bootstrap-token-state.json"
    state_path.write_text(
        '{"token_sha256":"348e9df2a42bd6e3c6356ca9c95c5f1fe9a6b3e5cd25f4ae58df0f09049c3209",'
        '"expires_at":"2999-01-01T00:00:00+00:00"}',
        encoding="utf-8",
    )

    assert bootstrap_token_state_request_valid("new-token", state_path=state_path) == (True, "")
    assert bootstrap_token_state_request_valid("old-token", state_path=state_path) == (
        False,
        "Invalid bootstrap setup token",
    )


@pytest.mark.parametrize("count", [False, None, -1, "0", 0.0])
def test_issue_token_rejects_invalid_trusted_count(count: object) -> None:
    runtime = FakeRuntime()
    service = BootstrapTokenService(user_count=lambda: count, runtime=runtime)
    with pytest.raises(ServicePreconditionError):
        service.issue()
    assert runtime.tokens == []
