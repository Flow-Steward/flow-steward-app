"""Application-owned identity contract for release-membership serialization."""

from __future__ import annotations

EXTENSION_RELEASE_MEMBERSHIP_LOCK_NAMESPACE = "fs.extension_release_membership"


def extension_release_membership_lock_key(
    release_id: str | None,
    *,
    extension_id: str = "",
) -> str:
    """Return the stable coordination key for one target membership set."""
    normalized_release_id = str(release_id or "").strip()
    if normalized_release_id:
        return normalized_release_id
    normalized_extension_id = str(extension_id or "").strip()
    if not normalized_extension_id:
        raise ValueError("release_id or extension_id is required for membership locking")
    return f"unbound:{normalized_extension_id}"
