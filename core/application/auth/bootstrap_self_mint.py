"""Let a runtime nobody installed mint its own first-administrator link.

AC17-R117.  Every path that produces a setup token today runs on the operator's
machine: ``install.sh`` mints one and prints the link, and ``users
issue-bootstrap-token`` replaces it.  An image started without either -- ``docker
run``, a PaaS, a compose file someone wrote themselves -- therefore comes up with
no token, no state file, and ``FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY`` at its safe
default, so the only screen that installation has answers 403 to the only person
who could ever create the first administrator.  There is no recovery from
outside, because every command that would issue a token is an installer command.

So the runtime issues one for itself, under exactly the conditions that make it
safe: local authentication on, bootstrap not switched off, nobody else managing a
token for this installation, and no administrator yet.  The link carries the
token in a fragment, the same transport the installer uses, and the state file it
writes is the same file the request path already validates against.  Nothing is
opened: without the token the endpoint still refuses, so an installation that
self-mints is strictly harder to reach than one relying on the localhost rule,
and that rule keeps its default either way.

This is Compact's concern alone.  Scalable is reached only through the installer,
which owns the env file the token would live in; Compact is the profile that can
be started by something that never saw our installer at all.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.application.auth.config import env_flag
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

# Shorter than the installer's hour, and deliberately so: that token is read from a
# terminal the operator is sitting at, while this one is written to the platform's
# own log stream, where it may be shipped to an aggregator, retained and read by
# anyone holding logs long after the deploy. Fifteen minutes covers an operator who
# is watching the deploy finish and looks away once; it does not cover a log line
# found later. Being locked out costs one restart, because the mint runs again on
# every start while no administrator exists -- which is also what invalidates the
# link an operator never used.
SELF_MINTED_TOKEN_TTL = timedelta(minutes=15)

_BOOTSTRAP_PATH = "/bootstrap"
_FRAGMENT = "#setup-token="


@dataclass(frozen=True)
class BootstrapSelfMint:
    """Either a one-time link this runtime just minted, or why it minted none."""

    token: str = ""
    expires_at: datetime | None = None
    link: str = ""
    declined_because: str = ""

    @property
    def minted(self) -> bool:
        return bool(self.token)


def usable_bootstrap_token_state(state_path: Path, *, now: datetime) -> bool:
    """True when the state file still names a token a caller could present.

    Missing, unreadable, malformed and expired are all one answer here. An expired
    file is as useless to the operator as no file at all, and the installer already
    settled that question the same way for its own token: an installed rerun
    replaces an expired setup token instead of advising around it.
    """
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        token_hash = str(payload["token_sha256"]).strip()
        expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
    except (OSError, KeyError, TypeError, ValueError):
        return False
    if not token_hash:
        return False
    return now < expires_at.astimezone(UTC)


def self_mint_bootstrap_link(
    *,
    state_path: Path,
    user_count: Callable[[], int],
    public_base_url: Callable[[], str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> BootstrapSelfMint:
    """Mint a one-time setup link when nothing and nobody else will."""
    moment = (now or (lambda: datetime.now(UTC)))().astimezone(UTC)
    if not env_flag("FS_LOCAL_AUTH_ENABLED", True):
        return BootstrapSelfMint(declined_because="local authentication is disabled")
    if not env_flag("FS_AUTH_BOOTSTRAP_ENABLED", True):
        return BootstrapSelfMint(declined_because="first-administrator bootstrap is disabled")
    if str(_runtime_env.get("FS_AUTH_BOOTSTRAP_TOKEN") or "").strip():
        return BootstrapSelfMint(
            declined_because="a setup token is already configured for this installation"
        )
    if usable_bootstrap_token_state(state_path, now=moment):
        return BootstrapSelfMint(declined_because="a setup link issued earlier is still valid")
    try:
        existing = int(user_count())
    except Exception as exc:  # the reason has to reach the log, not just the decision
        return BootstrapSelfMint(declined_because=f"the user count could not be read: {exc}")
    if existing != 0:
        return BootstrapSelfMint(
            declined_because="this installation already has its first administrator"
        )
    token = secrets.token_urlsafe(32)
    return BootstrapSelfMint(
        token=token,
        expires_at=moment + SELF_MINTED_TOKEN_TTL,
        link=_bootstrap_link(token, public_base_url=public_base_url),
    )


def _bootstrap_link(token: str, *, public_base_url: Callable[[], str] | None) -> str:
    if public_base_url is None:
        from core.application.auth.serve_auth_runtime import public_base_url as configured

        public_base_url = configured
    base = str(public_base_url() or "").strip().rstrip("/")
    # A runtime with no configured public URL still gets a usable line: the path and
    # the fragment are the whole secret, and the operator knows their own host. The
    # alternative -- withholding the link because we cannot name the host -- would
    # leave exactly the installations this exists for with nothing.
    return f"{base}{_BOOTSTRAP_PATH}{_FRAGMENT}{token}"
