"""Commands shown to an operator, written so they run when pasted where they were shown.

Application text names commands canonically (``flow-steward users list``). On a Docker
host the operator runs the repository launcher (``./flow-steward``, or a path to it);
inside the Compact image on a platform the command is ``flow-steward`` on PATH. The
launcher passes the name it was started as, and every command shown to the operator is
rewritten to that name at the presentation boundary.
"""

from __future__ import annotations

import re
from functools import lru_cache

CANONICAL_PROGRAM = "flow-steward"
_SAFE_PREFIX = re.compile(r"[A-Za-z0-9_./~+-]{1,256}")
_prefix = CANONICAL_PROGRAM


def set_invocation_prefix(value: str | None) -> None:
    """Record how the operator started the CLI; anything unexpected keeps the canonical name."""
    global _prefix
    candidate = (value or "").strip()
    _prefix = candidate if _SAFE_PREFIX.fullmatch(candidate) else CANONICAL_PROGRAM


def invocation_prefix() -> str:
    """The program name to show in front of a command."""
    return _prefix


@lru_cache(maxsize=1)
def _command_pattern() -> re.Pattern[str]:
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

    roots = sorted({spec.path[0] for spec in PUBLIC_COMMAND_SPECS}, key=len, reverse=True)
    words = "|".join(re.escape(root) for root in roots)
    # A canonical or ./-prefixed name followed by a public command or a root flag.
    # Image, container and project names (flow-steward-app, flow-steward_default) never
    # match: the name must end at a space before the command word.
    return re.compile(
        rf"(?<![\w./~+-])(?:\./)?{re.escape(CANONICAL_PROGRAM)}"
        rf"(?=[ ]+(?:(?:{words})\b|--help\b|--version\b|-h\b))"
    )


def command_line(*words: str) -> str:
    """Build a command to show: ``command_line("users", "list")`` -> ``./flow-steward users list``."""
    return " ".join((invocation_prefix(), *(word for word in words if word)))


def with_invocation_prefix(text: str) -> str:
    """Rewrite every command in ``text`` to the name the operator actually runs."""
    if CANONICAL_PROGRAM not in text:
        return text
    prefix = invocation_prefix()
    return _command_pattern().sub(lambda _match: prefix, text)
