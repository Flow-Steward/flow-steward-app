from __future__ import annotations

import base64
import contextlib
import http.client
import ipaddress
import json
import math
import os
import socket
import ssl
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, Request, build_opener

_MESSAGE_REMOTE_URL = "remote URL"


class PinnedPeerError(ValueError):
    """Raised when a URL or connected peer fails the SDK SSRF checks."""


def parse_retry_after(value: Any, *, now: datetime | None = None) -> float | None:
    """Parse positive Retry-After delta-seconds or an RFC HTTP-date."""
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 0.0
    if math.isfinite(seconds) and seconds > 0:
        return seconds
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    remaining = (parsed - reference).total_seconds()
    return remaining if remaining > 0 else None


def _allow_private_remote_urls() -> bool:
    return str(os.environ.get("FS_ALLOW_PRIVATE_REMOTE_URLS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_blocked_ip(address: str) -> bool:
    return not ipaddress.ip_address(str(address or "").split("%", 1)[0].strip()).is_global


def _resolve_hostname_ips(hostname: str, *, port: int) -> set[str]:
    resolved: set[str] = set()
    for info in socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP):
        sockaddr = info[4] if len(info) > 4 else ()
        if sockaddr:
            resolved.add(str(sockaddr[0] or "").strip())
    return resolved


def _validate_remote_http_url(
    url: str, *, purpose: str, allow_private: bool = False
) -> tuple[str, str, int]:
    normalized = str(url or "").strip()
    parsed = urlparse(normalized)
    scheme = str(parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{purpose} must use http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{purpose} must not include URL credentials")
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError(f"{purpose} must include host")
    if not (allow_private or _allow_private_remote_urls()) and (
        host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local"))
    ):
        raise ValueError(f"{purpose} host '{host}' is not allowed")
    port = int(parsed.port or (443 if scheme == "https" else 80))
    return normalized, host, port


def assert_safe_remote_http_url(url: str, *, purpose: str = _MESSAGE_REMOTE_URL) -> str:
    normalized, host, port = _validate_remote_http_url(url, purpose=purpose)
    if _allow_private_remote_urls():
        return normalized
    for ip in resolve_pinned_ips(host, port=port, purpose=purpose):
        if _is_blocked_ip(ip):
            raise ValueError(f"{purpose} host '{host}' resolves to disallowed address '{ip}'")
    return normalized


def assert_safe_remote_https_url(url: str, *, purpose: str = _MESSAGE_REMOTE_URL) -> str:
    """Apply canonical SSRF checks and require a TLS endpoint."""
    normalized = assert_safe_remote_http_url(url, purpose=purpose)
    if str(urlparse(normalized).scheme or "").lower() != "https":
        raise ValueError(f"{purpose} must use https URL")
    return normalized


def resolve_pinned_ips(
    host: str,
    *,
    port: int,
    purpose: str = _MESSAGE_REMOTE_URL,
    allow_private: bool = False,
) -> list[str]:
    normalized_host = str(host or "").strip().lower().rstrip(".")
    if not normalized_host:
        raise PinnedPeerError(f"{purpose} must include host")
    bare = normalized_host.split("%", 1)[0]
    try:
        ipaddress.ip_address(bare)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False
    if is_literal_ip:
        if not (allow_private or _allow_private_remote_urls()) and _is_blocked_ip(bare):
            raise PinnedPeerError(f"{purpose} host '{normalized_host}' is not allowed")
        return [bare]
    try:
        resolved = _resolve_hostname_ips(normalized_host, port=port)
    except Exception as exc:
        raise PinnedPeerError(
            f"{purpose} host '{normalized_host}' cannot be resolved: {exc}"
        ) from exc
    if not resolved:
        raise PinnedPeerError(f"{purpose} host '{normalized_host}' cannot be resolved")
    if not (allow_private or _allow_private_remote_urls()):
        for ip in resolved:
            if _is_blocked_ip(ip):
                raise PinnedPeerError(
                    f"{purpose} host '{normalized_host}' resolves to disallowed address '{ip}'"
                )
    return sorted(resolved)


def _assert_peer_not_blocked(
    sock: socket.socket, *, purpose: str, allow_private: bool = False
) -> None:
    if allow_private or _allow_private_remote_urls():
        return
    peer = sock.getpeername()
    peer_host = str((peer or ("",))[0] or "").strip()
    if peer_host and _is_blocked_ip(peer_host):
        raise PinnedPeerError(f"{purpose}: connection resolved to disallowed address '{peer_host}'")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    _pinned_ips: tuple[str, ...] = ()
    _pin_purpose = _MESSAGE_REMOTE_URL
    _allow_private = False

    def connect(self) -> None:
        last_exc: Exception | None = None
        for ip in self._pinned_ips or (self.host,):
            try:
                self.sock = socket.create_connection(
                    (ip, self.port),
                    self.timeout,
                    self.source_address,  # type: ignore[attr-defined]
                )
                _assert_peer_not_blocked(
                    self.sock,
                    purpose=self._pin_purpose,
                    allow_private=self._allow_private,
                )
                return
            except Exception as exc:
                if self.sock is not None:
                    with contextlib.suppress(Exception):
                        self.sock.close()
                    self.sock = None
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise PinnedPeerError(f"{self._pin_purpose}: no pinned address available")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    _pinned_ips: tuple[str, ...] = ()
    _pin_purpose = _MESSAGE_REMOTE_URL
    _allow_private = False

    def connect(self) -> None:
        context = self._context or ssl.create_default_context()  # type: ignore[attr-defined]
        last_exc: Exception | None = None
        for ip in self._pinned_ips or (self.host,):
            raw_sock: socket.socket | None = None
            try:
                raw_sock = socket.create_connection(
                    (ip, self.port),
                    self.timeout,
                    self.source_address,  # type: ignore[attr-defined]
                )
                _assert_peer_not_blocked(
                    raw_sock,
                    purpose=self._pin_purpose,
                    allow_private=self._allow_private,
                )
                self.sock = context.wrap_socket(raw_sock, server_hostname=self.host)
                return
            except Exception as exc:
                if self.sock is not None:
                    with contextlib.suppress(Exception):
                        self.sock.close()
                    self.sock = None
                elif raw_sock is not None:
                    with contextlib.suppress(Exception):
                        raw_sock.close()
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise PinnedPeerError(f"{self._pin_purpose}: no pinned address available")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _ = (req, fp, code, msg, headers, newurl)


def _build_pinned_opener(
    *,
    ips: tuple[str, ...],
    purpose: str = _MESSAGE_REMOTE_URL,
    follow_redirects: bool = False,
    allow_private: bool = False,
):
    class _HTTP(_PinnedHTTPConnection):
        _pinned_ips = tuple(ips)
        _pin_purpose = purpose
        _allow_private = allow_private

    class _HTTPS(_PinnedHTTPSConnection):
        _pinned_ips = tuple(ips)
        _pin_purpose = purpose
        _allow_private = allow_private

    class _HTTPHandler(HTTPHandler):
        def http_open(self, req):
            return self.do_open(_HTTP, req)

    class _HTTPSHandler(HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_HTTPS, req)

    handlers: list[Any] = [_HTTPHandler, _HTTPSHandler]
    if not follow_redirects:
        handlers.insert(0, _NoRedirectHandler)
    return build_opener(*handlers)


def build_pinned_opener(
    *,
    ips: tuple[str, ...],
    purpose: str = _MESSAGE_REMOTE_URL,
    follow_redirects: bool = False,
):
    return _build_pinned_opener(
        ips=ips,
        purpose=purpose,
        follow_redirects=follow_redirects,
        allow_private=False,
    )


def _open_pinned_url(
    request: Request | str,
    *,
    timeout_seconds: float,
    purpose: str = _MESSAGE_REMOTE_URL,
    pinned_ips: tuple[str, ...] | None = None,
    allow_private: bool = False,
):
    req = Request(request) if isinstance(request, str) else request
    _, host, port = _validate_remote_http_url(
        req.full_url,
        purpose=purpose,
        allow_private=allow_private,
    )
    if pinned_ips is None:
        ips = tuple(
            resolve_pinned_ips(
                host,
                port=port,
                purpose=purpose,
                allow_private=allow_private,
            )
        )
    else:
        ips = tuple(str(ip or "").strip() for ip in pinned_ips)
        if not ips:
            raise PinnedPeerError(f"{purpose}: no pinned address available")
        for ip in ips:
            try:
                if not (allow_private or _allow_private_remote_urls()) and _is_blocked_ip(ip):
                    raise PinnedPeerError(f"{purpose}: pinned address '{ip}' is not allowed")
            except ValueError as exc:
                raise PinnedPeerError(f"{purpose}: invalid pinned address '{ip}'") from exc
    return _build_pinned_opener(
        ips=ips,
        purpose=purpose,
        allow_private=allow_private,
    ).open(req, timeout=timeout_seconds)


def open_pinned_url(
    request: Request | str,
    *,
    timeout_seconds: float,
    purpose: str = _MESSAGE_REMOTE_URL,
    pinned_ips: tuple[str, ...] | None = None,
):
    return _open_pinned_url(
        request,
        timeout_seconds=timeout_seconds,
        purpose=purpose,
        pinned_ips=pinned_ips,
        allow_private=False,
    )


def platform_grant_signing_payload(descriptor: dict[str, Any], access: dict[str, Any]) -> bytes:
    """Return the stable byte payload used by platform-grant signing tests."""

    upload_url = str(access.get("upload_url") or "").strip()
    method = "PUT" if upload_url else "GET"
    granted_url = upload_url or str(access.get("download_url") or "").strip()
    claims = {
        "version": 1,
        "artifact_id": str(descriptor.get("artifact_id") or "").strip(),
        "artifact_kind": str(descriptor.get("artifact_kind") or "artifact").strip(),
        "binding_key": str(descriptor.get("binding_key") or "").strip(),
        "content_type": str(descriptor.get("content_type") or "").strip(),
        "scope": dict(descriptor.get("scope") or {}),
        "method": method,
        "upload_url": granted_url,
        "max_size_bytes": int(access.get("max_size_bytes") or 0),
        "required_headers": dict(access.get("required_headers") or {}),
        "expires_at": str(access.get("expires_at") or "").strip(),
        "single_use": bool(access.get("single_use")),
    }
    return json.dumps(
        claims,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _decode_b64url(value: str) -> bytes:
    normalized = str(value or "").strip()
    return base64.urlsafe_b64decode(normalized + "=" * (-len(normalized) % 4))


def verified_platform_grant_signature(descriptor: dict[str, Any], access: dict[str, Any]) -> str:
    grant = access.get("platform_grant")
    grant = grant if isinstance(grant, dict) else {}
    signature = str(grant.get("signature") or "").strip()
    verification_key = str(os.environ.get("FS_ARTIFACT_GRANT_VERIFY_KEY") or "").strip()
    expires_at = str(access.get("expires_at") or "").strip()
    if (
        int(grant.get("version") or 0) != 1
        or not signature
        or not verification_key
        or not expires_at
    ):
        return ""
    upload_url = str(access.get("upload_url") or "").strip()
    if access.get("single_use") is not bool(upload_url):
        return ""
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry <= datetime.now(UTC):
            return ""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(_decode_b64url(verification_key)).verify(
            _decode_b64url(signature),
            platform_grant_signing_payload(descriptor, access),
        )
    except Exception:
        return ""
    return signature


def open_platform_artifact_url(
    request: Request,
    *,
    descriptor: dict[str, Any],
    access: dict[str, Any],
    timeout_seconds: float,
):
    if not verified_platform_grant_signature(descriptor, access):
        raise PinnedPeerError("artifact URL does not have a valid platform grant")
    upload_url = str(access.get("upload_url") or "").strip()
    expected_method = "PUT" if upload_url else "GET"
    granted_url = upload_url or str(access.get("download_url") or "").strip()
    if request.full_url != granted_url:
        raise PinnedPeerError("artifact URL does not match its platform grant")
    if request.get_method().upper() != expected_method:
        raise PinnedPeerError("artifact URL method does not match its platform grant")
    required_headers = access.get("required_headers")
    if isinstance(required_headers, dict):
        actual_headers = {
            str(name).strip().lower(): str(value) for name, value in request.header_items()
        }
        for name, value in required_headers.items():
            if actual_headers.get(str(name).strip().lower()) != str(value):
                raise PinnedPeerError("artifact URL headers do not match its platform grant")
    return _open_pinned_url(
        request,
        timeout_seconds=timeout_seconds,
        purpose="artifact URL",
        allow_private=True,
    )


__all__ = [
    "PinnedPeerError",
    "Request",
    "assert_safe_remote_http_url",
    "assert_safe_remote_https_url",
    "build_pinned_opener",
    "open_pinned_url",
    "open_platform_artifact_url",
    "resolve_pinned_ips",
]
