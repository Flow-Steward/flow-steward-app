"""Core's external sign-in seam for platform modules (ADR 0082, ADR 0083).

Core owns one sign-in path for every method: the public routes, the single-use
attempt bound to the browser, the identity check, the session and its cookies, the
users it creates and the roles it grants. Core implements no sign-in protocol. A
**sign-in method** is a kind of source, provided by a platform module, that verifies
a person somewhere else and hands Core a :class:`VerifiedExternalIdentity`. A module
reaches the port as ``container.external_sign_in`` and may:

- register a **sign-in method** (:meth:`ExternalSignInPort.register_sign_in_method`);
  Core keeps the attempt, the callback and the session;
- set the **identity resolver** (:meth:`ExternalSignInPort.set_identity_resolver`)
  that maps a verified identity to a user and an account. Core's default,
  :class:`LinkedIdentityResolver`, signs in only identities already linked to a user;
- register one **trusted header source**
  (:meth:`ExternalSignInPort.register_header_source`) that reads an identity from
  request headers set by a component in front of Core that the module trusts. Core's
  built-in ``header`` kind offers it; without one that kind offers nothing;
- subscribe to user deletion (:meth:`ExternalSignInPort.subscribe_user_deleted`);
- create users without a local password and grant them operator or viewer
  (:meth:`ExternalSignInPort.create_user`, :meth:`ExternalSignInPort.grant_roles`),
  always inside one account and never the administrator role; a grant writes the
  person's role in that account only (ADR 0085), so the person may belong to other
  accounts too; look up and write identity links within one account
  (:meth:`ExternalSignInPort.linked_user`, :meth:`ExternalSignInPort.link_identity`),
  for a member of that account who is an administrator of no account;
- encrypt and decrypt a secret with Core's secret codec, bound to the module: the
  port a module receives in its container view
  (:meth:`ExternalSignInPort.for_platform_module`) encrypts values bound to that
  module and decrypts only values the module, or a module it requires, encrypted;
  Core's own port refuses both (ADR 0082 §12,
  ``core/infrastructure/security/module_secret_codec.py``).

Every registration is rolled back by the platform module loader when the module
fails to load. Writes run through ``connection``, a factory that yields a connection
inside one transaction and commits on a clean exit.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core.application.auth import external_sign_in_repository as repository
from core.application.auth import user_lifecycle_events
from core.infrastructure.security.module_secret_codec import (
    ModuleSecretCodec,
    ModuleSecretScopeError,
)

logger = logging.getLogger(__name__)

ConnectionFactory = Callable[[], AbstractContextManager[Any]]

#: Roles a module may grant through the port. Administrators are made only locally.
GRANTABLE_ROLES = frozenset({"operator", "viewer"})
_METHOD_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
#: Kinds Core reserves: the session values and the built-in trusted header kind.
RESERVED_METHOD_KINDS = frozenset({"local", "external", "header"})
_USER_LIFECYCLE_LOCK = "flow-steward:user-lifecycle"


@dataclass(frozen=True)
class VerifiedExternalIdentity:
    """An identity a sign-in method verified or a trusted header source vouched for.

    ``(issuer, subject)`` is the key. Core stores it under an issuer prefixed with the
    method's kind. ``claims`` are what the method verified; an email address among
    them is informative and never a key. ``account_id`` names the account the
    identity belongs to when the method knows it.
    """

    method: str
    option_key: str
    account_id: str
    issuer: str
    subject: str
    tenant_id: str = ""
    claims: Mapping[str, Any] = field(default_factory=dict)
    #: The entitlement the identity's source needs; empty when none.
    feature: str = ""


@dataclass(frozen=True)
class ExternalIdentityResolution:
    """The user an identity signs in as, and the account of the new session.

    ``account_id`` is the issuing account: when the verified identity names an
    account, it must be that account, or Core refuses the sign-in. The session stays
    pinned to it (``POST /api/auth/switch-account`` refuses external sessions), and
    the user must live only in it and hold no administrator role.
    """

    user_id: str
    account_id: str


class ExternalSignInError(Exception):
    """A sign-in method failed. ``reason`` is a stable code for the audit log.

    The message never carries a token, a code or a secret.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class SignInOption:
    """One button of the sign-in page that a method offers."""

    key: str
    display_name: str
    #: The account the option belongs to, when the method knows it before sign-in.
    account_id: str = ""
    #: The entitlement the option needs; empty when none.
    feature: str = ""


