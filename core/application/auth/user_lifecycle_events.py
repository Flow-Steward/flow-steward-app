"""Callbacks told when Core deletes a user (ADR 0082, ADR 0083).

A platform module that keeps per-user data subscribes through
``container.external_sign_in.subscribe_user_deleted``. Core calls every callback
after the deletion committed, with the user's id; a failing callback is logged and
never undoes the deletion. The registry is module-level so the users API reaches it
without the runtime container; the platform module loader rolls it back when a module
fails to load (``loading_checkpoint`` / ``restore_loading_checkpoint``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

UserDeletedCallback = Callable[[str], None]

_LOCK = threading.Lock()
_USER_DELETED: list[UserDeletedCallback] = []


def subscribe_user_deleted(callback: UserDeletedCallback) -> None:
    if not callable(callback):
        raise TypeError("callback must be callable")
    with _LOCK:
        _USER_DELETED.append(callback)


def notify_user_deleted(user_id: str) -> None:
    with _LOCK:
        callbacks = list(_USER_DELETED)
    for callback in callbacks:
        try:
            callback(str(user_id))
        except Exception:
            logger.exception("A user-deleted callback failed for user %s", user_id)


def loading_checkpoint() -> tuple[UserDeletedCallback, ...]:
    with _LOCK:
        return tuple(_USER_DELETED)


def restore_loading_checkpoint(checkpoint: tuple[UserDeletedCallback, ...] | None) -> None:
    with _LOCK:
        _USER_DELETED[:] = list(checkpoint or ())


__all__ = [
    "loading_checkpoint",
    "notify_user_deleted",
    "restore_loading_checkpoint",
    "subscribe_user_deleted",
]
