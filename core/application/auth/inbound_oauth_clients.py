"""Client ID Metadata Documents (CIMD) for inbound OAuth clients (ADR 0047 decision 5).

Public clients only: the ``client_id`` is an HTTPS URL on a fixed host allowlist and
the authorization server fetches the document itself. Nothing here touches the
database; the service owns the cache rows.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request

from core.application.auth.inbound_oauth_policy import (
    MAX_CLIENT_ID_LENGTH,
    valid_redirect_uri,
)
from core.infrastructure.config.version import APP_VERSION

#: Owner default (questions file rows 2, 13): exact hosts, no subdomains, fixed in code.
ALLOWED_CLIENT_ID_HOSTS: frozenset[str] = frozenset({"chatgpt.com", "claude.ai"})

FETCH_TIMEOUT_SECONDS = 5.0
MAX_DOCUMENT_BYTES = 64 * 1024
CACHE_MIN = timedelta(minutes=5)
CACHE_MAX = timedelta(hours=24)
CACHE_DEFAULT = timedelta(hours=1)
_MAX_AGE_RE = re.compile(r"(?:^|[,\s])max-age\s*=\s*\"?(\d+)\"?", re.IGNORECASE)
_PURPOSE = "OAuth client metadata"


class ClientNotAllowedError(ValueError):
    """``client_id`` is not an allowed CIMD URL (error page ``client_not_allowed``)."""

    code = "client_not_allowed"


class ClientMetadataUnavailableError(ValueError):
    """The document could not be fetched (error page ``client_metadata_unavailable``)."""

    code = "client_metadata_unavailable"


class InvalidClientMetadataError(ValueError):
    """The document violates the CIMD contract (error page ``invalid_client_metadata``)."""

    code = "invalid_client_metadata"


@dataclass(frozen=True, slots=True)
class ClientMetadata:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    metadata_sha256: str


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    body: bytes
    cache_control: str


MetadataFetcher = Callable[[str], FetchedDocument]

_CLIENT_ID_NOT_ALLOWED = "client_id is not an allowed client metadata URL"


def validate_client_id(
    client_id: str | None,
    *,
    allowed_hosts: frozenset[str] = ALLOWED_CLIENT_ID_HOSTS,
) -> str:
    """Return ``client_id`` when it is an allowed CIMD URL, else raise.

    ``https``, host exactly on the allowlist, non-empty path, no userinfo, query or
    fragment, at most 2048 characters. No fetch happens for a refused id.
    """
    raw = str(client_id or "")
    if not raw or raw != raw.strip() or len(raw) > MAX_CLIENT_ID_LENGTH:
        raise ClientNotAllowedError(_CLIENT_ID_NOT_ALLOWED)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ClientNotAllowedError(_CLIENT_ID_NOT_ALLOWED) from exc
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in raw
        or "#" in raw
        or port not in (None, 443)
        or host not in allowed_hosts
        or parsed.netloc.lower() not in {host, f"{host}:443"}
        or parsed.path in ("", "/")
    ):
        raise ClientNotAllowedError(_CLIENT_ID_NOT_ALLOWED)
    return raw


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def validate_client_metadata(body: bytes, *, client_id: str) -> ClientMetadata:
    """Validate a fetched CIMD document against ``client_id``."""
    try:
        document = json.loads(body.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError is a ValueError
        raise InvalidClientMetadataError("client metadata is not JSON") from exc
    if not isinstance(document, dict):
        raise InvalidClientMetadataError("client metadata is not a JSON object")
    if document.get("client_id") != client_id:
        raise InvalidClientMetadataError("client metadata client_id does not match")
    redirect_uris = _string_list(document.get("redirect_uris"))
    if not redirect_uris or not all(valid_redirect_uri(uri) for uri in redirect_uris):
        raise InvalidClientMetadataError("client metadata redirect_uris are invalid")
    auth_method = document.get("token_endpoint_auth_method")
    if auth_method is not None and auth_method != "none":
        raise InvalidClientMetadataError("only public clients are supported")
    if "grant_types" in document:
        grant_types = _string_list(document.get("grant_types"))
        if (
            not grant_types
            or "authorization_code" not in grant_types
            or not set(grant_types) <= {"authorization_code", "refresh_token"}
        ):
            raise InvalidClientMetadataError("client metadata grant_types are unsupported")
    if "response_types" in document:
        response_types = _string_list(document.get("response_types"))
        if not response_types or "code" not in response_types:
            raise InvalidClientMetadataError("client metadata response_types are unsupported")
    raw_name = document.get("client_name")
    client_name = raw_name.strip()[:200] if isinstance(raw_name, str) else ""
    return ClientMetadata(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=tuple(redirect_uris),
        metadata_sha256=hashlib.sha256(body).hexdigest(),
    )


def cache_lifetime(cache_control: str | None) -> timedelta:
    """``Cache-Control: max-age`` clamped to 5 minutes to 24 hours, default 1 hour."""
    match = _MAX_AGE_RE.search(str(cache_control or ""))
    if not match:
        return CACHE_DEFAULT
    seconds = timedelta(seconds=int(match.group(1)))
    return max(CACHE_MIN, min(CACHE_MAX, seconds))


def _public_peer_ips(host: str, port: int) -> tuple[str, ...]:
    """Resolve and keep public addresses only, whatever FS_ALLOW_PRIVATE_REMOTE_URLS says."""
    from core.application.extensions import url_safety

    try:
        resolved = url_safety._resolve_hostname_ips(host, port=port)
    except Exception as exc:
        raise ClientMetadataUnavailableError("client metadata host cannot be resolved") from exc
    addresses = tuple(str(item) for item in resolved or ())
    if not addresses or any(url_safety._is_blocked_ip(address) for address in addresses):
        raise ClientMetadataUnavailableError("client metadata host is not a public address")
    return addresses


def fetch_client_metadata(
    client_id: str,
    *,
    timeout_seconds: float = FETCH_TIMEOUT_SECONDS,
    max_bytes: int = MAX_DOCUMENT_BYTES,
    resolve_ips: Callable[[str, int], tuple[str, ...]] = _public_peer_ips,
    open_request: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FetchedDocument:
    """GET the document through a pinned public peer, no redirects, bounded body."""
    from core.application.extensions.url_safety import build_pinned_opener

    parsed = urlsplit(client_id)
    host = parsed.hostname or ""
    ips = resolve_ips(host, int(parsed.port or 443))
    request = Request(
        client_id,
        headers={
            "Accept": "application/json",
            # Public CIMD endpoints can reject the generic Python user agent.
            "User-Agent": f"FlowSteward/{APP_VERSION} (+https://flowsteward.org)",
        },
        method="GET",
    )
    started = clock()
    try:
        if open_request is None:
            opener = build_pinned_opener(ips=tuple(ips), purpose=_PURPOSE)
            response = opener.open(request, timeout=timeout_seconds)
        else:
            response = open_request(request, timeout=timeout_seconds, ips=tuple(ips))
        with response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            if status != 200:
                raise ClientMetadataUnavailableError("client metadata answered non-200")
            body = response.read(max_bytes + 1)
            headers = getattr(response, "headers", None)
            cache_control = str(headers.get("Cache-Control") or "") if headers else ""
    except ClientMetadataUnavailableError:
        raise
    except Exception as exc:
        # Redirects (the opener refuses them), TLS, timeouts and peer checks all land here.
        raise ClientMetadataUnavailableError("client metadata could not be fetched") from exc
    if len(body) > max_bytes:
        raise ClientMetadataUnavailableError("client metadata exceeds the size limit")
    if clock() - started > timeout_seconds:
        raise ClientMetadataUnavailableError("client metadata fetch exceeded the time limit")
    return FetchedDocument(body=body, cache_control=cache_control)


__all__ = [
    "ALLOWED_CLIENT_ID_HOSTS",
    "ClientMetadata",
    "ClientMetadataUnavailableError",
    "ClientNotAllowedError",
    "FetchedDocument",
    "InvalidClientMetadataError",
    "MetadataFetcher",
    "cache_lifetime",
    "fetch_client_metadata",
    "validate_client_id",
    "validate_client_metadata",
]
