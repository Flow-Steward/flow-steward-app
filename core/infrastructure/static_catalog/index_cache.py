"""The process-local index cache every static catalog uses.

There is no freshness window. Every read asks the catalog host whether the
index changed: a conditional request that sends back the previous response's
``ETag`` (``If-None-Match``) and ``Last-Modified`` (``If-Modified-Since``). A
``304`` reuses the already parsed index; a ``200`` is validated whole and only
then replaces it. A newly approved extension, template or server therefore
shows on the next read instead of an hour later; an index is small and served
gzipped, and an unchanged one costs a ``304`` with no body.

What the cache keeps is a fallback, never a substitute for asking:

* when a read fails (network error, an HTTP error, a document that does not
  validate), the last good index keeps serving and the cache reports itself
  stale with the error, so a catalog does not vanish in an outage;
* after a failure, reads within ``CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS`` do
  not ask again, so a dead host is not dialled on every request;
* one read at a time goes to the host. A caller that arrives while a read is in
  flight waits for it and takes its result instead of sending its own.

Nothing is persisted: a restart simply reads again. The cache knows nothing
about what a catalog item is; each business area supplies the fetch (bound to
its SSRF guard) and the parser for its own index contract.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from core.infrastructure.static_catalog.http_fetch import (
    CatalogFetchError,
    CatalogNotModifiedError,
    FetchedCatalogIndex,
)
from core.infrastructure.static_catalog.settings import CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS

IndexT = TypeVar("IndexT")


class CatalogIndexUnavailableError(CatalogFetchError):
    """There is no index to serve: the read failed and nothing good is cached."""


@dataclass(frozen=True, slots=True)
class CatalogValidators:
    """What the previous ``200`` said about its bytes, sent back on the next read."""

    etag: str = ""
    last_modified: str = ""


#: Fetch one index from ``url``, sending ``validators`` as conditional headers.
IndexFetcher = Callable[[str, CatalogValidators], FetchedCatalogIndex]


class CatalogIndexCache(Generic[IndexT]):
    """One catalog's last good index, revalidated with the host on every read.

    ``fetch``, ``parse`` and ``clock`` are looked up on every call, so an area
    can hand in small functions that resolve its own module globals at call
    time and keep its existing test seams.
    """

    def __init__(
        self,
        *,
        fetch: IndexFetcher,
        parse: Callable[[dict[str, Any]], IndexT],
        clock: Callable[[], float] = time.time,
        failure_cooldown_seconds: float = CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._parse = parse
        self._clock = clock
        self._failure_cooldown_seconds = float(failure_cooldown_seconds)
        self._cond = threading.Condition()
        self._index: IndexT | None = None
        self._validators = CatalogValidators()
        self._source_url = ""
        self._fetched_at = 0.0
        self._last_error = ""
        self._last_failure_at = 0.0
        self._in_flight = False
        #: Bumped by every reset, so a read that started before it is discarded.
        self._epoch = 0
        #: Bumped whenever a read finishes, so a waiter knows its read is done.
        self._reads_finished = 0
        self._last_read_epoch = -1

    def reset(self) -> None:
        """Forget everything. Used by tests and when the configured URL changes."""
        with self._cond:
            self._forget()
            self._source_url = ""

    def load(self, url: str) -> IndexT:
        """Ask the host for the index at ``url`` and return the current one.

        Raises :class:`CatalogIndexUnavailableError` only when there is nothing
        to serve: the read failed (or failed moments ago) with no good index.
        """
        with self._cond:
            self._adopt_url(url)
            while self._in_flight:
                finished = self._reads_finished
                while self._in_flight and self._reads_finished == finished:
                    self._cond.wait()
                self._adopt_url(url)
                if self._last_read_epoch == self._epoch:
                    # The read we waited for answered for this URL: its outcome
                    # is ours too, success or failure.
                    return self._current_or_raise()
            if self._in_failure_cooldown():
                return self._current_or_raise()
            self._in_flight = True
            epoch = self._epoch
            validators = self._validators if self._index is not None else CatalogValidators()
            cached = self._index

        fresh: IndexT | None = None
        fetched: FetchedCatalogIndex | None = None
        error = ""
        try:
            fetched = self._fetch(url, validators)
            fresh = self._parse(fetched.payload)
        except CatalogNotModifiedError as exc:
            if cached is None:
                error = f"the host answered 304 with nothing cached: {exc}"
        except (CatalogFetchError, ValueError) as exc:
            error = str(exc) or type(exc).__name__
        except BaseException as exc:
            error = str(exc) or type(exc).__name__
            self._finish(epoch, fresh=None, fetched=None, error=error)
            raise
        self._finish(epoch, fresh=fresh, fetched=fetched, error=error)
        with self._cond:
            if epoch != self._epoch or self._source_url != url:
                raise CatalogIndexUnavailableError("the catalog source changed during this read")
            return self._current_or_raise()

    def current(self, url: str) -> IndexT | None:
        """The last good index for ``url`` without asking the host, or None."""
        with self._cond:
            if self._source_url != url:
                return None
            return self._index

    def freshness(self) -> dict[str, Any]:
        """``fetched_at``, ``age_seconds``, ``stale`` and ``last_refresh_error``.

        ``fetched_at`` is when the host last confirmed the index (a ``200`` or a
        ``304``); a failed read since then makes it ``stale``.
        """
        with self._cond:
            fetched_at = self._fetched_at
            last_error = self._last_error
        return {
            "fetched_at": _iso_from_epoch(fetched_at),
            "age_seconds": max(0, int(self._clock() - fetched_at)) if fetched_at else 0,
            "stale": bool(last_error),
            "last_refresh_error": last_error,
        }

    def _finish(
        self,
        epoch: int,
        *,
        fresh: IndexT | None,
        fetched: FetchedCatalogIndex | None,
        error: str,
    ) -> None:
        with self._cond:
            try:
                if epoch != self._epoch:
                    return
                now = self._clock()
                if error:
                    self._last_error = error
                    self._last_failure_at = now
                    return
                if fresh is not None and fetched is not None:
                    self._index = fresh
                    self._validators = CatalogValidators(
                        etag=fetched.etag, last_modified=fetched.last_modified
                    )
                self._fetched_at = now
                self._last_error = ""
                self._last_failure_at = 0.0
            finally:
                if epoch == self._epoch:
                    self._last_read_epoch = epoch
                self._in_flight = False
                self._reads_finished += 1
                self._cond.notify_all()

    def _adopt_url(self, url: str) -> None:
        """Drop what another URL's host published. Caller holds the lock."""
        if self._source_url and self._source_url != url:
            # Serving the old index would attribute one publisher's entries to
            # another.
            self._forget()
        self._source_url = url

    def _forget(self) -> None:
        self._index = None
        self._validators = CatalogValidators()
        self._fetched_at = 0.0
        self._last_error = ""
        self._last_failure_at = 0.0
        self._epoch += 1

    def _in_failure_cooldown(self) -> bool:
        if not self._last_failure_at:
            return False
        return (self._clock() - self._last_failure_at) < self._failure_cooldown_seconds

    def _current_or_raise(self) -> IndexT:
        if self._index is not None:
            return self._index
        raise CatalogIndexUnavailableError(self._last_error or "the catalog index is unavailable")


def _iso_from_epoch(value: float) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "CatalogIndexCache",
    "CatalogIndexUnavailableError",
    "CatalogValidators",
    "IndexFetcher",
]
