"""Requests to install an extension, from a company account (ADR 0084).

Extension code is installed once per installation, and only the installation
administrator installs it (``core.application.auth.installation_admin``). An account
administrator of any other account asks for a catalog extension here instead of outside
the product:

* the account administrator requests it (one pending request per account and
  extension), may cancel it, and sees the outcome of their account's requests;
* the installation administrator lists every pending request, and either installs the
  extension and approves the request, which enables it for the asking account, or
  declines it with an optional reason.

Installing is not done here. The installation administrator installs the catalog
release through the existing catalog install call, the one an Install button already
uses (and which a Compact deployment hands to its installer process); :meth:`approve`
then only checks that the extension is installed, enables it for the account through
:class:`ExtensionAccountEnablementService` and records the decision. A failed install
therefore leaves the request pending.

Every change is written to the action audit trail. Authorization fails closed: a caller
without an account, a non-administrator, or an administrator of another account is
refused, and another account's request is answered as missing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from core.application.auth.installation_admin import (
    INSTALLATION_ACCOUNT_ID,
    INSTALLATION_ADMIN_REQUIRED_CODE,
    INSTALLATION_ADMIN_REQUIRED_MESSAGE,
    is_installation_account,
    is_installation_admin,
)
from core.application.auth.principal import principal_type_has_minimum_role
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.infrastructure.extension_runtime.package_identity import CANONICAL_EXTENSION_ID_RE

logger = logging.getLogger(__name__)

PENDING = "pending"
APPROVED = "approved"
DECLINED = "declined"
CANCELLED = "cancelled"
STATUSES = (PENDING, APPROVED, DECLINED, CANCELLED)

MAX_NOTE_CHARS = 500
MAX_REASON_CHARS = 500
MAX_NAME_CHARS = 200
#: How many of its own requests an account sees: the latest ones, newest first.
ACCOUNT_LIST_LIMIT = 200
#: How many pending requests the installation administrator sees at once.
PENDING_LIST_LIMIT = 500

ALREADY_INSTALLED_CODE = "extension_already_installed"
ALREADY_REQUESTED_CODE = "extension_install_already_requested"
NOT_INSTALLED_CODE = "extension_not_installed"
NOT_PENDING_CODE = "extension_install_request_not_pending"
INSTALLATION_ACCOUNT_CODE = "extension_install_request_installation_account"

_AUDIT_TASK = "extension_install_requests"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value)


def _uuid_or_none(value: Any) -> str | None:
    """A ``users.id`` the database can hold, or ``None`` (a service account, a test)."""
    text = _text(value)
    try:
        return str(uuid.UUID(text)) if text else None
    except ValueError:
        return None


@dataclass(frozen=True)
class ExtensionInstallRequest:
    request_id: str
    account_id: str
    extension_id: str
    extension_name: str = ""
    requested_by: str = ""
    note: str = ""
    status: str = PENDING
    decided_by: str = ""
    decided_at: str = ""
    decline_reason: str = ""
    created_at: str = ""
    #: Display names joined for the installation administrator's list; never stored.
    account_name: str = ""
    requested_by_name: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "id": self.request_id,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "extension_id": self.extension_id,
            "extension_name": self.extension_name or self.extension_id,
            "requested_by": self.requested_by,
            "requested_by_name": self.requested_by_name,
            "note": self.note,
            "status": self.status,
            "decided_at": self.decided_at,
            "decline_reason": self.decline_reason,
            "created_at": self.created_at,
        }


def _row_to_request(row: Mapping[str, Any]) -> ExtensionInstallRequest:
    return ExtensionInstallRequest(
        request_id=_text(row.get("id")),
        account_id=_text(row.get("account_id")),
        extension_id=_text(row.get("extension_id")),
        extension_name=_text(row.get("extension_name")),
        requested_by=_text(row.get("requested_by")),
        note=_text(row.get("note")),
        status=_text(row.get("status")) or PENDING,
        decided_by=_text(row.get("decided_by")),
        decided_at=_iso(row.get("decided_at")),
        decline_reason=_text(row.get("decline_reason")),
        created_at=_iso(row.get("created_at")),
        account_name=_text(row.get("account_name")),
        requested_by_name=_text(row.get("requested_by_name")),
    )


_COLUMNS = (
    "r.id::text AS id, r.account_id::text AS account_id, r.extension_id, r.extension_name,"
    " r.requested_by::text AS requested_by, r.note, r.status,"
    " r.decided_by::text AS decided_by, r.decided_at, r.decline_reason, r.created_at"
)
_NAMES = (
    "COALESCE(NULLIF(a.display_name, ''), a.account_key, '') AS account_name,"
    " COALESCE(NULLIF(u.display_name, ''), u.username, '') AS requested_by_name"
)
_JOINS = (
    " FROM extension_install_requests r"
    " LEFT JOIN accounts a ON a.id = r.account_id"
    " LEFT JOIN users u ON u.id = r.requested_by"
)


class DBExtensionInstallRequestStore:
    """PostgreSQL store of ``extension_install_requests`` (owned by ``db/schema``)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def _rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            names = [column[0] for column in cur.description or ()]
            return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]

    def create(
        self,
        *,
        account_id: str,
        extension_id: str,
        extension_name: str,
        requested_by: str | None,
        note: str,
    ) -> ExtensionInstallRequest | None:
        """The new pending request, or ``None`` when one is already pending."""
        rows = self._rows(
            f"""
            WITH r AS (
                INSERT INTO extension_install_requests (
                    account_id, extension_id, extension_name, requested_by, note
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (account_id, extension_id) WHERE status = 'pending' DO NOTHING
                RETURNING *
            )
            SELECT {_COLUMNS}, {_NAMES}
            FROM r
            LEFT JOIN accounts a ON a.id = r.account_id
            LEFT JOIN users u ON u.id = r.requested_by
            """,  # nosec B608 - constant column and join lists; values are bound parameters
            (account_id, extension_id, extension_name, requested_by, note),
        )
        return _row_to_request(rows[0]) if rows else None

    def get(self, request_id: str) -> ExtensionInstallRequest | None:
        if _uuid_or_none(request_id) is None:
            return None
        rows = self._rows(f"SELECT {_COLUMNS}, {_NAMES}{_JOINS} WHERE r.id = %s", (request_id,))
        return _row_to_request(rows[0]) if rows else None

    def pending_for(self, *, account_id: str, extension_id: str) -> ExtensionInstallRequest | None:
        rows = self._rows(
            f"SELECT {_COLUMNS}, {_NAMES}{_JOINS}"
            " WHERE r.account_id = %s AND r.extension_id = %s AND r.status = 'pending'",
            (account_id, extension_id),
        )
        return _row_to_request(rows[0]) if rows else None

    def list_for_account(self, account_id: str) -> list[ExtensionInstallRequest]:
        rows = self._rows(
            f"SELECT {_COLUMNS}, {_NAMES}{_JOINS}"
            " WHERE r.account_id = %s ORDER BY r.created_at DESC, r.id LIMIT %s",
            (account_id, ACCOUNT_LIST_LIMIT),
        )
        return [_row_to_request(row) for row in rows]

    def list_pending(self) -> list[ExtensionInstallRequest]:
        rows = self._rows(
            f"SELECT {_COLUMNS}, {_NAMES}{_JOINS}"
            " WHERE r.status = 'pending' ORDER BY r.created_at, r.id LIMIT %s",
            (PENDING_LIST_LIMIT,),
        )
        return [_row_to_request(row) for row in rows]

    def decide(
        self,
        request_id: str,
        *,
        status: str,
        decided_by: str | None,
        decline_reason: str = "",
    ) -> ExtensionInstallRequest | None:
        """Close a pending request; ``None`` when it is not pending any more."""
        rows = self._rows(
            f"""
            WITH r AS (
                UPDATE extension_install_requests
                   SET status = %s, decided_by = %s, decided_at = now(), decline_reason = %s
                 WHERE id = %s AND status = 'pending'
                RETURNING *
            )
            SELECT {_COLUMNS}, {_NAMES}
            FROM r
            LEFT JOIN accounts a ON a.id = r.account_id
            LEFT JOIN users u ON u.id = r.requested_by
            """,  # nosec B608 - constant column and join lists; values are bound parameters
            (status, decided_by, decline_reason, request_id),
        )
        return _row_to_request(rows[0]) if rows else None


