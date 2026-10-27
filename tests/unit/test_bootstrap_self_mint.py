"""AC17-R117: a runtime nobody installed has to be able to make its first admin."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.application.auth.bootstrap_self_mint import (
    SELF_MINTED_TOKEN_TTL,
    self_mint_bootstrap_link,
)
from core.application.auth.bootstrap_tokens import bootstrap_token_state_request_valid

_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _unmanaged_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment of an image started by something that is not our installer."""
    for name in (
        "FS_AUTH_BOOTSTRAP_TOKEN",
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT",
        "FS_AUTH_BOOTSTRAP_STATE_FILE",
        "FS_AUTH_BOOTSTRAP_ENABLED",
        "FS_LOCAL_AUTH_ENABLED",
        "FS_PUBLIC_BASE_URL",
        "FS_AUTH_TRUSTED_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)


def _write_state(path: Path, *, token: str, expires_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "expires_at": expires_at.isoformat(),
                "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            }
        ),
        encoding="utf-8",
    )


def test_an_installation_nobody_installed_gets_a_link_it_can_actually_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point, proven against the validator the request path really uses.

    Without this, the only screen such an installation has answers 403 to the only
    person who could ever create the first administrator, and every command that
    would issue a token is an installer command that runs somewhere else.
    """
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://flow.example.eu/")
    state_path = tmp_path / "bootstrap-token-state.json"

    outcome = self_mint_bootstrap_link(
        state_path=state_path,
        user_count=lambda: 0,
        now=lambda: _NOW,
    )

    assert outcome.minted
    assert outcome.link == f"https://flow.example.eu/bootstrap#setup-token={outcome.token}"
    assert outcome.expires_at == _NOW + SELF_MINTED_TOKEN_TTL

    # The state the runtime would write out of this is what the endpoint validates.
    _write_state(state_path, token=outcome.token, expires_at=outcome.expires_at)
    assert bootstrap_token_state_request_valid(
        outcome.token, state_path=state_path, now=_NOW
    ) == (True, "")
    valid, message = bootstrap_token_state_request_valid(
        "some-other-token", state_path=state_path, now=_NOW
    )
    assert not valid
    assert message == "Invalid bootstrap setup token"


def test_the_link_lives_for_minutes_rather_than_the_installer_hour() -> None:
    """A secret written to a log stream is not a secret read from a terminal.

    The installer's token is printed where the operator is sitting; this one goes
    into logs that get shipped, retained and read later. Being locked out costs one
    restart, because the mint runs again on every start while no administrator
    exists -- which is also what invalidates a link nobody used.
    """
    assert SELF_MINTED_TOKEN_TTL == timedelta(minutes=15)


def test_a_runtime_with_no_public_url_still_gets_the_whole_secret(tmp_path: Path) -> None:
    """Withholding the link because we cannot name the host helps nobody.

    The path and the fragment are the entire secret and the operator knows their own
    address; the installations this exists for are exactly the ones least likely to
    have been told their public URL.
    """
    outcome = self_mint_bootstrap_link(
        state_path=tmp_path / "state.json",
        user_count=lambda: 0,
        public_base_url=lambda: "",
        now=lambda: _NOW,
    )

    assert outcome.link == f"/bootstrap#setup-token={outcome.token}"


def test_an_installer_managed_installation_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Minting beside a configured token would invalidate the link the operator holds."""
    monkeypatch.setenv("FS_AUTH_BOOTSTRAP_TOKEN", "installer-minted")

    outcome = self_mint_bootstrap_link(
        state_path=tmp_path / "state.json",
        user_count=lambda: 0,
        now=lambda: _NOW,
    )

    assert not outcome.minted
    assert "already configured" in outcome.declined_because


def test_a_link_issued_earlier_is_not_replaced_while_it_still_works(tmp_path: Path) -> None:
    """Restarting during the operator's own setup must not pull the link out from under them."""
    state_path = tmp_path / "state.json"
    _write_state(state_path, token="live", expires_at=_NOW + timedelta(minutes=5))

    outcome = self_mint_bootstrap_link(
        state_path=state_path,
        user_count=lambda: 0,
        now=lambda: _NOW,
    )

    assert not outcome.minted
    assert "still valid" in outcome.declined_because