@dataclass(frozen=True)
class BeginResult:
    """What a method answers when a person picks one of its options.

    With ``redirect_url`` Core stores an attempt (``data`` encrypted, bound to the
    browser) and redirects; the person comes back to Core's callback. Without it the
    method completes at once, from the request, as the trusted header kind does.
    """

    redirect_url: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SignInMethod(Protocol):
    """A kind of sign-in. ``complete`` raises :class:`ExternalSignInError` on failure."""

    kind: str

    def options(self) -> Iterable[SignInOption]: ...

    def option(self, key: str) -> SignInOption | None: ...

    def begin(self, key: str, *, callback_url: str, state: str) -> BeginResult: ...

    def complete(
        self,
        key: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        data: Mapping[str, Any],
        callback_url: str,
    ) -> VerifiedExternalIdentity: ...


class ExternalIdentityRefused(Exception):
    """The identity may not sign in. ``reason`` is a stable code for the audit log."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@runtime_checkable
class ExternalIdentityResolver(Protocol):
    def resolve(self, identity: VerifiedExternalIdentity) -> ExternalIdentityResolution: ...


@runtime_checkable
class ExternalSignInHeaderSource(Protocol):
    """A trusted header source. An optional ``account_id`` attribute names the one
    account it signs people in to; Core then lists its option only with that account's
    hint."""

    feature: str
    display_name: str

    def resolve(self, headers: Mapping[str, str]) -> VerifiedExternalIdentity | None: ...


class LinkedIdentityResolver:
    """Core's default: only an identity already linked to a user signs in.

    The identity is looked up in the account its method names. A method that names no
    account finds the one account that linked it; an identity two accounts linked is
    refused as ambiguous. The session's account is the link's account; Core then
    checks the person lives only in it. No user is created and no role is granted.
    """

    def __init__(self, connection: ConnectionFactory) -> None:
        self._connection = connection

    def resolve(self, identity: VerifiedExternalIdentity) -> ExternalIdentityResolution:
        with self._connection() as conn, conn.cursor() as cur:
            if identity.account_id:
                row = repository.find_identity(
                    cur,
                    account_id=identity.account_id,
                    issuer=identity.issuer,
                    subject=identity.subject,
                )
            else:
                rows = repository.find_identities(
                    cur, issuer=identity.issuer, subject=identity.subject
                )
                if len(rows) > 1:
                    raise ExternalIdentityRefused("identity_ambiguous")
                row = rows[0] if rows else None
        if row is None:
            raise ExternalIdentityRefused("identity_not_linked")
        return ExternalIdentityResolution(
            user_id=str(row["user_id"]), account_id=str(row.get("account_id") or "")
        )


class ExternalSignInPort:
    """``container.external_sign_in``: what a platform module may plug into sign-in."""

    def __init__(self, connection: ConnectionFactory) -> None:
        self._connection = connection
        self._lock = threading.Lock()
        self._default_resolver = LinkedIdentityResolver(connection)
        self._resolver: ExternalIdentityResolver | None = None
        self._header_source: ExternalSignInHeaderSource | None = None
        self._methods: dict[str, SignInMethod] = {}

    # --- registration -----------------------------------------------------------------

    def register_sign_in_method(self, method: SignInMethod) -> None:
        """Add a kind of sign-in. Its kind names its sessions' ``auth_method``."""
        kind = str(getattr(method, "kind", "") or "")
        if not isinstance(method, SignInMethod) or not _METHOD_KIND_RE.match(kind):
            raise TypeError(
                "A sign-in method needs a kind of lower-case letters, digits, - or _, and "
                "options(), option(), begin() and complete()"
            )
        with self._lock:
            if kind in RESERVED_METHOD_KINDS or kind in self._methods:
                raise ValueError(f"The sign-in method kind {kind!r} is taken")
            self._methods[kind] = method

    def register_header_source(self, source: ExternalSignInHeaderSource) -> None:
        if not isinstance(source, ExternalSignInHeaderSource) or not str(
            getattr(source, "feature", "") or ""
        ):
            raise TypeError("A trusted header source needs feature, display_name and resolve()")
        with self._lock:
            if self._header_source is not None:
                raise ValueError("A trusted header source is already registered")
            self._header_source = source

    def set_identity_resolver(self, resolver: ExternalIdentityResolver) -> None:
        if not isinstance(resolver, ExternalIdentityResolver):
            raise TypeError("An identity resolver needs resolve(identity)")
        with self._lock:
            if self._resolver is not None:
                raise ValueError("An identity resolver is already set")
            self._resolver = resolver

    def subscribe_user_deleted(self, callback: Callable[[str], None]) -> None:
        user_lifecycle_events.subscribe_user_deleted(callback)

    def loading_checkpoint(self) -> tuple[Any, ...]:
        with self._lock:
            return (self._resolver, self._header_source, dict(self._methods))

    def restore_loading_checkpoint(self, checkpoint: tuple[Any, ...] | None) -> None:
        resolver, header, methods = checkpoint or (None, None, {})
        with self._lock:
            self._resolver = resolver
            self._header_source = header
            self._methods = dict(methods)

    # --- what Core reads --------------------------------------------------------------

    @property
    def resolver(self) -> ExternalIdentityResolver:
        with self._lock:
            return self._resolver or self._default_resolver

    @property
    def header_source(self) -> ExternalSignInHeaderSource | None:
        with self._lock:
            return self._header_source

    @property
    def sign_in_methods(self) -> tuple[SignInMethod, ...]:
        """The methods modules registered, in registration order."""
        with self._lock:
            return tuple(self._methods.values())

    # --- helpers for modules ----------------------------------------------------------

    def for_platform_module(
        self, module_id: str, requires: Iterable[str] = ()
    ) -> ModuleExternalSignInPort:
        """This port as module ``module_id`` sees it: the secret codec bound to it."""
        return ModuleExternalSignInPort(self, ModuleSecretCodec(module_id, requires))

    @staticmethod
    def encrypt_secret(value: str) -> str:
        """Refused on Core's own port: a module uses the port bound to it (ADR 0082 §12)."""
        raise ModuleSecretScopeError(
            "external_sign_in.encrypt_secret is available only on the port Core binds "
            "to a platform module"
        )

    @staticmethod
    def decrypt_secret(value: str) -> str:
        """Refused on Core's own port: a module uses the port bound to it (ADR 0082 §12)."""
        raise ModuleSecretScopeError(
            "external_sign_in.decrypt_secret is available only on the port Core binds "
            "to a platform module"
        )

    def linked_user(self, *, account_id: str, issuer: str, subject: str) -> str | None:
        """The id of the user ``account_id`` linked ``(issuer, subject)`` to, if any.

        Links are unique within an account; another account's link is never seen.
        """
        with self._connection() as conn, conn.cursor() as cur:
            row = repository.find_identity(
                cur, account_id=account_id, issuer=issuer, subject=subject
            )
        return str(row["user_id"]) if row else None

    def link_identity(
        self,
        *,
        account_id: str,
        user_id: str,
        method: str,
        issuer: str,
        subject: str,
        option_key: str = "",
        created_by: str = "",
    ) -> str:
        """Link ``(issuer, subject)`` of method ``method`` to a user of ``account_id``.

        The issuer is stored under the method's namespace, as Core stores it at sign-in.
        Returns the link's id. Refused with :class:`LookupError` for a person who is
        not a member of the account, :class:`PermissionError` for an administrator of
        any account (the link would let this account's sign-in source sign in as
        them), and :class:`ValueError` for an identity the account already linked to
        another user. Memberships of other accounts do not matter: the session the
        link signs in is pinned to this account and holds the role there (ADR 0085).
        """
        kind = str(method or "")
        if not _METHOD_KIND_RE.match(kind) or kind in {"local", "external"}:
            raise ValueError("Unknown sign-in method kind")
        subject = str(subject or "").strip()
        if not subject or len(subject) > 255 or not str(issuer or "").strip():
            raise ValueError("An identity needs an issuer and a subject")
        with self._connection() as conn, conn.cursor() as cur:
            if not repository.user_in_account(cur, user_id=user_id, account_id=account_id):
                raise LookupError("The user is not a member of the account")
            refusal = repository.external_person_refusal(
                cur, user_id=user_id, account_id=account_id
            )
            if not refusal:
                try:
                    row = repository.link_identity(
                        cur,
                        account_id=account_id,
                        user_id=user_id,
                        method=kind,
                        option_key=str(option_key or ""),
                        issuer=namespaced_issuer(kind, issuer),
                        subject=subject,
                        created_by=str(created_by or "module"),
                    )
                except repository.IdentityLinkedElsewhereError as exc:
                    raise ValueError(str(exc)) from exc
        if refusal:
            logger.warning(
                "External identity link refused (%s): user %s, account %s",
                refusal,
                user_id,
                account_id,
            )
            self._audit_refusal(
                "external_identity_link",
                reason=refusal,
                user_id=user_id,
                account_id=account_id,
                actor=str(created_by or "module"),
                detail={"method": kind},
            )
            raise PermissionError(refusal)
        return str(row["id"])

    def create_user(
        self,
        *,
        account_id: str,
        email: str,
        display_name: str = "",
        roles: Iterable[str] = ("viewer",),
        active: bool = True,
    ) -> str:
        """Create a user of ``account_id`` without a local password; returns its id.

        The user can sign in only externally (its password hash never verifies).
        Refused with :class:`ValueError` for an unknown or inactive account, a taken
        username, an installation without users (the first user is an administrator
        created locally) or a role other than :data:`GRANTABLE_ROLES`.
        """
        granted = _grantable(roles)
        username = str(email or "").strip().lower()
        if not username or "@" not in username or len(username) > 320:
            raise ValueError("A user needs an email address")
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_USER_LIFECYCLE_LOCK,))
            _require_active_account(cur, account_id)
            cur.execute("SELECT EXISTS (SELECT 1 FROM users)")
            if not _first_value(cur.fetchone()):
                raise ValueError("The first user is created locally")
            cur.execute("SELECT 1 FROM users WHERE lower(username) = %s", (username,))
            if cur.fetchone():
                raise ValueError("A user with this email already exists")
            cur.execute(
                """
                INSERT INTO users (username, password_hash, display_name, is_active, account_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id::text
                """,
                (
                    username,
                    repository.UNUSABLE_PASSWORD_MARKER,
                    str(display_name or "").strip() or username,
                    bool(active),
                    account_id,
                ),
            )
            user_id = str(_first_value(cur.fetchone()) or "")
            cur.execute(
                """
                INSERT INTO user_account_memberships (user_id, account_id, is_default)
                VALUES (%s, %s, true)
                """,
                (user_id, account_id),
            )
            _grant(cur, user_id=user_id, account_id=account_id, roles=granted)
        return user_id

    def grant_roles(self, *, account_id: str, user_id: str, roles: Iterable[str]) -> list[str]:
        """Add operator or viewer to a user of ``account_id``; never removes a role.

        The grant writes the person's role in ``account_id`` only (ADR 0085), so a
        person who also belongs to other accounts may be granted a role here (ADR 0083
        amendment of 2026-11-27): their role elsewhere does not change. Refused with
        :class:`LookupError` for a person who is not a member of the account. A user
        keeps the highest system role. Returns the stored roles in the account.
        """
        granted = _grantable(roles)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_USER_LIFECYCLE_LOCK,))
            if not repository.user_in_account(cur, user_id=user_id, account_id=account_id):
                raise LookupError("The user is not a member of the account")
            return _grant(cur, user_id=user_id, account_id=account_id, roles=granted)

    def remove_role(self, *, account_id: str, user_id: str) -> list[str]:
        """Take away the person's role in ``account_id`` (a directory sync, ADR 0085).

        The person keeps the membership without a role (no access there) and loses
        their sessions, personal tokens and OAuth grants of that account; other
        accounts are untouched. Refused with :class:`LookupError` for a person who is
        not a member and with :class:`ValueError` for the account's last active
        administrator. Returns the stored roles in the account (``[]``).
        """
        from core.application.auth.membership_roles import (
            LastAccountAdminError,
            cancel_invitations_of_former_admins,
            require_other_admins,
            revoke_account_access,
            set_membership_role,
        )

        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_USER_LIFECYCLE_LOCK,))
            if not repository.user_in_account(cur, user_id=user_id, account_id=account_id):
                raise LookupError("The user is not a member of the account")
            try:
                require_other_admins(cur, user_id=user_id, account_ids=[account_id])
            except LastAccountAdminError as exc:
                raise ValueError(str(exc)) from exc
            stored = set_membership_role(cur, user_id=user_id, account_id=account_id, role=None)
            revoke_account_access(
                cur, user_id=user_id, account_id=account_id, reason="account_role_removed"
            )
            # An administrator without the role no longer invites (ADR 0085, C4).
            cancel_invitations_of_former_admins(cur, user_id)
            return stored

    def _audit_refusal(
        self,
        action: str,
        *,
        reason: str,
        user_id: str,
        account_id: str,
        actor: str,
        detail: Mapping[str, Any],
    ) -> None:
        """Record a refused link or grant in the auth audit, in its own transaction.

        The refused write's transaction is rolled back, so the entry is written after
        it; an audit failure is logged and never turns the refusal into another error.
        """
        from core.application.auth.scoped_role_ports import record_scope_limit_audit

        try:
            with self._connection() as conn, conn.cursor() as cur:
                record_scope_limit_audit(
                    cur,
                    action=action,
                    actor=actor,
                    username="",
                    status="rejected",
                    detail={
                        "reason": reason,
                        "user_id": str(user_id or ""),
                        "account_id": str(account_id or ""),
                        **dict(detail),
                    },
                )
        except Exception:
            logger.warning("Auth audit of a refused %s could not be written", action)


