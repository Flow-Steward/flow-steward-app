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
import time
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
    #: Set by Core, never by the method: the identity comes from a method declared
    #: ``installation_wide`` (its options belong to no account). Such an identity is
    #: the person's own sign-in, so it may sign in an administrator of an ordinary
    #: account (never of the installation account), and its feature is checked for
    #: the installation account.
    installation_wide: bool = False
    #: Set by Core, never by the method: the account of the open invitation the
    #: person is answering, when the sign-in returns to an invitation link
    #: (``/invitations/<token>``, ADR 0085 C1); empty otherwise.
    invitation_account_id: str = ""
    #: Set by Core with ``invitation_account_id``: which invitation link it is (a link
    #: the administrator renews gets a new value).
    invitation_id: str = ""


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
    #: What the resolver created for this sign-in, undone when Core refuses the
    #: sign-in after resolving (inactive person, entitlement, session guard, link).
    undo: Callable[[], None] | None = field(default=None, compare=False, repr=False)
    #: Called once after the session is issued (e.g. to tell other modules).
    after_session: Callable[[], None] | None = field(default=None, compare=False, repr=False)


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
    """A kind of sign-in. ``complete`` raises :class:`ExternalSignInError` on failure.

    A method may declare ``installation_wide = True``: it is set up once for the
    whole installation, its options belong to no account (Core ignores an option's
    ``account_id``) and its identities name none. Core then marks each identity
    ``installation_wide`` (see :class:`VerifiedExternalIdentity`) and checks an
    option's feature for the installation account.
    """

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
    """The identity may not sign in. ``reason`` is a stable code for the audit log.

    ``public_message`` is an optional plain-language sentence for the person (at most
    240 characters, no personal data); Core shows it on the sign-in page instead of
    its generic text.
    """

    def __init__(self, reason: str, message: str = "", *, public_message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.public_message = str(public_message or "").strip()[:240]


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

    def __init__(
        self,
        connection: ConnectionFactory,
        entitlement_service: Callable[[], Any] | None = None,
    ) -> None:
        self._connection = connection
        self._entitlement_service = entitlement_service
        self._lock = threading.Lock()
        self._default_resolver = LinkedIdentityResolver(connection)
        self._resolver: ExternalIdentityResolver | None = None
        self._header_source: ExternalSignInHeaderSource | None = None
        self._methods: dict[str, SignInMethod] = {}
        #: What this process created for sign-ins, discardable once: user -> account.
        self._created: dict[str, tuple[str, float]] = {}

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
        awaiting_invitation: bool = False,
    ) -> str:
        """Create a user of ``account_id`` without a local password; returns its id.

        The user can sign in only externally (its password hash never verifies).
        ``awaiting_invitation`` marks a person created only to answer an invitation:
        they take no role (``roles`` must be empty), are left out of user lists and
        get no role in the installation account until accepting moves them into the
        company they joined.
        Refused with :class:`ValueError` for an unknown or inactive account, a taken
        username, an installation without users (the first user is an administrator
        created locally) or a role other than :data:`GRANTABLE_ROLES`.
        """
        granted = _grantable(roles)
        if awaiting_invitation and granted:
            raise ValueError("A person awaiting an invitation takes no role")
        marker = (
            repository.AWAITING_INVITATION_MARKER
            if awaiting_invitation
            else repository.UNUSABLE_PASSWORD_MARKER
        )
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
                    marker,
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
        self._remember_created(user_id, "")
        return user_id

    def create_own_account(
        self,
        *,
        email: str,
        display_name: str = "",
        account_name: str = "",
        created_by: str,
        before: Callable[[Any], None] | None = None,
        after: Callable[[Any, Mapping[str, str]], None] | None = None,
    ) -> dict[str, str]:
        """Self-service sign-up: a new account with a new person as its administrator.

        For a method that verified a person nobody invited and that an installation
        setting lets create their own account. In one transaction under the account
        lifecycle lock: the ``accounts`` limit of the installation is checked (as for
        an installation administrator creating an account), the account is created,
        the person is created without a local password, made a member with the
        administrator role, and ``account_create`` is audited (``source``
        ``self_service``, actor ``created_by``). Returns ``{"account_id", "user_id"}``.

        Refused with :class:`~core.application.auth.account_limit.AccountLimitError`
        when the limit is reached (409) or unreadable (503), and :class:`ValueError`
        for a taken email address, an installation without users, or no limit source.

        ``before(cursor)`` runs in the same transaction, under the lock, after Core's
        checks and before anything is written: a module checks its own quota there
        and raises to refuse. ``after(cursor, created)`` runs before the commit: a
        module records the new account there, so both commit or neither does.
        """
        import uuid

        from core.application.auth.account_limit import (
            account_limit_unavailable,
            require_account_creation_allowed,
        )
        from core.application.auth.membership_roles import set_membership_role
        from core.application.auth.permissions import ADMIN_ROLE
        from core.application.auth.scoped_role_ports import record_scope_limit_audit
        from core.infrastructure.db.logger_extensions_accounts_accounts_mixin import (
            ACCOUNT_LIFECYCLE_LOCK_KEY,
        )

        username = str(email or "").strip().lower()
        if not username or "@" not in username or len(username) > 320:
            raise ValueError("A user needs an email address")
        actor = str(created_by or "").strip()
        if not actor:
            raise ValueError("created_by is required")
        service = self._entitlement_service() if callable(self._entitlement_service) else None
        if service is None:
            raise account_limit_unavailable("no entitlement service")
        name = str(display_name or "").strip() or username
        company = (str(account_name or "").strip() or name)[:200]
        account_id = str(uuid.uuid4())
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ACCOUNT_LIFECYCLE_LOCK_KEY,))
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_USER_LIFECYCLE_LOCK,))
            cur.execute("SELECT EXISTS (SELECT 1 FROM users)")
            if not _first_value(cur.fetchone()):
                raise ValueError("The first user is created locally")
            cur.execute("SELECT 1 FROM users WHERE lower(username) = %s", (username,))
            if cur.fetchone():
                raise ValueError("A user with this email already exists")

            def count_active() -> int:
                cur.execute("SELECT count(*) FROM accounts WHERE is_active")
                return int(_first_value(cur.fetchone()) or 0)

            require_account_creation_allowed(service, count_active)
            if before is not None:
                before(cur)
            cur.execute(
                """
                INSERT INTO accounts (id, account_key, display_name, is_active)
                VALUES (%s, %s, %s, true)
                """,
                (account_id, account_id, company),
            )
            cur.execute(
                """
                INSERT INTO users (username, password_hash, display_name, is_active, account_id)
                VALUES (%s, %s, %s, true, %s)
                RETURNING id::text
                """,
                (username, repository.UNUSABLE_PASSWORD_MARKER, name, account_id),
            )
            user_id = str(_first_value(cur.fetchone()) or "")
            cur.execute(
                """
                INSERT INTO user_account_memberships (user_id, account_id, is_default)
                VALUES (%s, %s, true)
                """,
                (user_id, account_id),
            )
            set_membership_role(cur, user_id=user_id, account_id=account_id, role=ADMIN_ROLE)
            record_scope_limit_audit(
                cur,
                action="account_create",
                actor=actor,
                username=account_id,
                detail={
                    "account_id": account_id,
                    "is_active": True,
                    "source": "self_service",
                    "user_id": user_id,
                },
            )
            if after is not None:
                after(cur, {"account_id": account_id, "user_id": user_id})
        self._remember_created(user_id, account_id)
        return {"account_id": account_id, "user_id": user_id}

    def _remember_created(self, user_id: str, account_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in [k for k, (_a, at) in self._created.items() if now - at > 3600]:
                del self._created[key]
            self._created[user_id] = (account_id, now)

    def discard_new_person(self, *, user_id: str, account_id: str = "", reason: str) -> bool:
        """Undo :meth:`create_user` or :meth:`create_own_account` of a sign-in Core then
        refused, so neither the address nor the account limit stays taken.

        Only a person who never had a local password, signed in no session and was
        created in the last hour is removed (with their memberships and links);
        ``account_id``, when given, goes too if that person was its only member and
        it was created in the last hour (otherwise it is disabled, so it no longer
        counts against the limit). Audited as ``external_person_discard``. Returns
        whether the person was removed.

        Only what :meth:`create_user` or :meth:`create_own_account` of this process
        created can be discarded, once, and an account only with the person it was
        created with: a module cannot name anybody else.
        """
        with self._lock:
            created = self._created.pop(user_id, None)
            if created is None or created[0] != str(account_id or ""):
                return False
        if not repository.is_uuid(user_id):
            return False
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_USER_LIFECYCLE_LOCK,))
            cur.execute(
                """
                SELECT 1 FROM users u
                 WHERE u.id = %s AND u.password_hash = ANY(%s)
                   AND u.created_at > now() - interval '1 hour'
                   AND NOT EXISTS (SELECT 1 FROM auth_sessions s WHERE s.user_id = u.id)
                """,
                (user_id, list(repository.UNUSABLE_PASSWORD_MARKERS)),
            )
            if cur.fetchone() is None:
                return False
            cur.execute("DELETE FROM external_identities WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM user_account_memberships WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            account_removed = False
            if repository.is_uuid(account_id):
                cur.execute(
                    """
                    SELECT 1 FROM accounts a
                     WHERE a.id = %s AND a.created_at > now() - interval '1 hour'
                       AND NOT EXISTS (
                           SELECT 1 FROM user_account_memberships m WHERE m.account_id = a.id)
                    """,
                    (account_id,),
                )
                if cur.fetchone() is not None:
                    cur.execute("SAVEPOINT discard_account")
                    try:
                        cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
                        account_removed = True
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT discard_account")
                    if not account_removed:
                        cur.execute(
                            "UPDATE accounts SET is_active = false, updated_at = now() "
                            "WHERE id = %s",
                            (account_id,),
                        )
            from core.application.auth.scoped_role_ports import record_scope_limit_audit

            record_scope_limit_audit(
                cur,
                action="external_person_discard",
                actor="external-sign-in",
                username="",
                detail={
                    "user_id": user_id,
                    "account_id": str(account_id or ""),
                    "account_removed": account_removed,
                    "reason": str(reason or ""),
                },
            )
        return True

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
        administrator and for its owner (ADR 0085, "Company owner": the owner keeps the
        role until ownership is transferred). Returns the stored roles in the account
        (``[]``).
        """
        from core.application.auth.account_owner import (
            AccountOwnerProtectedError,
            require_owner_kept,
        )
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
            try:
                require_owner_kept(cur, user_id=user_id, account_ids=[account_id])
            except AccountOwnerProtectedError as exc:
                raise ValueError(exc.message) from exc
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