def test_an_expired_link_is_replaced_rather_than_left_as_a_dead_end(tmp_path: Path) -> None:
    """Otherwise the short lifetime becomes a permanent lockout with no way out.

    "Readable state" has to mean usable state: an expired file is as useless to the
    operator as no file, and the installer settled the same question the same way --
    an installed rerun replaces an expired setup token instead of advising around it.
    """
    state_path = tmp_path / "state.json"
    _write_state(state_path, token="stale", expires_at=_NOW - timedelta(seconds=1))

    outcome = self_mint_bootstrap_link(
        state_path=state_path,
        user_count=lambda: 0,
        public_base_url=lambda: "https://flow.example.eu",
        now=lambda: _NOW,
    )

    assert outcome.minted
    assert outcome.token not in {"stale", ""}


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ("not json at all", "unparseable"),
        ('{"expires_at": "2099-01-01T00:00:00Z"}', "no token hash"),
        ('{"token_sha256": "", "expires_at": "2099-01-01T00:00:00Z"}', "empty token hash"),
        ('{"token_sha256": "abc", "expires_at": "whenever"}', "unreadable expiry"),
    ],
)
def test_state_that_cannot_be_used_is_treated_as_no_state(
    tmp_path: Path, payload: str, why: str
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(payload, encoding="utf-8")

    outcome = self_mint_bootstrap_link(
        state_path=state_path,
        user_count=lambda: 0,
        public_base_url=lambda: "",
        now=lambda: _NOW,
    )

    assert outcome.minted, why


def test_nothing_is_minted_once_the_installation_has_its_administrator(tmp_path: Path) -> None:
    outcome = self_mint_bootstrap_link(
        state_path=tmp_path / "state.json",
        user_count=lambda: 1,
        now=lambda: _NOW,
    )

    assert not outcome.minted
    assert "already has its first administrator" in outcome.declined_because


@pytest.mark.parametrize(
    ("variable", "expected"),
    [
        ("FS_LOCAL_AUTH_ENABLED", "local authentication is disabled"),
        ("FS_AUTH_BOOTSTRAP_ENABLED", "first-administrator bootstrap is disabled"),
    ],
)
def test_an_operator_who_switched_bootstrap_off_stays_switched_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str, expected: str
) -> None:
    """Self-minting is a recovery from a dead end, never an override of a decision."""
    monkeypatch.setenv(variable, "0")

    outcome = self_mint_bootstrap_link(
        state_path=tmp_path / "state.json",
        user_count=lambda: 0,
        now=lambda: _NOW,
    )

    assert not outcome.minted
    assert outcome.declined_because == expected


def test_a_database_that_cannot_answer_declines_and_says_so(tmp_path: Path) -> None:
    """Minting on an unreadable count would issue a link to an installation that has admins."""

    def unavailable() -> int:
        raise RuntimeError("connection refused")

    outcome = self_mint_bootstrap_link(
        state_path=tmp_path / "state.json",
        user_count=unavailable,
        now=lambda: _NOW,
    )

    assert not outcome.minted
    assert "connection refused" in outcome.declined_because


def test_two_runtimes_never_mint_the_same_token(tmp_path: Path) -> None:
    tokens = {
        self_mint_bootstrap_link(
            state_path=tmp_path / f"{index}.json",
            user_count=lambda: 0,
            public_base_url=lambda: "",
            now=lambda: _NOW,
        ).token
        for index in range(8)
    }

    assert len(tokens) == 8
    assert all(len(token) >= 32 for token in tokens)


def test_the_remote_operator_who_had_no_way_in_now_has_exactly_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The defect and the fix, both measured through the gate that really decides.

    Before: no token, no state, and FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY at its default,
    so the one screen the installation has refuses the one person who could use it.
    After: the same request carrying the minted token is let through, and every other
    request still is not. The localhost default is untouched either way -- a
    self-minted installation is harder to reach than one relying on that rule, not
    easier.
    """
    from types import SimpleNamespace

    from core.application.auth import serve_auth_runtime

    def remote(headers: dict[str, str]) -> SimpleNamespace:
        return SimpleNamespace(
            headers=headers,
            cookies={},
            client=SimpleNamespace(host="203.0.113.7"),
        )

    # Every flag at its shipped default: bootstrap on, localhost-only on, no trusted
    # proxy. Nothing here relaxes the configuration the defect is measured under.
    def default_flags(_name: str, default: bool = False) -> bool:
        return default

    state_path = tmp_path / "bootstrap-token-state.json"
    locked_out = serve_auth_runtime.bootstrap_request_allowed(
        remote({}),
        env_flag=default_flags,
        bootstrap_token_header="x-platform-setup-token",
    )
    assert locked_out == (False, "Bootstrap is restricted to localhost requests")

    outcome = self_mint_bootstrap_link(
        state_path=state_path,
        user_count=lambda: 0,
        public_base_url=lambda: "https://flow.example.eu",
    )
    assert outcome.minted
    assert outcome.expires_at is not None
    _write_state(state_path, token=outcome.token, expires_at=outcome.expires_at)
    monkeypatch.setenv("FS_AUTH_BOOTSTRAP_STATE_FILE", str(state_path))

    allowed = serve_auth_runtime.bootstrap_request_allowed(
        remote({"x-platform-setup-token": outcome.token}),
        env_flag=default_flags,
        bootstrap_token_header="x-platform-setup-token",
    )
    still_refused = serve_auth_runtime.bootstrap_request_allowed(
        remote({}),
        env_flag=default_flags,
        bootstrap_token_header="x-platform-setup-token",
    )

    assert allowed == (True, "")
    assert still_refused == (False, "Invalid bootstrap setup token")
