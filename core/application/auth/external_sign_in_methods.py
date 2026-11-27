"""Core's one built-in sign-in method: the trusted header kind (ADR 0083).

``header`` takes an identity from request headers through the trusted header source
a module registered; Core reads no header itself. It implements
:class:`core.application.auth.external_sign_in_ports.SignInMethod`, the same contract
a module's own method implements. Sign-in protocols are methods modules register.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from core.application.auth.external_sign_in_ports import (
    BeginResult,
    ExternalSignInError,
    ExternalSignInPort,
    SignInOption,
    VerifiedExternalIdentity,
)

logger = logging.getLogger(__name__)

HEADER_KIND = "header"
#: The key of the header method's one option.
HEADER_OPTION_KEY = "trusted"


class HeaderSignInMethod:
    """A trusted header source, through the module that registered it."""

    kind = HEADER_KIND

    def __init__(self, port: ExternalSignInPort) -> None:
        self._port = port

    def options(self) -> Iterable[SignInOption]:
        option = self.option(HEADER_OPTION_KEY)
        return [option] if option is not None else []

    def option(self, key: str) -> SignInOption | None:
        source = self._port.header_source
        if source is None or key != HEADER_OPTION_KEY:
            return None
        return SignInOption(
            key=HEADER_OPTION_KEY,
            display_name=str(getattr(source, "display_name", "") or ""),
            # The account the source signs people in to: the option is then listed
            # only with that account's hint, never on the installation-wide list.
            account_id=str(getattr(source, "account_id", "") or "").strip(),
            feature=str(getattr(source, "feature", "") or ""),
        )

    def begin(self, key: str, *, callback_url: str, state: str) -> BeginResult:
        return BeginResult()

    def complete(
        self,
        key: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        data: Mapping[str, Any],
        callback_url: str,
    ) -> VerifiedExternalIdentity:
        source = self._port.header_source
        if source is None:
            raise ExternalSignInError("method_unavailable")
        try:
            identity = source.resolve(headers)
        except Exception as exc:
            logger.exception("The trusted header source failed")
            raise ExternalSignInError("header_source_failed") from exc
        if identity is None:
            raise ExternalSignInError("header_identity_missing")
        return VerifiedExternalIdentity(
            method=HEADER_KIND,
            option_key=HEADER_OPTION_KEY,
            account_id=identity.account_id,
            issuer=identity.issuer,
            subject=identity.subject,
            tenant_id=identity.tenant_id,
            claims=identity.claims,
            feature=str(getattr(source, "feature", "") or ""),
        )


__all__ = ["HEADER_KIND", "HEADER_OPTION_KEY", "HeaderSignInMethod"]
