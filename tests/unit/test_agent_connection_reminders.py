"""ADR 0076 §5: the owner is reminded before an AI agent connection stops working.

The reminder rules run for real; the SQL is replaced by an in-memory table (the
PostgreSQL suite ``tests/integration/test_agent_connection_postgres.py`` runs the same
pass on the real tables).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.application.auth import agent_connection_reminders as reminders
from core.application.auth.agent_connection_catalog import CLAUDE_CODE_LABEL
from core.application.auth.agent_connection_repository import (
    KIND_OAUTH_GRANT,
    KIND_PERSONAL_TOKEN,
)
from core.application.extensions import system_email

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def _row(kind: str, row_id: str, *, created_days_ago: float, ends_in_days: float, **extra):
    return {
        "kind": kind,
        "id": row_id,
        "label": f"AI agent · {CLAUDE_CODE_LABEL}" if kind == KIND_PERSONAL_TOKEN else "ChatGPT",
        "created_at": NOW - created_days_ago * DAY,
        "expires_at": NOW + ends_in_days * DAY,
        "username": "anna@example.test",
        "display_name": "Anna",
        "locale": "lt",
        "sent_at": None,
        **extra,
    }


# --- the window ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("created_days_ago", "ends_in_days", "expected"),
    [
        (85, 5, True),
        (83, 7, True),
        (82.9, 7.1, False),  # not yet within seven days
        (89.9, 0.1, True),
        (90, -0.1, False),  # already ended
        (2, 5, False),  # a seven-day connection: its owner chose so short a life
        (0, 7, False),
    ],
)
def test_ending_soon_is_the_last_seven_days_of_a_longer_connection(
    created_days_ago, ends_in_days, expected
) -> None:
    assert (
        reminders.is_ending_soon(
            created_at=NOW - created_days_ago * DAY,
            expires_at=NOW + ends_in_days * DAY,
            now=NOW,
        )
        is expected
    )


def test_days_left_round_up_and_never_go_negative() -> None:
    assert reminders.days_left(NOW + 6.2 * DAY, NOW) == 7
    assert reminders.days_left(NOW + 1 * DAY, NOW) == 1
    assert reminders.days_left(NOW - DAY, NOW) == 0
    assert reminders.days_left(None, NOW) is None


def test_a_connection_says_when_it_ends_and_how_it_is_renewed() -> None:
    token = reminders.connection_view(
        _row(KIND_PERSONAL_TOKEN, "tok-1", created_days_ago=85, ends_in_days=5), NOW
    )
    assert token["client_id"] == "claude_code"
    assert token["client_label"] == CLAUDE_CODE_LABEL
    assert token["ends_at"] == (NOW + 5 * DAY).isoformat()
    assert token["days_left"] == 5
    assert token["ending_soon"] is True
    assert token["renew"] == reminders.RENEW_CONNECT_AGAIN

    grant = reminders.connection_view(
        _row(KIND_OAUTH_GRANT, "grant-1", created_days_ago=10, ends_in_days=80), NOW
    )
    assert grant["client_id"] == ""
    assert grant["client_label"] == "ChatGPT"
    assert grant["ending_soon"] is False
    assert grant["renew"] == reminders.RENEW_RECONNECT_FROM_CLIENT


def test_the_agents_page_link_uses_the_public_address(monkeypatch) -> None:
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://fs.example.com/")
    assert reminders.agents_page_url() == "https://fs.example.com/settings/account?tab=ai-agents"
    monkeypatch.delenv("FS_PUBLIC_BASE_URL")
    assert reminders.agents_page_url() == "/settings/account?tab=ai-agents"


# --- one email per connection ----------------------------------------------------------


class _Table:
    """The token and grant rows with their reminder column."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {row["id"]: row for row in rows}

    def candidates(self, _conn, *, now, window, limit):
        due = [
            dict(row)
            for row in self.rows.values()
            if row["sent_at"] is None
            and now < row["expires_at"] <= now + window
            and row["expires_at"] - row["created_at"] > window
        ]
        return sorted(due, key=lambda row: row["expires_at"])[:limit]

    def claim(self, _conn, _kind, connection_id, *, now):
        row = self.rows[connection_id]
        if row["sent_at"] is not None:
            return False
        row["sent_at"] = now
        return True

    def release(self, _conn, _kind, connection_id, *, claimed_at):
        row = self.rows[connection_id]
        if row["sent_at"] == claimed_at:
            row["sent_at"] = None


