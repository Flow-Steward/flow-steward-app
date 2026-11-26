"""Reminders before an AI agent connection stops working (ADR 0076 §5).

An operator or a viewer connects an agent with one button and never chooses an expiry,
so the server tells them before the connection ends:

* in the app, every connection shows when it ends; within
  :data:`~core.application.auth.agent_access_presets.AGENT_EXPIRY_REMINDER_DAYS` days
  of that it is "ending soon", with the one action that renews it;
* by email, once per connection, when it enters that window. The control plane runs
  :meth:`AgentExpiryReminders.run_once` every :data:`REMINDER_SCAN_INTERVAL_SECONDS`.
  The reminder is recorded on the token or grant row (``expiry_reminder_sent_at``)
  before the email goes out, so a restart or a second control process never sends
  another. Without an email connection nothing is recorded or sent; the in-app
  reminder is enough, and an email connection added later still reminds once.

Every date comes from here; the browser only formats them.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from core.application.auth import agent_connection_repository as repo
from core.application.auth.agent_access_presets import AGENT_EXPIRY_REMINDER_DAYS
from core.application.auth.agent_connection_catalog import CLIENTS
from core.application.auth.inbound_oauth_policy import current_oauth_environment
from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.public_identity import public_origin

logger = logging.getLogger(__name__)

REMINDER_WINDOW = timedelta(days=AGENT_EXPIRY_REMINDER_DAYS)
#: How often the control plane looks for connections entering the reminder window.
REMINDER_SCAN_INTERVAL_SECONDS = 3600.0
#: At most this many reminders per pass; the rest go out on the next one.
REMINDER_BATCH_LIMIT = 200
#: The AI agents page, where every connection and its end date is shown.
AGENTS_PAGE_PATH = "/settings/account?tab=ai-agents"

#: How the owner keeps a connection working: a fresh token from the wizard, or the
#: OAuth consent again, which only the cloud client itself can start.
RENEW_CONNECT_AGAIN = "connect_again"
RENEW_RECONNECT_FROM_CLIENT = "reconnect_from_client"


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_ending_soon(*, created_at: Any, expires_at: Any, now: datetime) -> bool:
    """Whether a connection that still works ends within the reminder window.

    A connection made for the window's length or less is never "ending soon": its owner
    chose that short a life.
    """
    ends = _aware(expires_at)
    if ends is None or ends <= now:
        return False
    started = _aware(created_at)
    if started is not None and ends - started <= REMINDER_WINDOW:
        return False
    return ends - now <= REMINDER_WINDOW


def days_left(expires_at: Any, now: datetime) -> int | None:
    """Whole days until the end, rounded up: "ends in 1 day" until the very end."""
    ends = _aware(expires_at)
    if ends is None:
        return None
    return max(0, math.ceil((ends - now) / timedelta(days=1)))


def _token_client(label: str) -> tuple[str, str]:
    """``(client id, client label)`` of a wizard token from its ``AI agent · X`` label."""
    name = label[len(repo.AGENT_TOKEN_LABEL_PREFIX) :].strip()
    client = next((item for item in CLIENTS if item.label == name), None)
    return (client.id if client else "", name or label)


def connection_view(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """One connection as the AI agents page and the app shell notice show it."""
    kind = str(row.get("kind") or "")
    label = str(row.get("label") or "")
    if kind == repo.KIND_PERSONAL_TOKEN:
        client_id, client_label = _token_client(label)
        renew = RENEW_CONNECT_AGAIN
    else:
        client_id, client_label = "", label or str(row.get("client_id") or "")
        renew = RENEW_RECONNECT_FROM_CLIENT
    expires_at = _aware(row.get("expires_at"))

    def iso(value: Any) -> str | None:
        moment = _aware(value)
        return moment.isoformat() if moment else None

    return {
        "id": str(row.get("id") or ""),
        "kind": kind,
        "client_id": client_id,
        "client_label": client_label,
        "created_at": iso(row.get("created_at")),
        "ends_at": iso(expires_at),
        "last_used_at": iso(row.get("last_used_at")),
        "days_left": days_left(expires_at, now),
        "ending_soon": is_ending_soon(
            created_at=row.get("created_at"), expires_at=expires_at, now=now
        ),
        "renew": renew,
    }


def agents_page_url() -> str:
    """The AI agents page at the installation's public address, or the bare path."""
    origin = public_origin(current_oauth_environment().as_identity_mapping())
    return f"{origin.rstrip('/')}{AGENTS_PAGE_PATH}" if origin else AGENTS_PAGE_PATH


