"""Bounded outbound HTTP for the read-only static catalogs.

Every catalog request — index or artifact — goes through here, so the outbound
rules are stated once: HTTPS only, the repository's SSRF preflight plus
connected-peer pinning, a bounded timeout, a bounded redirect chain that is
re-validated at every hop, and a hard ceiling on the number of bytes read.

The SSRF guard itself is reached through :class:`CatalogUrlGuard` rather than
imported. The repository's canonical guard currently lives in
``core/application/extensions/url_safety``, and infrastructure must not import
the application layer; each business area binds its own guard instead. The port
names its members after the operations they perform, so the outbound call stays
visible to the outbound-HTTP inventory scan.

Nothing here knows what a catalog item is. Parsing and schema validation belong
to the two domain readers.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request

from core.infrastructure.static_catalog.settings import (
    CATALOG_HTTP_TIMEOUT_SECONDS,
    CATALOG_MAX_REDIRECTS,
)

_READ_CHUNK_BYTES = 64 * 1024
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_SHA256_HEX_LENGTH = 64


class CatalogFetchError(RuntimeError):
    """An index or artifact could not be retrieved within the declared bounds."""


class CatalogNotModifiedError(CatalogFetchError):
    """The host answered ``304 Not Modified`` for a conditional index request."""


class CatalogArtifactMismatchError(CatalogFetchError):
    """A fetched artifact did not match the byte size or digest the index declared."""


@dataclass(frozen=True, slots=True)
class CatalogUrlGuard:
    """The two SSRF operations this module needs, supplied by the caller.

    Field names match the functions they carry on purpose: the outbound-HTTP
    inventory scan keys on the called attribute, so ``guard.open_pinned_url(...)``
    stays a registered outbound caller rather than an invisible indirection.
    """

    assert_safe_remote_https_url: Callable[..., str]
    open_pinned_url: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class FetchedCatalogIndex:
    """A decoded index document plus the validators the next read may send back."""

    payload: dict[str, Any]
    etag: str
    last_modified: str = ""


def fetch_catalog_index_json(
    url: str,
    *,
    guard: CatalogUrlGuard,
    max_bytes: int,
    purpose: str,
    etag: str = "",
    last_modified: str = "",
    timeout_seconds: float = CATALOG_HTTP_TIMEOUT_SECONDS,
    max_redirects: int = CATALOG_MAX_REDIRECTS,
) -> FetchedCatalogIndex:
    """Fetch and JSON-decode a catalog index.

    ``etag`` is sent as ``If-None-Match`` and ``last_modified`` as
    ``If-Modified-Since`` when present. A host that answers ``304`` raises
    :class:`CatalogNotModifiedError`; a host that always answers ``200`` is
    equally correct and simply returns the document again.
    """
    # An index is text that compresses about ninefold, and the MCP catalog's is
    # megabytes; GitHub's raw host serves gzip when asked. Artifacts are never
    # requested compressed: their SHA-256 is of the bytes as published.
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip"}
    validator = str(etag or "").strip()
    if validator:
        headers["If-None-Match"] = validator
    modified_since = str(last_modified or "").strip()
    if modified_since:
        headers["If-Modified-Since"] = modified_since

    body, response_etag, response_last_modified = _fetch_bounded(
        url,
        guard=guard,
        headers=headers,
        max_bytes=max_bytes,
        purpose=purpose,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
    )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogFetchError(f"{purpose} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CatalogFetchError(f"{purpose} must be a JSON object")
    return FetchedCatalogIndex(
        payload=payload, etag=response_etag, last_modified=response_last_modified
    )


def fetch_catalog_artifact(
    url: str,
    *,
    guard: CatalogUrlGuard,
    expected_bytes: int,
    expected_sha256: str,
    max_bytes: int,
    purpose: str,
    timeout_seconds: float = CATALOG_HTTP_TIMEOUT_SECONDS,
    max_redirects: int = CATALOG_MAX_REDIRECTS,
) -> bytes:
    """Fetch one artifact and return it only when size and digest both match.

    The index's declared size is the read ceiling, so a host that streams more
    than it published is cut off rather than buffered. The digest is checked
    before the bytes are handed to any parser or installer.
    """
    declared_size = int(expected_bytes)
    if declared_size <= 0:
        raise CatalogArtifactMismatchError(f"{purpose} declares no byte size")
    if declared_size > int(max_bytes):
        raise CatalogArtifactMismatchError(
            f"{purpose} declares {declared_size} bytes, above the {int(max_bytes)} byte limit"
        )
    digest = str(expected_sha256 or "").strip().lower()
    if len(digest) != _SHA256_HEX_LENGTH or any(char not in "0123456789abcdef" for char in digest):
        raise CatalogArtifactMismatchError(f"{purpose} declares no usable SHA-256 digest")

    body, _etag, _last_modified = _fetch_bounded(
        url,
        guard=guard,
        headers={"Accept": "application/octet-stream"},
        max_bytes=declared_size,
        purpose=purpose,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
    )
    if len(body) != declared_size:
        raise CatalogArtifactMismatchError(
            f"{purpose} is {len(body)} bytes but the catalog declared {declared_size}"
        )
    actual = hashlib.sha256(body).hexdigest()
    if actual != digest:
        raise CatalogArtifactMismatchError(f"{purpose} SHA-256 does not match the catalog entry")
    return body


def _fetch_bounded(
    url: str,
    *,
    guard: CatalogUrlGuard,
    headers: dict[str, str],
    max_bytes: int,
    purpose: str,
    timeout_seconds: float,
    max_redirects: int,
) -> tuple[bytes, str, str]:
    """Return the body, its ``ETag`` and its ``Last-Modified`` (each '' when absent)."""
    current = _safe_https_url(guard, url, purpose=purpose)
    for _hop in range(int(max_redirects) + 1):
        request = Request(current, headers=dict(headers), method="GET")
        try:
            with guard.open_pinned_url(  # nosec B310 - scheme asserted https, peer pinned
                request,
                timeout_seconds=float(timeout_seconds),
                purpose=purpose,
            ) as response:
                body = _read_bounded(response, max_bytes=max_bytes, purpose=purpose)
                encoding = _header_value(response, "Content-Encoding").lower()
                if encoding == "gzip":
                    body = _gunzip_bounded(body, max_bytes=max_bytes, purpose=purpose)
                elif encoding not in ("", "identity"):
                    raise CatalogFetchError(
                        f"{purpose} arrived with unsupported encoding {encoding}"
                    )
                return (
                    body,
                    _header_value(response, "ETag"),
                    _header_value(response, "Last-Modified"),
                )
        except HTTPError as exc:
            try:
                current = _redirect_target(guard, exc, base_url=current, purpose=purpose)
            finally:
                exc.close()
        except (OSError, ValueError) as exc:
            raise CatalogFetchError(f"{purpose} could not be retrieved: {exc}") from exc
    raise CatalogFetchError(f"{purpose} exceeded {int(max_redirects)} redirects")


def _redirect_target(guard: CatalogUrlGuard, exc: HTTPError, *, base_url: str, purpose: str) -> str:
    """Return the next validated hop for a redirect, or raise for anything else.

    ``304`` and ordinary HTTP failures each keep their own error type so the
    caller can tell "unchanged" apart from "unreachable".
    """
    if int(exc.code) == 304:
        raise CatalogNotModifiedError(f"{purpose} is unchanged")
    if int(exc.code) not in _REDIRECT_CODES:
        raise CatalogFetchError(f"{purpose} request failed with HTTP {exc.code}")
    location = str((exc.headers or {}).get("Location") or "").strip()
    if not location:
        raise CatalogFetchError(f"{purpose} returned HTTP {exc.code} without a Location")
    # Every hop is re-validated: a first-hop redirect into a private address is
    # exactly the SSRF the preflight on the original URL cannot see.
    return _safe_https_url(guard, urljoin(base_url, location), purpose=purpose)


def _safe_https_url(guard: CatalogUrlGuard, url: str, *, purpose: str) -> str:
    try:
        return guard.assert_safe_remote_https_url(url, purpose=purpose)
    except Exception as exc:
        raise CatalogFetchError(f"{purpose} URL is not allowed: {exc}") from exc


def _read_bounded(response: Any, *, max_bytes: int, purpose: str) -> bytes:
    limit = int(max_bytes)
    declared = _header_value(response, "Content-Length")
    if declared.isdigit() and int(declared) > limit:
        raise CatalogFetchError(
            f"{purpose} declares {declared} bytes, above the {limit} byte limit"
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise CatalogFetchError(f"{purpose} exceeds the {limit} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _gunzip_bounded(body: bytes, *, max_bytes: int, purpose: str) -> bytes:
    """Decompress a gzip body, refusing one that would expand past ``max_bytes``.

    The compressed read was already bounded; this bounds the expansion, so a
    small hostile body cannot inflate into an unbounded one.
    """
    limit = int(max_bytes)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(body, limit + 1)
        if len(out) > limit or inflater.unconsumed_tail:
            raise CatalogFetchError(f"{purpose} expands past the {limit} byte limit")
        out += inflater.flush()
    except zlib.error as exc:
        raise CatalogFetchError(f"{purpose} is not valid gzip") from exc
    if len(out) > limit:
        raise CatalogFetchError(f"{purpose} expands past the {limit} byte limit")
    if not inflater.eof:
        raise CatalogFetchError(f"{purpose} gzip stream is truncated")
    return out


def _header_value(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    return str(getter(name) or "").strip()


__all__ = [
    "CatalogArtifactMismatchError",
    "CatalogFetchError",
    "CatalogNotModifiedError",
    "CatalogUrlGuard",
    "FetchedCatalogIndex",
    "fetch_catalog_artifact",
    "fetch_catalog_index_json",
]
