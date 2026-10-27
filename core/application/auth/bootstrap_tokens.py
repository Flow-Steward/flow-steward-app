"""Expiring one-time first-administrator setup token policy."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from core.application.management.errors import ServicePreconditionError

TOKEN_TTL = timedelta(minutes=60)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapTokenIssueInput(_ClosedModel):
    pass


class BootstrapTokenActivation(_ClosedModel):
    secret_path: str


class BootstrapTokenIssueResult(_ClosedModel):
    status: str = "issued"
    output_path: str
    expires_at: datetime
    header_name: str = "x-platform-setup-token"


class BootstrapTokenRuntime(Protocol):
    def activate(self, *, token: str, expires_at: datetime) -> BootstrapTokenActivation: ...


class BootstrapTokenService:
    """Issue exactly one active token while the zero-user guard holds."""

    def __init__(
        self,
        *,
        user_count: Callable[[], int],
        runtime: BootstrapTokenRuntime,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._user_count = user_count
        self._runtime = runtime
        self._now = now or (lambda: datetime.now(UTC))

    def issue(self) -> BootstrapTokenIssueResult:
        count = self._user_count()
        if type(count) is not int or count != 0:
            raise ServicePreconditionError(
                "A bootstrap token can be issued only while no users exist."
            )
        issued_at = self._now().astimezone(UTC)
        token = secrets.token_urlsafe(32)
        expires_at = issued_at + TOKEN_TTL
        activation = self._runtime.activate(token=token, expires_at=expires_at)
        return BootstrapTokenIssueResult(
            output_path=activation.secret_path,
            expires_at=expires_at,
        )


def bootstrap_token_request_valid(
    provided: str,
    *,
    required: str,
    expires_at: str,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Validate configured setup-token equality and expiry without leaking values."""
    if not provided or not hmac.compare_digest(provided, required):
        return False, "Invalid bootstrap setup token"
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        return False, "Bootstrap setup token expiry is missing or invalid"
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current >= expiry:
        return False, "Bootstrap setup token has expired"
    return True, ""


def bootstrap_token_state_request_valid(
    provided: str, *, state_path: Path, now: datetime | None = None
) -> tuple[bool, str]:
    """Validate against the authoritative owner-only hash state."""
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        required_hash = str(payload["token_sha256"])
        expires_at = str(payload["expires_at"])
    except (OSError, KeyError, TypeError, ValueError):
        return False, "Bootstrap setup token state is missing or invalid"
    provided_hash = hashlib.sha256(str(provided or "").encode("utf-8")).hexdigest()
    if not provided or not hmac.compare_digest(provided_hash, required_hash):
        return False, "Invalid bootstrap setup token"
    return bootstrap_token_request_valid(
        provided_hash,
        required=required_hash,
        expires_at=expires_at,
        now=now,
    )
