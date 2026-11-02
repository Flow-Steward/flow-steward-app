"""Provider-independent read of one stored secret value.

Consumers hold an opaque, account-qualified local reference and ask a
`StoredSecretReader` for its value at an authorized server-side boundary. How
the value is kept — encrypted in this database today, a remote secret manager
later — belongs to the reader. Authorization does not: the caller has already
decided that this account may use this reference before it asks.

`stored_secret_reader` is the one place the host assembles the reader. Step
parameters, workflow content and prompts never select it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretReadContext:
    """What one read is for: the trusted owning account and its time budget.

    One context covers one workflow attempt, one standalone operation call or
    one notification delivery attempt. `deadline` is a `time.monotonic()` value.
    """

    account_id: str
    deadline: float | None = None

    @classmethod
    def for_account(
        cls, account_id: Any, *, timeout_seconds: float | None = None
    ) -> SecretReadContext:
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        return cls(account_id=str(account_id or "").strip(), deadline=deadline)

    def remaining_seconds(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())


class StoredSecretError(RuntimeError):
    """A stored value could not be produced. The message never names the value or its store.

    `code`, `retryable` and `failure_class` are the closed facts consumers hand
    to `retry_policy`; the reference key stays on the exception for audit and is
    not part of the message.
    """

    code: ClassVar[str] = "secret_value_unavailable"
    retryable: ClassVar[bool] = False
    failure_class: ClassVar[str] = "validation"
    safe_message: ClassVar[str] = "A stored secret could not be read."

    def __init__(self, ref_key: str = "") -> None:
        super().__init__(self.safe_message)
        self.ref_key = str(ref_key or "")


class StoredSecretMissing(StoredSecretError):
    """The reference points at nothing. Retrying cannot create a value."""

    code = "secret_value_missing"
    safe_message = "No stored value exists for a configured secret."


class StoredSecretUndecryptable(StoredSecretError):
    """A value exists but cannot be turned back into plaintext (wrong key, damaged record)."""

    code = "secret_value_undecryptable"
    failure_class = "platform_blocker"
    safe_message = "A stored secret value cannot be decrypted."


class StoredSecretUnavailable(StoredSecretError):
    """The store did not answer in time. Nothing was sent, so the operation may be retried."""

    code = "secret_store_unavailable"
    retryable = True
    failure_class = "transient"
    safe_message = "Secret storage is temporarily unavailable."


class StoredSecretReader(Protocol):
    """Return the plaintext behind one authorized reference, or raise `StoredSecretError`."""

    def read(self, context: SecretReadContext, ref_key: str) -> str: ...


class LocalStoredSecretReader:
    """Values encrypted with the installation key in `secret_refs`."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def read(self, context: SecretReadContext, ref_key: str) -> str:
        import psycopg

        from core.infrastructure.db.model_provider_store import get_secret_ref_ciphertext
        from core.infrastructure.security.secret_codec import decrypt_secret

        key = str(ref_key or "").strip()
        if not key:
            raise StoredSecretMissing(key)
        if context.remaining_seconds() == 0.0 or self._conn is None:
            raise StoredSecretUnavailable(key)
        try:
            ciphertext = get_secret_ref_ciphertext(
                self._conn, account_id=context.account_id, ref_key=key
            )
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            # The driver's text can carry the statement and connection details;
            # only the type is useful to an operator reading the log.
            _logger.warning("stored value read failed: %s", type(exc).__name__)
            raise StoredSecretUnavailable(key) from None
        if not ciphertext:
            raise StoredSecretMissing(key)
        try:
            return decrypt_secret(ciphertext)
        except Exception as exc:
            _logger.warning("stored value decryption failed: %s", type(exc).__name__)
            raise StoredSecretUndecryptable(key) from None


StoredSecretReaderFactory = Callable[[Any], StoredSecretReader]


def stored_secret_reader(conn: Any) -> StoredSecretReader:
    """The host-selected reader for values persisted through `conn`'s database."""
    return LocalStoredSecretReader(conn)


class _DeferredStoredSecretReader:
    def __init__(self, conn_provider: Callable[[], Any]) -> None:
        self._conn_provider = conn_provider
        self._reader: StoredSecretReader | None = None

    def read(self, context: SecretReadContext, ref_key: str) -> str:
        if self._reader is None:
            self._reader = stored_secret_reader(self._conn_provider())
        return self._reader.read(context, ref_key)


def deferred_stored_secret_reader(conn_provider: Callable[[], Any]) -> StoredSecretReader:
    """The host-selected reader, with its connection looked up on the first read.

    A step whose payload carries no authorized reference never touches the store.
    """
    return _DeferredStoredSecretReader(conn_provider)


__all__ = [
    "LocalStoredSecretReader",
    "SecretReadContext",
    "StoredSecretError",
    "StoredSecretMissing",
    "StoredSecretReader",
    "StoredSecretReaderFactory",
    "StoredSecretUnavailable",
    "StoredSecretUndecryptable",
    "deferred_stored_secret_reader",
    "stored_secret_reader",
]
