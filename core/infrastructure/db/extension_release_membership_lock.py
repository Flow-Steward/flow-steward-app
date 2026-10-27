"""PostgreSQL coordination for shared extension-release membership."""

from __future__ import annotations

from typing import Any

from core.application.extensions.release_membership_lock import (
    EXTENSION_RELEASE_MEMBERSHIP_LOCK_NAMESPACE,
    extension_release_membership_lock_key,
)


def acquire_extension_release_membership_lock(cur: Any, *, release_id: str) -> None:
    """Serialize scan enumeration/publication for one shared release."""
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
        (
            EXTENSION_RELEASE_MEMBERSHIP_LOCK_NAMESPACE,
            extension_release_membership_lock_key(release_id),
        ),
    )