@pytest.fixture
def table(monkeypatch):
    rows = _Table(
        [
            _row(KIND_PERSONAL_TOKEN, "tok-due", created_days_ago=85, ends_in_days=5),
            _row(KIND_OAUTH_GRANT, "grant-due", created_days_ago=23, ends_in_days=6.5),
            _row(KIND_PERSONAL_TOKEN, "tok-later", created_days_ago=10, ends_in_days=80),
            _row(KIND_PERSONAL_TOKEN, "tok-short", created_days_ago=1, ends_in_days=6),
        ]
    )

    @contextlib.contextmanager
    def _transaction(_conn):
        yield object()

    monkeypatch.setattr(reminders, "transaction_connection", _transaction)
    monkeypatch.setattr(reminders.repo, "list_reminder_candidates", rows.candidates)
    monkeypatch.setattr(reminders.repo, "claim_reminder", rows.claim)
    monkeypatch.setattr(reminders.repo, "release_reminder", rows.release)
    return rows


class _Store:
    conn = object()


def _pass(send, *, available=True, clock=lambda: NOW) -> reminders.AgentExpiryReminders:
    return reminders.AgentExpiryReminders(
        _Store(),
        send_email=send,
        email_available=lambda: available,
        agents_url=lambda: "https://fs.example.com/settings/account?tab=ai-agents",
        clock=clock,
    )


def test_each_connection_entering_the_window_is_reminded_once(table) -> None:
    sent: list[dict[str, Any]] = []

    def send(**values):
        sent.append(values)
        return {"ok": True, "reason": "sent"}

    first = _pass(send).run_once()
    assert first == {"due": 2, "sent": 2, "skipped": 0, "failed": 0, "no_email": 0}
    assert [item["client"] for item in sent] == [CLAUDE_CODE_LABEL, "ChatGPT"]
    token, grant = sent
    assert token == {
        "recipient": "anna@example.test",
        "client": CLAUDE_CODE_LABEL,
        "end_date": (NOW + 5 * DAY).date().isoformat(),
        "agents_url": "https://fs.example.com/settings/account?tab=ai-agents",
        "oauth_app": False,
        "display_name": "Anna",
        "locale": "lt",
    }
    assert grant["oauth_app"] is True
    # A restart, or the next hour's pass, finds nothing more to send.
    assert _pass(send).run_once()["due"] == 0
    assert len(sent) == 2
    assert table.rows["tok-later"]["sent_at"] is None
    assert table.rows["tok-short"]["sent_at"] is None


def test_without_an_email_connection_nothing_is_sent_or_recorded(table) -> None:
    def send(**_values):
        raise AssertionError("no email connection: nothing may be sent")

    assert _pass(send, available=False).run_once()["no_email"] == 1
    assert all(row["sent_at"] is None for row in table.rows.values())


def test_an_email_connection_removed_mid_pass_leaves_the_reminder_due(table) -> None:
    calls: list[str] = []

    def send(**values):
        calls.append(values["client"])
        return {"ok": False, "reason": "disabled_or_unconfigured"}

    summary = _pass(send).run_once()
    assert summary["no_email"] == 1
    assert calls == [CLAUDE_CODE_LABEL]  # the pass stops at the first refusal
    assert table.rows["tok-due"]["sent_at"] is None


def test_a_failed_send_is_not_repeated(table) -> None:
    def send(**_values):
        return {"ok": False, "reason": "send_failed"}

    assert _pass(send).run_once()["failed"] == 2

    def boom(**_values):
        raise RuntimeError("smtp down")

    assert _pass(boom).run_once()["due"] == 0
    assert table.rows["tok-due"]["sent_at"] == NOW