def _first_value(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    return row[0]


def _grantable(roles: Iterable[str]) -> list[str]:
    requested = [str(role or "").strip().lower() for role in roles or ()]
    refused = sorted({role for role in requested if role not in GRANTABLE_ROLES})
    if refused:
        raise ValueError(f"Roles that cannot be granted here: {', '.join(refused)}")
    return requested


def _require_active_account(cur: Any, account_id: str) -> None:
    if not repository.is_uuid(account_id):
        raise ValueError("Unknown account")
    cur.execute("SELECT is_active FROM accounts WHERE id = %s", (account_id,))
    row = cur.fetchone()
    if row is None or not _first_value(row):
        raise ValueError("Unknown or inactive account")


def _grant(cur: Any, *, user_id: str, account_id: str, roles: list[str]) -> list[str]:
    """Raise the person's role in ``account_id`` to the highest of theirs and ``roles``.

    The role is the membership's (ADR 0085), so a grant never reaches another account.
    """
    from core.application.auth.external_session_revocation import (
        revoke_external_sign_in_grants,
        roles_changed,
    )
    from core.application.auth.membership_roles import membership_role_keys
    from core.application.auth.role_assignments import (
        global_assignments,
        replace_user_role_assignments,
    )

    existing = membership_role_keys(cur, user_id, account_id)
    if not roles:
        return existing
    stored = replace_user_role_assignments(
        cur,
        user_id=user_id,
        account_id=account_id,
        assignments=global_assignments([*existing, *roles]),
    )
    if roles_changed(existing, stored):
        # ADR 0083: the person's sessions from external sign-in were issued under the
        # roles they had; a grant ends them and their tokens.
        revoke_external_sign_in_grants(cur, [user_id])
    return stored


class ModuleExternalSignInPort:
    """:class:`ExternalSignInPort` as one platform module sees it.

    Every attribute is Core's port's, except the secret codec, which is bound to the
    module: ``encrypt_secret`` writes a value bound to it, ``decrypt_secret``
    reads only the module's own values and those of the modules it requires, and
    ``reseal_legacy_secrets`` re-encrypts values the module stored before the
    binding (see :class:`~core.infrastructure.security.module_secret_codec.ModuleSecretCodec`).
    """

    def __init__(self, port: ExternalSignInPort, codec: ModuleSecretCodec) -> None:
        self._port = port
        self._codec = codec

    @property
    def module_id(self) -> str:
        return self._codec.module_id

    def encrypt_secret(self, value: str) -> str:
        return self._codec.encrypt(str(value or ""))

    def decrypt_secret(self, value: str) -> str:
        return self._codec.decrypt(str(value or "")) if value else ""

    def reseal_legacy_secrets(
        self, cursor: Any, *, table: str, column: str, json_key: str | None = None
    ) -> int:
        return self._codec.reseal_legacy_secrets(
            cursor, table=table, column=column, json_key=json_key
        )

    def __getattr__(self, name: str) -> Any:
        # A bound port cannot be re-bound to another module identity.
        if name == "for_platform_module" or name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._port, name)


def namespaced_issuer(kind: str, issuer: str) -> str:
    """The issuer an identity of method ``kind`` is stored under.

    Every identity lives under an issuer prefixed with its method's kind, so one
    method can never name an identity another one verified.
    """
    text = str(issuer or "").strip()
    prefix = f"{kind}:"
    return text if text.startswith(prefix) else f"{prefix}{text}"


__all__ = [
    "GRANTABLE_ROLES",
    "RESERVED_METHOD_KINDS",
    "BeginResult",
    "ExternalIdentityRefused",
    "ExternalIdentityResolution",
    "ExternalIdentityResolver",
    "ExternalSignInError",
    "ExternalSignInHeaderSource",
    "ExternalSignInPort",
    "LinkedIdentityResolver",
    "ModuleExternalSignInPort",
    "SignInMethod",
    "SignInOption",
    "VerifiedExternalIdentity",
    "namespaced_issuer",
]
