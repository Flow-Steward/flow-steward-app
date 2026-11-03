"""The PostgreSQL `application_name` every Flow Steward process connects with (ADR 0056).

`database upgrade` refuses to change the schema while another Flow Steward process
holds a session. Processes identify themselves as `flow-steward:<role>:<version>`;
each entrypoint names its role before it connects, and anything else that imports
the connection factory is labelled `runtime`, which also blocks.
"""

from __future__ import annotations

import re

from core.infrastructure.config.version import APP_VERSION

SESSION_PREFIX = "flow-steward"
#: Roles that may hold a session while an upgrade runs: the upgrade itself, and a
#: maintenance status surface that writes nothing.
NON_BLOCKING_ROLES = frozenset({"upgrade", "maintenance"})
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_role = "runtime"


def set_session_role(role: str) -> None:
    """Name this process's role; call before the first database connection."""
    global _role
    normalized = str(role or "").strip().lower()
    if not _ROLE_RE.match(normalized):
        raise ValueError(f"invalid session role: {role!r}")
    _role = normalized


def session_application_name() -> str:
    # PostgreSQL truncates application_name to 63 bytes.
    return f"{SESSION_PREFIX}:{_role}:{APP_VERSION}"[:63]


__all__ = [
    "NON_BLOCKING_ROLES",
    "SESSION_PREFIX",
    "session_application_name",
    "set_session_role",
]