class InMemoryExtensionInstallRequestStore:
    """The same contract in memory, for tests and offline tools."""

    def __init__(self, *, account_names: Mapping[str, str] | None = None) -> None:
        self._rows: dict[str, ExtensionInstallRequest] = {}
        self._order: list[str] = []
        self._account_names = dict(account_names or {})

    def _named(self, row: ExtensionInstallRequest) -> ExtensionInstallRequest:
        return replace(row, account_name=self._account_names.get(row.account_id, ""))

    def create(
        self,
        *,
        account_id: str,
        extension_id: str,
        extension_name: str,
        requested_by: str | None,
        note: str,
    ) -> ExtensionInstallRequest | None:
        if self.pending_for(account_id=account_id, extension_id=extension_id) is not None:
            return None
        row = ExtensionInstallRequest(
            request_id=str(uuid.uuid4()),
            account_id=account_id,
            extension_id=extension_id,
            extension_name=extension_name,
            requested_by=requested_by or "",
            note=note,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._rows[row.request_id] = row
        self._order.append(row.request_id)
        return self._named(row)

    def get(self, request_id: str) -> ExtensionInstallRequest | None:
        row = self._rows.get(_text(request_id))
        return self._named(row) if row is not None else None

    def pending_for(self, *, account_id: str, extension_id: str) -> ExtensionInstallRequest | None:
        for row in self._rows.values():
            if (
                row.account_id == account_id
                and row.extension_id == extension_id
                and row.status == PENDING
            ):
                return self._named(row)
        return None

    def list_for_account(self, account_id: str) -> list[ExtensionInstallRequest]:
        rows = [self._rows[key] for key in reversed(self._order)]
        return [self._named(row) for row in rows if row.account_id == account_id][
            :ACCOUNT_LIST_LIMIT
        ]

    def list_pending(self) -> list[ExtensionInstallRequest]:
        rows = [self._rows[key] for key in self._order]
        return [self._named(row) for row in rows if row.status == PENDING][:PENDING_LIST_LIMIT]

    def decide(
        self,
        request_id: str,
        *,
        status: str,
        decided_by: str | None,
        decline_reason: str = "",
    ) -> ExtensionInstallRequest | None:
        row = self._rows.get(_text(request_id))
        if row is None or row.status != PENDING:
            return None
        decided = replace(
            row,
            status=status,
            decided_by=decided_by or "",
            decided_at=datetime.now(UTC).isoformat(),
            decline_reason=decline_reason,
        )
        self._rows[row.request_id] = decided
        return self._named(decided)


def _admin_scope(scope: Any) -> bool:
    """An administrator by principal type; a token also needs ``admin:all``."""
    if scope is None:
        return False
    if not principal_type_has_minimum_role(_text(getattr(scope, "principal_type", "")), "admin"):
        return False
    token_scopes = frozenset(getattr(scope, "token_scopes", frozenset()) or frozenset())
    return not token_scopes or "admin:all" in token_scopes


def can_request_extension_installs(scope: Any) -> bool:
    """Whether the caller is an account administrator of a company account.

    Only they request installation; the installation administrator installs directly,
    so a single-account installation (only the installation account) never offers it.
    """
    account_id = _text(getattr(scope, "account_id", "")) if scope is not None else ""
    return bool(account_id) and not is_installation_account(account_id) and _admin_scope(scope)


def _bounded(value: Any, *, limit: int, label: str) -> str:
    text = _text(value)
    if len(text) > limit:
        raise ServiceValidationError(f"{label} must be at most {limit} characters")
    return text


def record_install_request_audit(
    get_logger: Callable[[], Any] | None,
    *,
    action: str,
    actor: str,
    row: ExtensionInstallRequest,
) -> None:
    """Write one request change to the action audit trail; a failed write is logged."""
    if get_logger is None:
        return
    try:
        bl = get_logger()
        log_action = getattr(bl, "log_action", None)
        if not callable(log_action):
            return
        log_action(
            project_id=None,
            job_id=None,
            task_id=_AUDIT_TASK,
            task_summary=f"Extension install request {action}",
            agent=_text(actor) or "system",
            action_type=f"extension_install_request_{action}",
            status="ok",
            payload={
                "request_id": row.request_id,
                "account_id": row.account_id,
                "extension_id": row.extension_id,
            },
            result={"status": row.status, "decline_reason": row.decline_reason},
            meta={"audit_category": "extensions", "account_id": row.account_id},
        )
    except Exception as exc:  # the decision stands; a missing audit row is reported
        logger.warning(
            "Extension install request audit write failed for %s: %s", action, type(exc).__name__
        )


class ExtensionInstallRequestService:
    """Request, cancel, list, approve and decline extension install requests."""

    def __init__(
        self,
        *,
        get_store: Callable[[], Any],
        get_install_store: Callable[[], Any],
        get_enablement_service: Callable[[], Any],
        catalog_name: Callable[[str], str | None],
        get_audit_logger: Callable[[], Any] | None = None,
    ) -> None:
        self._get_store = get_store
        self._get_install_store = get_install_store
        self._get_enablement_service = get_enablement_service
        self._catalog_name = catalog_name
        self._get_audit_logger = get_audit_logger

    # --- who may do what --------------------------------------------------------------

    @staticmethod
    def _account_admin(scope: ScopeContext | None) -> ScopeContext:
        if scope is None or not _text(scope.account_id) or not _admin_scope(scope):
            raise ServiceForbiddenError("Only an account administrator can do this")
        if is_installation_account(scope.account_id):
            raise ServiceValidationError(
                "The installation administrator installs extensions directly",
                payload={
                    "error": "The installation administrator installs extensions directly",
                    "code": INSTALLATION_ACCOUNT_CODE,
                },
            )
        return scope

    @staticmethod
    def _installation_admin(scope: ScopeContext | None) -> ScopeContext:
        if scope is None or not is_installation_admin(scope):
            raise ServiceForbiddenError(
                INSTALLATION_ADMIN_REQUIRED_MESSAGE,
                payload={
                    "error": INSTALLATION_ADMIN_REQUIRED_MESSAGE,
                    "code": INSTALLATION_ADMIN_REQUIRED_CODE,
                },
            )
        return scope

    @staticmethod
    def _actor(scope: ScopeContext) -> str:
        return _text(scope.actor_id) or _text(scope.user_id)

    def _audit(self, action: str, scope: ScopeContext, row: ExtensionInstallRequest) -> None:
        record_install_request_audit(
            self._get_audit_logger, action=action, actor=self._actor(scope), row=row
        )

    def _installation_install(self, extension_id: str) -> Any:
        return self._get_install_store().get(
            extension_id=extension_id, account_id=INSTALLATION_ACCOUNT_ID
        )

    @staticmethod
    def _answered() -> ServiceConflictError:
        message = "This request has already been answered"
        return ServiceConflictError(message, payload={"error": message, "code": NOT_PENDING_CODE})

    def _pending(self, row: ExtensionInstallRequest | None) -> ExtensionInstallRequest:
        if row is None:
            raise ServiceNotFoundError("Installation request not found")
        if row.status != PENDING:
            raise self._answered()
        return row

    # --- the account administrator ----------------------------------------------------

    def create(
        self, extension_id: Any, *, note: Any = "", scope: ScopeContext | None
    ) -> dict[str, Any]:
        resolved = self._account_admin(scope)
        account_id = _text(resolved.account_id)
        normalized_id = _text(extension_id)
        if not CANONICAL_EXTENSION_ID_RE.fullmatch(normalized_id):
            raise ServiceValidationError("Choose an extension from the catalog")
        normalized_note = _bounded(note, limit=MAX_NOTE_CHARS, label="The note")
        install = self._installation_install(normalized_id)
        if install is not None:
            message = (
                "This extension is already installed on this server; enable it for your company"
            )
            raise ServiceConflictError(
                message, payload={"error": message, "code": ALREADY_INSTALLED_CODE}
            )
        name = self._catalog_name(normalized_id)
        if name is None:
            raise ServiceNotFoundError(f"Extension '{normalized_id}' is not in the catalog")
        store = self._get_store()
        row = store.create(
            account_id=account_id,
            extension_id=normalized_id,
            extension_name=_text(name)[:MAX_NAME_CHARS],
            requested_by=_uuid_or_none(resolved.user_id),
            note=normalized_note,
        )
        if row is None:
            message = "Installation of this extension has already been requested"
            raise ServiceConflictError(
                message, payload={"error": message, "code": ALREADY_REQUESTED_CODE}
            )
        self._audit("created", resolved, row)
        return row.view()

    def cancel(self, request_id: Any, *, scope: ScopeContext | None) -> dict[str, Any]:
        resolved = self._account_admin(scope)
        store = self._get_store()
        row = store.get(_text(request_id))
        # Another account's request is answered exactly like a missing one.
        if row is not None and row.account_id != _text(resolved.account_id):
            row = None
        self._pending(row)
        cancelled = store.decide(
            _text(request_id), status=CANCELLED, decided_by=_uuid_or_none(resolved.user_id)
        )
        if cancelled is None:
            raise self._answered()
        self._audit("cancelled", resolved, cancelled)
        return cancelled.view()

    def list_requests(self, *, scope: ScopeContext | None) -> dict[str, Any]:
        """Every pending request for the installation administrator; otherwise the
        caller's own account's requests, newest first."""
        if scope is not None and is_installation_admin(scope):
            rows = self._get_store().list_pending()
            return {"scope": "installation", "items": [row.view() for row in rows]}
        resolved = self._account_admin(scope)
        rows = self._get_store().list_for_account(_text(resolved.account_id))
        return {"scope": "account", "items": [row.view() for row in rows]}

    # --- the installation administrator -----------------------------------------------

    def approve(self, request_id: Any, *, scope: ScopeContext | None) -> dict[str, Any]:
        """Enable the installed extension for the asking account and approve.

        The extension must already be installed: the page installs it first through the
        catalog install call. Without an install the request stays pending.
        """
        resolved = self._installation_admin(scope)
        store = self._get_store()
        row = self._pending(store.get(_text(request_id)))
        install = self._installation_install(row.extension_id)
        if install is None:
            message = "The extension is not installed yet; install it first"
            raise ServiceConflictError(
                message, payload={"error": message, "code": NOT_INSTALLED_CODE}
            )
        self._get_enablement_service().enable_on_request(
            row.extension_id, account_id=row.account_id, scope=resolved
        )
        approved = store.decide(
            row.request_id, status=APPROVED, decided_by=_uuid_or_none(resolved.user_id)
        )
        if approved is None:
            raise self._answered()
        self._audit("approved", resolved, approved)
        return approved.view()

    def decline(
        self, request_id: Any, *, reason: Any = "", scope: ScopeContext | None
    ) -> dict[str, Any]:
        resolved = self._installation_admin(scope)
        normalized_reason = _bounded(reason, limit=MAX_REASON_CHARS, label="The reason")
        store = self._get_store()
        row = self._pending(store.get(_text(request_id)))
        declined = store.decide(
            row.request_id,
            status=DECLINED,
            decided_by=_uuid_or_none(resolved.user_id),
            decline_reason=normalized_reason,
        )
        if declined is None:
            raise self._answered()
        self._audit("declined", resolved, declined)
        return declined.view()


def default_install_request_store(get_logger: Callable[[], Any]) -> DBExtensionInstallRequestStore:
    """The database store on the logger's connection; refused without a database."""
    try:
        bl = get_logger()
    except Exception as exc:
        raise ServiceUnavailableError("Extension install requests are unavailable") from exc
    conn = getattr(bl, "conn", None) if bl is not None else None
    if conn is None:
        raise ServiceUnavailableError("Extension install requests are unavailable")
    return DBExtensionInstallRequestStore(conn)


def catalog_extension_name(extension_id: str) -> str | None:
    """The catalog's name of ``extension_id``; ``None`` when the catalog does not list it."""
    from core.application.extensions.extension_catalog import (
        ExtensionCatalogError,
        load_extension_catalog,
    )

    try:
        index = load_extension_catalog()
    except ExtensionCatalogError as exc:
        raise ServiceUnavailableError("The extension catalog cannot be reached now") from exc
    item = index.item(extension_id)
    return None if item is None else (_text(item.name) or extension_id)


def build_install_request_service(
    *,
    get_logger: Callable[[], Any],
    get_install_store: Callable[[], Any],
    catalog_name: Callable[[str], str | None] = catalog_extension_name,
    get_store: Callable[[], Any] | None = None,
) -> ExtensionInstallRequestService:
    """The service on the platform stores, as the API composes it."""
    from core.application.extensions.account_enablement import (
        ExtensionAccountEnablementService,
        default_enablement_store,
    )

    def _enablements() -> Any:
        return getattr(get_install_store(), "account_enablements", None) or (
            default_enablement_store(get_logger)
        )

    return ExtensionInstallRequestService(
        get_store=get_store or (lambda: default_install_request_store(get_logger)),
        get_install_store=get_install_store,
        get_enablement_service=lambda: ExtensionAccountEnablementService(
            get_install_store=get_install_store, get_enablement_store=_enablements
        ),
        catalog_name=catalog_name,
        get_audit_logger=get_logger,
    )


__all__ = [
    "APPROVED",
    "CANCELLED",
    "DECLINED",
    "MAX_NOTE_CHARS",
    "MAX_REASON_CHARS",
    "PENDING",
    "DBExtensionInstallRequestStore",
    "ExtensionInstallRequest",
    "ExtensionInstallRequestService",
    "InMemoryExtensionInstallRequestStore",
    "build_install_request_service",
    "can_request_extension_installs",
    "catalog_extension_name",
    "default_install_request_store",
    "record_install_request_audit",
]