def _default_send(**kwargs: Any) -> dict[str, Any]:
    from core.application.extensions.system_email import send_agent_connection_expiring_email

    return send_agent_connection_expiring_email(**kwargs)


def _default_email_available() -> bool:
    from core.application.extensions.system_email import system_email_available

    return system_email_available()


@dataclass
class AgentExpiryReminders:
    """One pass of the expiry reminder over the store ``bl`` (anything with ``conn``)."""

    bl: Any
    send_email: Callable[..., Mapping[str, Any]] = _default_send
    email_available: Callable[[], bool] = _default_email_available
    agents_url: Callable[[], str] = agents_page_url
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    batch_limit: int = REMINDER_BATCH_LIMIT

    def run_once(self) -> dict[str, int]:
        """Send each due reminder once; returns counts for the control loop's log."""
        summary = {"due": 0, "sent": 0, "skipped": 0, "failed": 0, "no_email": 0}
        if not self.email_available():
            summary["no_email"] = 1
            return summary
        now = self.clock()
        with transaction_connection(self.bl.conn) as conn:
            due = repo.list_reminder_candidates(
                conn, now=now, window=REMINDER_WINDOW, limit=self.batch_limit
            )
        summary["due"] = len(due)
        url = self.agents_url() if due else ""
        for row in due:
            outcome = self._remind(row, now=now, url=url)
            if outcome == "no_email":
                summary["no_email"] = 1
                break
            summary[outcome] += 1
        return summary

    def _remind(self, row: Mapping[str, Any], *, now: datetime, url: str) -> str:
        kind, connection_id = str(row["kind"]), str(row["id"])
        with transaction_connection(self.bl.conn) as conn:
            if not repo.claim_reminder(conn, kind, connection_id, now=now):
                return "skipped"  # another pass reminded it already
        view = connection_view(row, now)
        ends = _aware(row.get("expires_at")) or now
        try:
            result = self.send_email(
                recipient=str(row.get("username") or ""),
                client=view["client_label"],
                end_date=ends.date().isoformat(),
                agents_url=url,
                oauth_app=kind == repo.KIND_OAUTH_GRANT,
                display_name=str(row.get("display_name") or row.get("username") or ""),
                locale=str(row.get("locale") or ""),
            )
        except Exception:
            # Recorded as sent: one reminder at most, never a repeat after a crash.
            logger.warning("Agent expiry reminder could not be sent for %s %s", kind, connection_id)
            return "failed"
        if result.get("ok"):
            return "sent"
        if str(result.get("reason") or "") == "disabled_or_unconfigured":
            # The email connection went away during the pass: nothing was sent, so the
            # reminder stays due for when one is configured.
            with transaction_connection(self.bl.conn) as conn:
                repo.release_reminder(conn, kind, connection_id, claimed_at=now)
            return "no_email"
        return "failed"


def run_agent_expiry_reminders(bl: Any) -> dict[str, int]:
    """Entry point of the control-plane loop."""
    return AgentExpiryReminders(bl).run_once()


__all__ = [
    "AGENTS_PAGE_PATH",
    "REMINDER_BATCH_LIMIT",
    "REMINDER_SCAN_INTERVAL_SECONDS",
    "REMINDER_WINDOW",
    "RENEW_CONNECT_AGAIN",
    "RENEW_RECONNECT_FROM_CLIENT",
    "AgentExpiryReminders",
    "agents_page_url",
    "connection_view",
    "days_left",
    "is_ending_soon",
    "run_agent_expiry_reminders",
]
