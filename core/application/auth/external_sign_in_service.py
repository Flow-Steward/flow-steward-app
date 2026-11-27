"""External sign-in: from a sign-in button to a Core session (ADR 0083).

:class:`ExternalSignInService` is transport-agnostic. The routes in
``core/entrypoints/api/external_sign_in_routes.py`` turn its results into redirects
and cookies.

Every sign-in method takes one Core path. Core implements no sign-in protocol: it
ships only the trusted header kind (:mod:`core.application.auth.external_sign_in_methods`)
and platform modules register the others through the port. For a person who picks an
option:

1. :meth:`ExternalSignInService.start` asks the method to begin. A method that
   redirects gets a fresh ``state`` and Core's callback URL; Core stores a single-use
   attempt (hash of ``state``, the method's data encrypted, the return path, the hash
   of a browser-binding value) that expires after ten minutes, and returns the
   redirect plus the binding value the route sets as a short HttpOnly cookie. A
   method that does not redirect (``header``) completes at once from the request.
2. :meth:`ExternalSignInService.callback` consumes the attempt atomically, requires
   the same browser and asks the attempt's method to complete.
3. Either way the verified identity goes through the port's resolver, Core checks
   the user and the account, links the identity and issues a session whose
   ``auth_method`` is ``external``. The session's account is the issuing account
   (the identity's account when the method names one); the person must be a member
   of that account and an administrator of no account (the installation account
   included), so one account's sign-in source never signs in as a person outside it
   or as an administrator. The session stays pinned to that account and holds the
   person's role there only (ADR 0085): switching accounts is refused for it.
   A method declared ``installation_wide`` (ADR 0083 amendment of 2026-11-27) is
   set up once for the installation: its options belong to no account, its feature
   is the installation account's, and its identity may sign in an administrator of
   an ordinary account (never of the installation account). Core tells the resolver
   the account of an open invitation the sign-in returns to
   (``invitation_account_id``), and shows a resolver's ``public_message`` on the
   sign-in page.

Identities are stored under issuers prefixed with the method's kind, so one method
can never name an identity another one verified. Every success and refusal is
audited with a reason code, the method, the option, the issuer, the subject and the
tenant. Tokens, codes and other claims are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, NoReturn
from urllib.parse import urlencode

from core.application.auth import external_sign_in_repository as repository
from core.application.auth.external_sign_in_methods import HeaderSignInMethod
from core.application.auth.external_sign_in_ports import (
    ExternalIdentityRefused,
    ExternalSignInError,
    ExternalSignInPort,
    SignInMethod,
    VerifiedExternalIdentity,
    namespaced_issuer,
)
from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.auth.password import session_token_hash
from core.application.auth.secrets import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

ConnectionFactory = Callable[[], AbstractContextManager[Any]]

START_PATH = "/api/auth/external/start"
CALLBACK_PATH = "/api/auth/external/callback"
OPTIONS_PATH = "/api/auth/external/options"
_MAX_RETURN_PATH = 2048


def safe_return_path(raw: Any) -> str:
    """A same-origin relative path, or ``/``.

    Refuses absolute and protocol-relative URLs, backslashes, control characters and
    the sign-in routes themselves (which would loop).
    """
    text = str(raw or "").strip()
    if (
        not text.startswith("/")
        or text.startswith(("//", "/api/auth/external/"))
        or "\\" in text
        or len(text) > _MAX_RETURN_PATH
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)
    ):
        return "/"
    return text


def new_secret_value() -> str:
    """A fresh random value for ``state`` or the browser binding."""
    return secrets.token_urlsafe(32)


def value_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def same_hash(value: str, expected_hash: str) -> bool:
    return bool(expected_hash) and hmac.compare_digest(value_hash(value), str(expected_hash))


@dataclass(frozen=True)
class StartResult:
    authorization_url: str
    browser_binding: str


@dataclass(frozen=True)
class SignInResult:
    """A session to set, or (for linking) only a place to return to."""

    return_path: str
    session_token: str = ""
    session_ttl_hours: int = 0
    user_id: str = ""
    account_id: str = ""
    linked: bool = False


class ExternalSignInRefused(Exception):
    """Sign-in did not complete. ``reason`` is the audit code; ``return_path`` survives.

    ``public_message`` is the resolver's sentence for the person, if it gave one.
    """

    def __init__(self, reason: str, *, return_path: str = "/", public_message: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.return_path = return_path
        self.public_message = public_message


#: An invitation link the sign-in may return to (ADR 0085, C1).
_INVITATION_PATH = re.compile(r"^/invitations/([A-Za-z0-9_-]{16,128})/?$")


def _installation_wide(method: Any) -> bool:
    return getattr(method, "installation_wide", False) is True


SessionGuard = Callable[[Mapping[str, Any], str, str], str | None]
AuditFn = Callable[..., None]
EntitledFn = Callable[[str, str], bool]


class ExternalSignInService:
    def __init__(
        self,
        *,
        connection: ConnectionFactory,
        port: ExternalSignInPort,
        entitled: EntitledFn,
        audit: AuditFn,
        session_ttl_hours: int,
        session_guard: SessionGuard | None = None,
    ) -> None:
        self._connection = connection
        self._port = port
        self._entitled = entitled
        self._audit = audit
        self._session_ttl_hours = int(session_ttl_hours)
        self._session_guard = session_guard
        self._builtin: tuple[SignInMethod, ...] = (HeaderSignInMethod(port),)

    # --- methods and options ----------------------------------------------------------

    def methods(self) -> dict[str, SignInMethod]:
        """Every sign-in method: Core's trusted header kind, then the ones modules added."""
        registered: dict[str, SignInMethod] = {method.kind: method for method in self._builtin}
        for method in self._port.sign_in_methods:
            registered.setdefault(method.kind, method)
        return registered

    def _is_entitled(self, account_id: str, feature: str) -> bool:
        try:
            return bool(self._entitled(account_id, feature))
        except Exception:
            logger.exception("Entitlement check failed for external sign-in")
            return False

    def _offered(self, option: Any, *, installation_wide: bool = False) -> bool:
        feature = str(getattr(option, "feature", "") or "")
        if installation_wide:
            # An installation-wide option is offered behind the installation's feature.
            return not feature or self._is_entitled(INSTALLATION_ACCOUNT_ID, feature)
        account_id = str(getattr(option, "account_id", "") or "")
        if feature and account_id:
            return self._is_entitled(account_id, feature)
        return True

    def _option_account(self, kind: str, option: Any) -> str:
        """The account an option belongs to; none for an installation-wide method."""
        method = self.methods().get(kind)
        if _installation_wide(method):
            return ""
        return str(getattr(option, "account_id", "") or "")

    def has_account_options(self) -> bool:
        """Whether any method offers an option that belongs to one account."""
        return any(self._option_account(kind, option) for kind, option in self._all_options())

    def _all_options(self) -> list[tuple[str, Any]]:
        listed: list[tuple[str, Any]] = []
        for kind, method in self.methods().items():
            try:
                options = list(method.options())
            except Exception:
                logger.exception("Sign-in method %s failed to list its options", kind)
                continue
            listed.extend((kind, option) for option in options)
        return listed

    def sign_in_options(self, account_id: str | None = "") -> list[dict[str, str]]:
        """The buttons of the public sign-in page.

        Without an account (``""``) only the options that belong to no account, the
        installation-wide ones. With ``account_id`` those plus the options of that
        account. ``None`` (an account hint that names no active account) yields only
        the installation-wide options. One account's buttons are never listed to a
        visitor who has not named that account (ADR 0083, security notes).
        """
        wanted = str(account_id or "")
        buttons: list[dict[str, str]] = []
        seen: set[str] = set()
        methods = self.methods()
        for kind, option in self._all_options():
            button_key = f"{kind}:{option.key}"
            option_account = self._option_account(kind, option)
            if option_account and option_account != wanted:
                continue
            offered = self._offered(option, installation_wide=_installation_wide(methods.get(kind)))
            if button_key in seen or not option.display_name or not offered:
                continue
            seen.add(button_key)
            query = urlencode({"method": kind, "key": option.key})
            buttons.append(
                {
                    "provider_key": button_key,
                    "display_name": option.display_name,
                    "login_path": f"{START_PATH}?{query}",
                }
            )
        return buttons

    def account_for_hint(self, hint: str) -> str | None:
        """The active account a sign-in page hint (id or key) names, or ``None``."""
        if not str(hint or "").strip():
            return None
        with self._connection() as conn, conn.cursor() as cur:
            return repository.account_id_for_hint(cur, hint)

    def public_options(self, hint: str = "") -> dict[str, Any]:
        """The public answer of ``GET /api/auth/external/options``."""
        account_id: str | None = ""
        if str(hint or "").strip():
            account_id = self.account_for_hint(hint)
        return {
            "options": self.sign_in_options(account_id),
            "account_hint_supported": self.has_account_options(),
        }

    # --- start and callback -----------------------------------------------------------

    def start(
        self,
        *,
        method: str,
        key: str,
        next_path: Any,
        callback_url: str,
        headers: Mapping[str, str] | None = None,
        link_user_id: str | None = None,
    ) -> StartResult | SignInResult:
        return_path = safe_return_path(next_path)
        kind = str(method or "").strip()
        option_key = str(key or "").strip()
        sign_in = self.methods().get(kind)
        if sign_in is None:
            self._refuse("method_unavailable", option=f"{kind}:{option_key}", path=return_path)
        try:
            option = sign_in.option(option_key)
        except Exception:
            logger.exception("Sign-in method %s failed to find option %s", kind, option_key)
            option = None
        if option is None or not self._offered(
            option, installation_wide=_installation_wide(sign_in)
        ):
            self._refuse("option_unavailable", option=f"{kind}:{option_key}", path=return_path)
        state = new_secret_value()
        try:
            begun = sign_in.begin(option_key, callback_url=callback_url, state=state)
        except ExternalSignInError as exc:
            self._refuse(exc.reason, option=f"{kind}:{option_key}", path=return_path)
        if not begun.redirect_url:
            identity = self._complete_method(
                sign_in,
                option_key,
                params={},
                headers=headers or {},
                data=begun.data,
                callback_url=callback_url,
                path=return_path,
            )
            return self._finish(identity, link_user_id=link_user_id, return_path=return_path)
        if not callback_url:
            self._refuse("public_url_missing", option=f"{kind}:{option_key}", path=return_path)
        binding = new_secret_value()
        with self._connection() as conn, conn.cursor() as cur:
            repository.insert_state(
                cur,
                state_hash=value_hash(state),
                method=kind,
                option_key=option_key,
                browser_binding_hash=value_hash(binding),
                method_data_encrypted=encrypt_secret(json.dumps(dict(begun.data))),
                redirect_uri=callback_url,
                return_path=return_path,
                link_user_id=link_user_id or None,
            )
        return StartResult(authorization_url=begun.redirect_url, browser_binding=binding)

    def callback(
        self,
        *,
        params: Mapping[str, str],
        browser_binding: str,
        headers: Mapping[str, str] | None = None,
    ) -> SignInResult:
        state = str(params.get("state") or "")
        if not state:
            self._refuse("state_missing")
        with self._connection() as conn, conn.cursor() as cur:
            attempt = repository.consume_state(cur, value_hash(state))
        if attempt is None:
            self._refuse("state_invalid_or_used")
        return_path = safe_return_path(attempt.get("return_path"))
        kind = str(attempt.get("method") or "")
        option_key = str(attempt.get("option_key") or "")
        label = f"{kind}:{option_key}"
        if not same_hash(browser_binding, str(attempt.get("browser_binding_hash") or "")):
            self._refuse("browser_mismatch", option=label, path=return_path)
        sign_in = self.methods().get(kind)
        if sign_in is None:
            self._refuse("method_unavailable", option=label, path=return_path)
        try:
            encrypted = str(attempt.get("method_data_encrypted") or "")
            data = json.loads(decrypt_secret(encrypted)) if encrypted else {}
        except Exception:
            self._refuse("attempt_unreadable", option=label, path=return_path)
        identity = self._complete_method(
            sign_in,
            option_key,
            params=params,
            headers=headers or {},
            data=data if isinstance(data, dict) else {},
            callback_url=str(attempt.get("redirect_uri") or ""),
            path=return_path,
        )
        return self._finish(
            identity,
            link_user_id=str(attempt.get("link_user_id") or "") or None,
            return_path=return_path,
        )

    def _complete_method(
        self,
        sign_in: SignInMethod,
        option_key: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        data: Mapping[str, Any],
        callback_url: str,
        path: str,
    ) -> VerifiedExternalIdentity:
        label = f"{sign_in.kind}:{option_key}"
        try:
            option = sign_in.option(option_key)
        except Exception:
            logger.exception("Sign-in method %s failed to find option %s", sign_in.kind, option_key)
            option = None
        if option is None:
            self._refuse("option_unavailable", option=label, path=path)
        try:
            identity = sign_in.complete(
                option_key, params=params, headers=headers, data=data, callback_url=callback_url
            )
        except ExternalSignInError as exc:
            self._refuse(exc.reason, option=label, path=path)
        except Exception:
            logger.exception("Sign-in method %s failed", sign_in.kind)
            self._refuse("method_failed", option=label, path=path)
        if not isinstance(identity, VerifiedExternalIdentity):
            self._refuse("identity_missing", option=label, path=path)
        subject = str(identity.subject or "").strip()
        issuer = str(identity.issuer or "").strip()
        if not subject or not issuer or len(subject) > 255:
            self._refuse("identity_invalid", option=label, path=path)
        installation_wide = _installation_wide(sign_in)
        # Core, not the method, names the method and the issuer namespace, and
        # whether the identity is installation-wide (then it names no account).
        return VerifiedExternalIdentity(
            method=sign_in.kind,
            option_key=option_key,
            account_id="" if installation_wide else str(identity.account_id or ""),
            issuer=namespaced_issuer(sign_in.kind, issuer),
            subject=subject,
            tenant_id=str(identity.tenant_id or ""),
            claims=identity.claims,
            feature=str(identity.feature or option.feature or ""),
            installation_wide=installation_wide,
            invitation_account_id=self._invitation_account(path),
        )

    def _invitation_account(self, return_path: str) -> str:
        """The account of the open invitation the sign-in returns to, or ``""``."""
        match = _INVITATION_PATH.match(str(return_path or ""))
        if match is None:
            return ""
        from core.application.auth.user_admin.account_invitations import (
            open_invitation_account,
        )

        try:
            with self._connection() as conn, conn.cursor() as cur:
                return open_invitation_account(cur, match.group(1))
        except Exception:
            logger.warning("The invitation of an external sign-in could not be read")
            return ""

    # --- the common end ---------------------------------------------------------------

    def _finish(
        self,
        identity: VerifiedExternalIdentity,
        *,
        link_user_id: str | None,
        return_path: str,
    ) -> SignInResult:
        if link_user_id:
            return self._link(identity, user_id=link_user_id, return_path=return_path)
        return self._complete(identity, return_path=return_path)

    def _complete(self, identity: VerifiedExternalIdentity, *, return_path: str) -> SignInResult:
        try:
            resolution = self._port.resolver.resolve(identity)
        except ExternalIdentityRefused as exc:
            self._refuse(
                exc.reason,
                identity=identity,
                path=return_path,
                public_message=str(getattr(exc, "public_message", "") or ""),
            )
        except Exception:
            logger.exception("The external identity resolver failed")
            self._refuse("resolver_failed", identity=identity, path=return_path)
        user_id = str(resolution.user_id or "")
        account_id = str(resolution.account_id or "")
        if not account_id:
            self._refuse("account_missing", identity=identity, path=return_path)
        if identity.account_id and identity.account_id != account_id:
            # The session belongs to the account that issued the identity, never another.
            self._refuse("account_mismatch", identity=identity, path=return_path)
        # An installation-wide identity's feature is the installation's.
        entitled_account = INSTALLATION_ACCOUNT_ID if identity.installation_wide else account_id
        if identity.feature and not self._is_entitled(entitled_account, identity.feature):
            self._refuse("not_entitled", identity=identity, path=return_path)
        session_token = secrets.token_urlsafe(48)
        with self._connection() as conn, conn.cursor() as cur:
            user = repository.load_user(cur, user_id)
            if user is None or not bool(user.get("is_active")):
                self._refuse("user_inactive", identity=identity, path=return_path)
            if not repository.user_in_account(cur, user_id=user_id, account_id=account_id):
                self._refuse("account_unavailable", identity=identity, path=return_path)
            person_refusal = repository.external_person_refusal(
                cur,
                user_id=user_id,
                account_id=account_id,
                allow_account_admin=identity.installation_wide,
            )
            if person_refusal:
                self._refuse(person_refusal, identity=identity, path=return_path)
            refusal = (
                self._session_guard(user, account_id, repository.EXTERNAL_AUTH_METHOD)
                if self._session_guard is not None
                else None
            )
            if refusal:
                self._refuse(refusal, identity=identity, path=return_path)
            link = self._link_row(
                cur,
                identity,
                account_id=account_id,
                user_id=user_id,
                created_by="external-sign-in",
            )
            if link is None:
                self._refuse("identity_linked_to_another_user", identity=identity, path=return_path)
            repository.touch_identity(cur, str(link["id"]))
            repository.insert_session(
                cur,
                user_id=user_id,
                account_id=account_id,
                token_hash=session_token_hash(session_token),
                ttl_hours=self._session_ttl_hours,
                external_identity_id=str(link["id"]),
            )
        self._audit_event(
            "ok",
            identity=identity,
            username=str(user.get("username") or ""),
            extra={"account_id": account_id, "session_ttl_hours": self._session_ttl_hours},
        )
        return SignInResult(
            return_path=return_path,
            session_token=session_token,
            session_ttl_hours=self._session_ttl_hours,
            user_id=user_id,
            account_id=account_id,
        )

    @staticmethod
    def _link_row(
        cur: Any,
        identity: VerifiedExternalIdentity,
        *,
        account_id: str,
        user_id: str,
        created_by: str,
    ) -> dict[str, Any] | None:
        try:
            return repository.link_identity(
                cur,
                account_id=account_id,
                user_id=user_id,
                method=identity.method,
                option_key=identity.option_key,
                issuer=identity.issuer,
                subject=identity.subject,
                created_by=created_by,
            )
        except repository.IdentityLinkedElsewhereError:
            return None

    def _link(
        self, identity: VerifiedExternalIdentity, *, user_id: str, return_path: str
    ) -> SignInResult:
        """Link the identity to the signed-in person who started the attempt."""
        with self._connection() as conn, conn.cursor() as cur:
            user = repository.load_user(cur, user_id)
            if user is None or not bool(user.get("is_active")):
                self._refuse("user_inactive", identity=identity, path=return_path)
            account_id = identity.account_id or str(user.get("account_id") or "")
            if not repository.user_in_account(cur, user_id=user_id, account_id=account_id):
                self._refuse("account_unavailable", identity=identity, path=return_path)
            # The same rule as sign-in: a link that could never sign in is not made.
            person_refusal = repository.external_person_refusal(
                cur,
                user_id=user_id,
                account_id=account_id,
                allow_account_admin=identity.installation_wide,
            )
            if person_refusal:
                self._refuse(person_refusal, identity=identity, path=return_path)
            created_by = str(user.get("username") or user_id)
            linked = self._link_row(
                cur, identity, account_id=account_id, user_id=user_id, created_by=created_by
            )
            if linked is None:
                self._refuse("identity_linked_to_another_user", identity=identity, path=return_path)
        self._audit_event(
            "ok",
            identity=identity,
            username=str(user.get("username") or ""),
            action="external_link",
        )
        return SignInResult(return_path=return_path, user_id=user_id, linked=True)

    # --- audit ------------------------------------------------------------------------

    def _audit_event(
        self,
        status: str,
        *,
        identity: VerifiedExternalIdentity | None = None,
        option: str = "",
        username: str = "",
        reason: str = "",
        action: str = "external_login",
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        detail: dict[str, Any] = {"option": option}
        if identity is not None:
            detail.update(
                option=f"{identity.method}:{identity.option_key}",
                method=identity.method,
                issuer=identity.issuer,
                subject=identity.subject,
                tenant_id=identity.tenant_id,
            )
        if reason:
            detail["reason"] = reason
        detail.update(extra or {})
        try:
            self._audit(
                action=action,
                username=username or (identity.subject if identity else ""),
                actor="auth",
                status=status,
                detail=detail,
            )
        except Exception:
            logger.exception("Auditing external sign-in failed")

    def _refuse(
        self,
        reason: str,
        *,
        option: str = "",
        identity: VerifiedExternalIdentity | None = None,
        path: str = "/",
        public_message: str = "",
    ) -> NoReturn:
        self._audit_event("rejected", identity=identity, option=option, reason=reason)
        raise ExternalSignInRefused(reason, return_path=path, public_message=public_message)


def external_sign_in_offered(port: Any) -> bool:
    """Whether the sign-in page should ask for sign-in buttons at all."""
    return port is not None and bool(port.header_source is not None or port.sign_in_methods)


__all__ = [
    "CALLBACK_PATH",
    "OPTIONS_PATH",
    "START_PATH",
    "ExternalSignInRefused",
    "ExternalSignInService",
    "SignInResult",
    "StartResult",
    "external_sign_in_offered",
    "new_secret_value",
    "safe_return_path",
    "same_hash",
    "value_hash",
]
