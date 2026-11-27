"""Extra invitation limits platform modules may add (ADR 0085, P4 extension).

Core counts every invitation against its own limits (per administrator, per account,
per recipient; :mod:`member_lookup_throttle`). A platform module may add stricter
limits for some accounts, for example accounts made by self-service sign-up until the
installation administrator trusts them, so a new account cannot use the system email
to send many invitations:

    register_invitation_limits(lambda account_id: [("my-key:" + account_id, 5, 86_400)])

A policy returns ``(key, max_attempts, window_seconds)`` triples; Core counts the
invitation against them in the same shared store and transaction as its own, and an
invitation over any of them is refused like Core's own limits. A policy that fails
refuses the invitation (fails closed). The registry is process-wide and rolled back
by the platform module loader when a module fails to load.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from core.application.auth.user_admin.member_lookup_throttle import ThrottleLimit

_LOG = logging.getLogger(__name__)

InvitationLimitPolicy = Callable[[str], Iterable[tuple[str, int, int]]]
_POLICIES: list[InvitationLimitPolicy] = []


class InvitationPolicyUnavailable(RuntimeError):
    """A module's invitation limit could not be decided."""


def register_invitation_limits(policy: InvitationLimitPolicy) -> None:
    if callable(policy) and policy not in _POLICIES:
        _POLICIES.append(policy)


def extra_invitation_limits(account_id: str) -> list[ThrottleLimit]:
    limits: list[ThrottleLimit] = []
    for policy in list(_POLICIES):
        try:
            for key, max_attempts, window in policy(str(account_id or "")) or ():
                text = str(key or "").strip()
                if text and int(max_attempts) >= 1 and int(window) > 0:
                    limits.append(ThrottleLimit(f"module:{text}", int(max_attempts), int(window)))
        except Exception as exc:
            _LOG.warning("An invitation limit policy failed: %s", type(exc).__name__)
            raise InvitationPolicyUnavailable("invitation limits unavailable") from exc
    return limits


@dataclass(frozen=True)
class NewcomerSource:
    """A module that creates people only to answer an invitation.

    ``is_newcomer(user_id)`` says whether the module created this person for an
    invitation (a durable record of its own, which survives the person setting a
    password); ``identity_methods`` are the sign-in method kinds whose identity links
    move with the person into the company they join. Accepting settles such a person
    (see ``account_invitations._settle_newcomer``).
    """

    is_newcomer: Callable[[str], bool]
    identity_methods: tuple[str, ...] = ()


_NEWCOMER_SOURCES: list[NewcomerSource] = []


def register_newcomer_source(source: NewcomerSource) -> None:
    if isinstance(source, NewcomerSource) and source not in _NEWCOMER_SOURCES:
        _NEWCOMER_SOURCES.append(source)


def newcomer_sources() -> tuple[NewcomerSource, ...]:
    return tuple(_NEWCOMER_SOURCES)


def loading_checkpoint() -> tuple[Any, ...]:
    return (*_POLICIES, *_NEWCOMER_SOURCES)


def restore_loading_checkpoint(checkpoint: tuple[Any, ...] | None) -> None:
    kept = tuple(checkpoint or ())
    _POLICIES[:] = [policy for policy in _POLICIES if any(policy is old for old in kept)]
    _NEWCOMER_SOURCES[:] = [
        source for source in _NEWCOMER_SOURCES if any(source is old for old in kept)
    ]


__all__ = [
    "InvitationPolicyUnavailable",
    "NewcomerSource",
    "extra_invitation_limits",
    "loading_checkpoint",
    "newcomer_sources",
    "register_invitation_limits",
    "register_newcomer_source",
    "restore_loading_checkpoint",
]
