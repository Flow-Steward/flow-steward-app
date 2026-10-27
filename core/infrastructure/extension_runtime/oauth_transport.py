"""Bounded, provider-neutral OAuth token endpoint transport."""

from __future__ import annotations

import base64
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote_plus, urlencode, urlparse
from urllib.request import Request

from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import http as url_safety


class OAuthEndpointPermissionLike(Protocol):
    url: str
    client_auth_method: str


_MAX_RESPONSE_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 8 * 1024
_MAX_RETRY_AFTER_SECONDS = 300
_MAX_TOKEN_BYTES = 8192
_MAX_TOKEN_TYPE_BYTES = 64
_MAX_SCOPE_BYTES = 4096
_MAX_EXPIRY_SECONDS = 315_360_000
_MAX_ENDPOINT_BYTES = 2048
_MAX_INPUT_BYTES = 16 * 1024


class OAuthErrorCode(StrEnum):
    AUTHORIZATION_DENIED = "authorization_denied"
    INVALID_CLIENT = "invalid_client"
    INVALID_GRANT = "invalid_grant"
    AUTHORIZATION_REQUIRED = "authorization_required"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    INTERNAL_FAILURE = "internal_failure"


class OAuthProviderOperation(StrEnum):
    EXCHANGE = "exchange"
    REFRESH = "refresh"
    REVOKE = "revoke"