def test_a_reminder_another_pass_took_is_skipped(table, monkeypatch) -> None:
    def claim(conn, kind, connection_id, *, now):
        if connection_id == "grant-due":
            table.rows["grant-due"]["sent_at"] = now - DAY  # the other process won
        return table.claim(conn, kind, connection_id, now=now)

    monkeypatch.setattr(reminders.repo, "claim_reminder", claim)
    sent: list[str] = []
    summary = _pass(lambda **values: sent.append(values["client"]) or {"ok": True}).run_once()
    assert summary["skipped"] == 1
    assert sent == [CLAUDE_CODE_LABEL]


# --- the email text ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("oauth_app", "locale", "subject", "renew"),
    [
        (False, "en", "Your AI agent connection stops working soon", "choose Connect again"),
        (True, "en", "Your AI app connection stops working soon", "connect Flow Steward again"),
        (False, "lt", "Jūsų DI agento ryšys netrukus nustos veikti", "„Prijungti iš naujo“"),
        (True, "lt", "Jūsų DI programos ryšys netrukus nustos veikti", "iš naujo prijunkite"),
    ],
)
def test_the_reminder_email_names_the_client_the_date_and_the_renewal(
    monkeypatch, oauth_app, locale, subject, renew
) -> None:
    delivered: list[dict[str, Any]] = []
    from core.application.extensions import system_email_sender
    from core.application.notifications import delivery

    # The confirmed system email sender (ADR 0081) is the only connection that sends.
    connection = {
        "connection_id": "mail",
        "extension_id": "acme.mail",
        "extension_install_id": "install-mail",
        "connection_type": "example_mail",
        "sender_revision": 1,
    }
    monkeypatch.setattr(
        system_email_sender,
        "sender_status",
        lambda _bl: system_email_sender.SenderStatus(
            available=True,
            setting=system_email_sender.SenderSetting(
                connection_id="mail",
                extension_install_id="install-mail",
                connection_type="example_mail",
                sender_revision=1,
            ),
            connection=connection,
            extension={"extension_key": "acme.mail", "extension_install_id": "install-mail"},
        ),
    )
    monkeypatch.setattr(
        delivery,
        "build_notification_connection_target",
        lambda _bl, target: ({"connection_id": target["connection_id"]},) * 2,
    )
    monkeypatch.setattr(
        delivery,
        "deliver_direct_notification",
        lambda _bl, **values: delivered.append(values) or {"status": "delivered"},
    )
    result = system_email.send_agent_connection_expiring_email(
        recipient="Anna@Example.test",
        client=CLAUDE_CODE_LABEL,
        end_date="2026-11-25",
        agents_url="https://fs.example.com/settings/account?tab=ai-agents",
        oauth_app=oauth_app,
        display_name="Anna",
        locale=locale,
        bl=object(),
    )
    assert result["ok"] is True
    [message] = delivered
    assert message["subject"] == subject
    assert message["recipients"] == ["anna@example.test"]
    assert CLAUDE_CODE_LABEL in message["message"]
    assert "2026-11-25" in message["message"]
    assert "https://fs.example.com/settings/account?tab=ai-agents" in message["message"]
    assert renew in message["message"]


def test_the_control_plane_runs_the_reminder_on_its_own_cadence() -> None:
    from unittest.mock import MagicMock, patch

    from core.entrypoints.cli.control_process import _build_control_scan_fns
    from core.infrastructure.runtime.control.config import ControlConfig

    stores = {"connection": MagicMock()}
    with patch.object(
        reminders, "run_agent_expiry_reminders", return_value={"sent": 1}
    ) as run_pass:
        scan_fns = _build_control_scan_fns(
            stores=stores, cfg=ControlConfig.from_env(), artifact_plane=object()
        )
        assert scan_fns["agent_expiry_reminders"]() == {"sent": 1}
    run_pass.assert_called_once_with(stores["connection"])
    assert reminders.REMINDER_SCAN_INTERVAL_SECONDS == 3600.0
