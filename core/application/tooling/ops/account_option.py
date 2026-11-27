"""The ``--account`` option of the registry operations commands (ADR 0020, step C5).

Registry data is kept per account, so every operations command works on one
account. The command line names it with ``--account``; without it the command
works on the installation account and says so in its output. The library code a
command calls never picks an account by itself: it is handed the one resolved
here.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID


@dataclass(frozen=True)
class CommandAccount:
    """The account a command works on and whether the command line named it."""

    account_id: str
    named: bool

    def describe(self) -> str:
        if self.named:
            return f"account: {self.account_id}"
        return (
            f"account: {self.account_id} (the installation account; "
            "pass --account to work on another account)"
        )

    def as_payload(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "account_named": self.named}


def add_account_argument(parser: argparse.ArgumentParser, *, purpose: str) -> None:
    """Add ``--account`` (also accepted as ``--account-id``) to *parser*."""
    parser.add_argument(
        "--account",
        "--account-id",
        dest="account",
        default="",
        help=f"Account {purpose} (default: the installation account, stated in the output).",
    )


def resolve_command_account(args: argparse.Namespace) -> CommandAccount:
    """The account named on the command line, else the installation account."""
    named = str(getattr(args, "account", "") or "").strip()
    if named:
        return CommandAccount(account_id=named, named=True)
    return CommandAccount(account_id=INSTALLATION_ACCOUNT_ID, named=False)


def report_command_account(account: CommandAccount, *, stream: TextIO | None = None) -> None:
    """State the account the command works on, on stderr so JSON output stays clean."""
    print(account.describe(), file=stream or sys.stderr)


def require_account(account_id: Any, *, operation: str) -> str:
    """Return *account_id*, or refuse a library call that names no account."""
    owner = str(account_id or "").strip()
    if not owner:
        raise ValueError(f"{operation} needs an account; the command passes the one it works on")
    return owner


__all__ = [
    "CommandAccount",
    "add_account_argument",
    "report_command_account",
    "require_account",
    "resolve_command_account",
]