class OAuthProviderError(Exception):
    """Closed transport failure that never retains provider or credential data."""

    def __init__(
        self,
        code: OAuthErrorCode,
        operation: OAuthProviderOperation,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = OAuthErrorCode(code)
        self.operation = OAuthProviderOperation(operation)
        self.retry_after_seconds = (
            max(0, min(int(retry_after_seconds), _MAX_RETRY_AFTER_SECONDS))
            if retry_after_seconds is not None
            else None
        )
        super().__init__(f"OAuth provider {self.operation.value} failed: {self.code.value}")

    def __repr__(self) -> str:
        retry = (
            f", retry_after_seconds={self.retry_after_seconds}"
            if self.retry_after_seconds is not None
            else ""
        )
        return (
            f"OAuthProviderError(code={self.code.value!r}, "
            f"operation={self.operation.value!r}{retry})"
        )


@dataclass(frozen=True, slots=True)
class OAuthTimeouts:
    connect_seconds: float = 5.0
    read_seconds: float = 5.0
    overall_seconds: float = 15.0

    def __post_init__(self) -> None:
        values = (self.connect_seconds, self.read_seconds, self.overall_seconds)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("OAuth timeouts must be finite positive values")
        if self.connect_seconds > 30 or self.read_seconds > 30 or self.overall_seconds > 60:
            raise ValueError("OAuth timeouts exceed safe bounds")
        if self.overall_seconds < max(self.connect_seconds, self.read_seconds):
            raise ValueError("OAuth overall timeout must cover connect and read timeouts")


_DEFAULT_TIMEOUTS = OAuthTimeouts()


def _default_opener(
    request: Request,
    *,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    overall_deadline: float,
    purpose: str,
):
    _ = (read_timeout_seconds, overall_deadline)
    return url_safety.open_pinned_url(
        request,
        timeout_seconds=connect_timeout_seconds,
        purpose=purpose,
    )


def _set_read_timeout(response: Any, timeout_seconds: float) -> None:
    # urllib wraps sockets differently for successful HTTPResponse objects and
    # raised HTTPError objects. Walk only the known stdlib wrapper edges, with a
    # strict depth bound and identity set so malformed test/provider wrappers
    # cannot create an unbounded traversal.
    pending: list[tuple[Any, int]] = [(response, 0)]
    seen: set[int] = set()
    while pending:
        candidate, depth = pending.pop(0)
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            setter(timeout_seconds)
            return
        if depth >= 4:
            continue
        for attribute in ("fp", "raw", "_sock"):
            try:
                child = getattr(candidate, attribute, None)
            except Exception:
                child = None
            if child is not None:
                pending.append((child, depth + 1))


def _close_response(response: Any) -> None:
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            return


def _prepare_response_timeout(
    response: Any,
    *,
    read_timeout_seconds: float,
    overall_deadline: float,
) -> bool:
    remaining = overall_deadline - time.monotonic()
    if remaining <= 0:
        _close_response(response)
        return False
    _set_read_timeout(response, min(read_timeout_seconds, remaining))
    return True


def _header(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            value = getter(name.lower())
        if value is not None:
            return str(value).strip()
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value).strip()
    return ""


def _status(response: Any) -> int:
    value = getattr(response, "status", None)
    if value is None:
        value = getattr(response, "code", None)
    if value is None:
        getter = getattr(response, "getcode", None)
        value = getter() if callable(getter) else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _validate_endpoint(
    endpoint: OAuthEndpointPermissionLike, *, operation: OAuthProviderOperation
) -> str:
    url = str(endpoint.url or "").strip()
    try:
        parsed = urlparse(url)
        invalid = (
            not url
            or len(url) > _MAX_ENDPOINT_BYTES
            or parsed.scheme != "https"
            or not parsed.netloc
            or bool(parsed.username or parsed.password)
            or bool(parsed.query or parsed.fragment)
            or "{" in url
            or "}" in url
        )
    except (TypeError, ValueError):
        invalid = True
    if invalid:
        raise OAuthProviderError(OAuthErrorCode.PROVIDER_UNAVAILABLE, operation)
    try:
        return url_safety.assert_safe_remote_https_url(
            url,
            purpose=f"extension OAuth {operation.value} endpoint",
        )
    except Exception:
        raise OAuthProviderError(OAuthErrorCode.PROVIDER_UNAVAILABLE, operation) from None


def _validate_input(value: str, *, operation: OAuthProviderOperation) -> str:
    normalized = value if isinstance(value, str) else ""
    encoded_length: int | None = None
    if normalized:
        try:
            encoded_length = len(normalized.encode("utf-8"))
        except UnicodeError:
            encoded_length = None
    if not normalized or encoded_length is None or encoded_length > _MAX_INPUT_BYTES:
        raise OAuthProviderError(OAuthErrorCode.INTERNAL_FAILURE, operation)
    return normalized


def _client_auth(
    endpoint: OAuthEndpointPermissionLike,
    *,
    operation: OAuthProviderOperation,
    client_id: str,
    client_secret: str,
    form: dict[str, str],
) -> dict[str, str]:
    method = str(endpoint.client_auth_method or "").strip().lower()
    normalized_client_id = _validate_input(client_id, operation=operation)
    normalized_secret = str(client_secret or "")
    headers: dict[str, str] = {}
    if method == "client_secret_basic":
        if not normalized_secret:
            raise OAuthProviderError(OAuthErrorCode.INTERNAL_FAILURE, operation)
        encoded_pair = (
            f"{quote_plus(normalized_client_id, safe='~')}:"
            f"{quote_plus(_validate_input(normalized_secret, operation=operation), safe='~')}"
        )
        headers["Authorization"] = "Basic " + base64.b64encode(encoded_pair.encode("ascii")).decode(
            "ascii"
        )
    elif method == "client_secret_post":
        if not normalized_secret:
            raise OAuthProviderError(OAuthErrorCode.INTERNAL_FAILURE, operation)
        form["client_id"] = normalized_client_id
        form["client_secret"] = _validate_input(normalized_secret, operation=operation)
    elif method == "none":
        if normalized_secret:
            raise OAuthProviderError(OAuthErrorCode.INTERNAL_FAILURE, operation)
        form["client_id"] = normalized_client_id
    else:
        raise OAuthProviderError(OAuthErrorCode.INTERNAL_FAILURE, operation)
    return headers


def _read_capped(response: Any, *, overall_deadline: float) -> bytes:
    content_length = _header(getattr(response, "headers", None), "content-length")
    if content_length.isdigit() and int(content_length) > _MAX_RESPONSE_BYTES:
        raise OverflowError
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() >= overall_deadline:
            raise TimeoutError
        chunk = response.read(min(_READ_CHUNK_BYTES, _MAX_RESPONSE_BYTES + 1 - total))
        if time.monotonic() >= overall_deadline:
            raise TimeoutError
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise OverflowError
        chunks.append(bytes(chunk))
    return b"".join(chunks)


class _DuplicateJSONKey(ValueError):
    pass


def _json_object(raw: bytes) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise _DuplicateJSONKey
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError

    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=pairs,
        parse_constant=reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError
    return value


def _validate_json_headers(response: Any) -> None:
    headers = getattr(response, "headers", None)
    encoding = _header(headers, "content-encoding").lower()
    if encoding not in {"", "identity"}:
        raise ValueError
    content_type = _header(headers, "content-type")
    media_type, _, parameters = content_type.partition(";")
    if media_type.strip().lower() != "application/json":
        raise ValueError
    if parameters:
        pieces = [piece.strip() for piece in parameters.split(";") if piece.strip()]
        for piece in pieces:
            key, separator, value = piece.partition("=")
            if not separator or key.strip().lower() != "charset":
                raise ValueError
            if value.strip().strip('"').lower() not in {"utf-8", "utf8"}:
                raise ValueError


def _parse_retry_after(value: str, *, now: datetime | None = None) -> int | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return min(int(normalized), _MAX_RETRY_AFTER_SECONDS)
    try:
        parsed = parsedate_to_datetime(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        delay = math.ceil((parsed - (now or datetime.now(UTC))).total_seconds())
    except Exception:
        return None
    return max(0, min(delay, _MAX_RETRY_AFTER_SECONDS))


def _closed_error_code(status: int, payload: dict[str, Any]) -> OAuthErrorCode:
    provider_code = str(payload.get("error") or "").strip().lower()
    mapping = {
        "access_denied": OAuthErrorCode.AUTHORIZATION_DENIED,
        "invalid_client": OAuthErrorCode.INVALID_CLIENT,
        "invalid_grant": OAuthErrorCode.INVALID_GRANT,
        "authorization_required": OAuthErrorCode.AUTHORIZATION_REQUIRED,
        "interaction_required": OAuthErrorCode.AUTHORIZATION_REQUIRED,
        "login_required": OAuthErrorCode.AUTHORIZATION_REQUIRED,
        "consent_required": OAuthErrorCode.AUTHORIZATION_REQUIRED,
    }
    if status == 429:
        return OAuthErrorCode.RATE_LIMITED
    if provider_code in mapping:
        return mapping[provider_code]
    # A 401 saying the request was invalid is the authorization server declining
    # the credential itself, not complaining about the request's shape. Retrying
    # it forever would never succeed, so it asks for reauthorization.
    if status == 401 and provider_code == "invalid_request":
        return OAuthErrorCode.AUTHORIZATION_REQUIRED
    # An explicit 400 or 403 is a settled refusal about this client or request.
    # Treating it as a transient outage would retry something that cannot change,
    # so it is reported as a client problem and the stored grant is left intact
    # for diagnosis.
    if status in {400, 403}:
        return OAuthErrorCode.INVALID_CLIENT
    return OAuthErrorCode.PROVIDER_UNAVAILABLE


def _bounded_token(value: Any, *, maximum: int, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError
    return value


def _token_payload(
    payload: dict[str, Any],
    *,
    implicit_token_type: str = "",
    required_fields: Sequence[str] = (),
) -> dict[str, Any]:
    """Normalize a token response, or raise for one that cannot be trusted.

    `token_type` is required unless the provider has declared what an omitted one
    means. A provider that never sends the field is answering to a documented
    protocol; a provider that usually sends it and this time did not is not, so
    the requirement stays on by default.
    """
    token_type = (
        _bounded_token(
            payload.get("token_type"),
            maximum=_MAX_TOKEN_TYPE_BYTES,
            required=not implicit_token_type,
        )
        or implicit_token_type
    )
    if token_type.lower() != "bearer":
        raise ValueError
    result: dict[str, Any] = {
        "access_token": _bounded_token(
            payload.get("access_token"), maximum=_MAX_TOKEN_BYTES, required=True
        ),
        "token_type": "Bearer",
    }
    refresh = _bounded_token(payload.get("refresh_token"), maximum=_MAX_TOKEN_BYTES)
    if refresh:
        result["refresh_token"] = refresh
    scope = _bounded_token(payload.get("scope"), maximum=_MAX_SCOPE_BYTES)
    if scope:
        result["scope"] = scope
    if "expires_in" in payload:
        expiry = payload["expires_in"]
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
            or not math.isfinite(float(expiry))
            or float(expiry) < 0
            or float(expiry) > _MAX_EXPIRY_SECONDS
            or not float(expiry).is_integer()
        ):
            raise ValueError
        result["expires_in"] = int(expiry)
    # A refresh token may carry its own lifetime. It is read whenever it is
    # present: knowing when reauthorization becomes unavoidable is worth more
    # than a guess, and guessing it is what a hardcoded ninety days would be.
    if "refresh_token_expires_in" in payload:
        refresh_expiry = payload["refresh_token_expires_in"]
        if (
            isinstance(refresh_expiry, bool)
            or not isinstance(refresh_expiry, (int, float))
            or not math.isfinite(float(refresh_expiry))
            or float(refresh_expiry) < 0
            or float(refresh_expiry) > _MAX_EXPIRY_SECONDS
            or not float(refresh_expiry).is_integer()
        ):
            raise ValueError
        result["refresh_token_expires_in"] = int(refresh_expiry)
    # A provider that documents a field as always present gets a malformed
    # response when it is missing, rather than a partial one the caller would
    # have to complete from somewhere else.
    if any(field not in result for field in required_fields):
        raise ValueError
    return result


def _perform(
    *,
    endpoint: OAuthEndpointPermissionLike,
    operation: OAuthProviderOperation,
    form: dict[str, str],
    client_id: str,
    client_secret: str,
    opener: Callable[..., Any],
    timeouts: OAuthTimeouts,
    success_statuses: set[int],
    token_response: bool,
    implicit_token_type: str = "",
    required_response_fields: Sequence[str] = (),
    token_endpoint_permission: OAuthEndpointPermissionLike | None = None,
) -> dict[str, Any] | None:
    # stdlib DNS resolution is synchronous and cannot be interrupted portably.
    # Start the absolute deadline first so time spent resolving still consumes
    # the budget and fails closed before any subsequent network/read work.
    started_at = time.monotonic()
    overall_deadline = started_at + timeouts.overall_seconds
    url = _validate_endpoint(endpoint, operation=operation)
    headers = _client_auth(
        token_endpoint_permission or endpoint,
        operation=operation,
        client_id=client_id,
        client_secret=client_secret,
        form=form,
    )
    headers.update(
        {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-store",
            "Content-Type": "application/x-www-form-urlencoded",
        }
    )
    request = Request(  # nosec B310 - destination is TLS-only and IP-pinned by the opener
        url,
        data=urlencode(form).encode("ascii"),
        headers=headers,
        method="POST",
    )
    remaining = overall_deadline - time.monotonic()
    if remaining <= 0:
        raise OAuthProviderError(OAuthErrorCode.PROVIDER_UNAVAILABLE, operation)
    try:
        response_context = opener(
            request,
            connect_timeout_seconds=min(timeouts.connect_seconds, remaining),
            read_timeout_seconds=min(timeouts.read_seconds, remaining),
            overall_deadline=overall_deadline,
            purpose=f"extension OAuth {operation.value}",
        )
    except OAuthProviderError:
        raise
    except HTTPError as exc:
        response_context = exc
    except Exception:
        raise OAuthProviderError(OAuthErrorCode.PROVIDER_UNAVAILABLE, operation) from None

    try:
        response_ready = _prepare_response_timeout(
            response_context,
            read_timeout_seconds=timeouts.read_seconds,
            overall_deadline=overall_deadline,
        )
    except Exception:
        _close_response(response_context)
        raise OAuthProviderError(OAuthErrorCode.PROVIDER_UNAVAILABLE, operation) from None
    if not response_ready:
        raise OAuthProviderError(OAuthErrorCode.PROVIDER_UNAVAILABLE, operation)

    try:
        with response_context as response:
            status = _status(response)
            headers_value = getattr(response, "headers", None)
            try:
                raw = _read_capped(response, overall_deadline=overall_deadline)
            except TimeoutError:
                raise OAuthProviderError(OAuthErrorCode.PROVIDER_UNAVAILABLE, operation) from None
            except (OverflowError, TypeError, UnicodeError):
                raise OAuthProviderError(OAuthErrorCode.MALFORMED_RESPONSE, operation) from None
            if status not in success_statuses:
                payload: dict[str, Any] = {}
                if raw:
                    try:
                        _validate_json_headers(response)
                        payload = _json_object(raw)
                    except Exception:
                        payload = {}
                raise OAuthProviderError(
                    _closed_error_code(status, payload),
                    operation,
                    _parse_retry_after(_header(headers_value, "retry-after")),
                )
            if not token_response:
                if not raw:
                    return None
                try:
                    _validate_json_headers(response)
                    _json_object(raw)
                except Exception:
                    raise OAuthProviderError(OAuthErrorCode.MALFORMED_RESPONSE, operation) from None
                return None
            try:
                _validate_json_headers(response)
                return _token_payload(
                    _json_object(raw),
                    implicit_token_type=implicit_token_type,
                    required_fields=required_response_fields,
                )
            except OAuthProviderError:
                raise
            except Exception:
                raise OAuthProviderError(OAuthErrorCode.MALFORMED_RESPONSE, operation) from None
    except OAuthProviderError:
        raise
    except Exception:
        raise OAuthProviderError(OAuthErrorCode.PROVIDER_UNAVAILABLE, operation) from None


def exchange_code(
    *,
    endpoint: OAuthEndpointPermissionLike,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_id: str,
    client_secret: str = "",
    include_grant_type: bool = True,
    include_redirect_uri: bool = True,
    static_parameters: Mapping[str, str] | None = None,
    implicit_token_type: str = "",
    required_response_fields: Sequence[str] = (),
    opener: Callable[..., Any] = _default_opener,
    timeouts: OAuthTimeouts = _DEFAULT_TIMEOUTS,
) -> dict[str, Any]:
    form: dict[str, str] = {
        "code": _validate_input(code, operation=OAuthProviderOperation.EXCHANGE)
    }
    if include_grant_type:
        form["grant_type"] = "authorization_code"
    if include_redirect_uri:
        form["redirect_uri"] = _validate_input(
            redirect_uri, operation=OAuthProviderOperation.EXCHANGE
        )
    if code_verifier:
        form["code_verifier"] = _validate_input(
            code_verifier, operation=OAuthProviderOperation.EXCHANGE
        )
    # Static parameters are validated at contract time and cannot name a field
    # the exchange already owns, so they are added last without overwriting one.
    for name, value in sorted((static_parameters or {}).items()):
        if name not in form:
            form[name] = _validate_input(value, operation=OAuthProviderOperation.EXCHANGE)
    payload = _perform(
        endpoint=endpoint,
        operation=OAuthProviderOperation.EXCHANGE,
        form=form,
        client_id=client_id,
        client_secret=client_secret,
        opener=opener,
        timeouts=timeouts,
        success_statuses={200},
        token_response=True,
        implicit_token_type=implicit_token_type,
        required_response_fields=required_response_fields,
    )
    return dict(payload or {})


def refresh_token(
    *,
    endpoint: OAuthEndpointPermissionLike,
    refresh_token: str,
    client_id: str,
    client_secret: str = "",
    implicit_token_type: str = "",
    required_response_fields: Sequence[str] = (),
    opener: Callable[..., Any] = _default_opener,
    timeouts: OAuthTimeouts = _DEFAULT_TIMEOUTS,
) -> dict[str, Any]:
    payload = _perform(
        endpoint=endpoint,
        operation=OAuthProviderOperation.REFRESH,
        form={
            "grant_type": "refresh_token",
            "refresh_token": _validate_input(
                refresh_token, operation=OAuthProviderOperation.REFRESH
            ),
        },
        client_id=client_id,
        client_secret=client_secret,
        opener=opener,
        timeouts=timeouts,
        success_statuses={200},
        token_response=True,
        implicit_token_type=implicit_token_type,
        required_response_fields=required_response_fields,
    )
    return dict(payload or {})


def revoke_token(
    *,
    endpoint: OAuthEndpointPermissionLike,
    token_endpoint_permission: OAuthEndpointPermissionLike,
    token: str,
    client_id: str,
    client_secret: str = "",
    opener: Callable[..., Any] = _default_opener,
    timeouts: OAuthTimeouts = _DEFAULT_TIMEOUTS,
) -> None:
    _perform(
        endpoint=endpoint,
        operation=OAuthProviderOperation.REVOKE,
        form={
            "token": _validate_input(token, operation=OAuthProviderOperation.REVOKE),
            "token_type_hint": "refresh_token",
        },
        client_id=client_id,
        client_secret=client_secret,
        opener=opener,
        timeouts=timeouts,
        success_statuses={200, 204},
        token_response=False,
        token_endpoint_permission=token_endpoint_permission,
    )


__all__ = [
    "OAuthErrorCode",
    "OAuthProviderError",
    "OAuthProviderOperation",
    "OAuthTimeouts",
    "exchange_code",
    "refresh_token",
    "revoke_token",
]
