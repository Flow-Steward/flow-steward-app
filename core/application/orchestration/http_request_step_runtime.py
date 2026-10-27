"""Runtime helpers for ``step_kind=http_request`` execution."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from core.application.orchestration.http_response_decoding import (
    RESPONSE_TEXT_EXCERPT_CHARS,
    _bounded_prefix,
    _collect_inline_body,
    _decode_excerpt,
    _decoded_response_chunks,
    _filename_from_content_disposition,
    _filename_from_url_path,
    _inline_payload_from_bytes,
    _media_type,
    _response_can_have_body,
    _ResponseTooLargeError,
    _safe_plain_filename,
    _trustworthy_content_length,
)
from core.application.orchestration.http_secret_boundary import (
    HttpSecretBoundary,
    HttpSecretExposureError,
    HttpSecretInputError,
    has_http_secret,
    render_http_value,
)
from core.application.orchestration.retry_policy import parse_retry_after
from core.application.orchestration.step_templating import (
    STATE_PREFIX,
    _is_source_reference,
    _parse_literal,
    _render_template_string,
    _render_value,
    _resolve_path,
    _split_default_filter,
)
from core.application.orchestration.workflow_state_selector_contract import (
    RUNTIME_ERROR_CODE,
    validate_step_state_selectors,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

__all__ = [
    "STATE_PREFIX",
    "_is_source_reference",
    "_parse_literal",
    "_render_template_string",
    "_render_value",
    "_request_body_template",
    "_resolve_path",
    "_split_default_filter",
    "execute_http_request_step",
]

INLINE_RESPONSE_MAX_BYTES = 5 * 1024 * 1024
ARTIFACT_RESPONSE_DEFAULT_BYTES = 100 * 1024 * 1024
ARTIFACT_RESPONSE_MAX_BYTES = 200 * 1024 * 1024
ERROR_RESPONSE_PREFIX_BYTES = 8 * 1024

_ARTIFACT_PLANE_SINGLETON: Any | None = None
_SAFE_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _normalize_http_headers(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {
            str(k).strip(): str(v).strip()
            for k, v in raw.items()
            if str(k).strip() and v is not None
        }
    return {}


def _normalize_query_params(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {
            str(k).strip(): str(v).strip()
            for k, v in raw.items()
            if str(k).strip() and v is not None
        }
    return {}


def _request_body_template(config: dict[str, Any]) -> Any:
    """Return the canonical request body template."""
    return config.get("body_template")


def _coerce_positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _normalize_response_mode(value: Any) -> str:
    mode = str(value or "inline").strip().lower()
    if mode in {"", "inline"}:
        return "inline"
    if mode == "artifact":
        return "artifact"
    raise ValueError("http_request response_mode must be inline or artifact")


def _response_size_limit(config: dict[str, Any], mode: str) -> int:
    raw = config.get("max_response_bytes")
    default = ARTIFACT_RESPONSE_DEFAULT_BYTES if mode == "artifact" else INLINE_RESPONSE_MAX_BYTES
    ceiling = ARTIFACT_RESPONSE_MAX_BYTES if mode == "artifact" else INLINE_RESPONSE_MAX_BYTES
    if raw in (None, ""):
        return default
    parsed = _coerce_positive_int(raw)
    if parsed <= 0:
        raise ValueError("http_request max_response_bytes must be a positive integer")
    if parsed > ceiling:
        raise ValueError("http_request max_response_bytes exceeds mode ceiling")
    return parsed


def _validate_http_request_url(url: str) -> str:
    from core.application.extensions.service_support import validate_remote_url

    return validate_remote_url(url, purpose="http_request step url")


class _SSRFGuardedHTTPTransport(httpx.HTTPTransport):
    """httpx transport that (a) re-validates the request URL string and (b) pins
    every connection to an IP re-validated at connect time, so the destination is
    checked *before* request bytes are sent rather than only after the response
    is received. This closes the DNS-rebinding / private-network first-hop window.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        from core.application.extensions.url_safety import pin_httpx_transport

        pin_httpx_transport(self, purpose="http_request step url")

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _validate_http_request_url(str(request.url))
        return super().handle_request(request)


def _reject_blocked_response_peer(response: httpx.Response) -> None:
    extensions = getattr(response, "extensions", {}) or {}
    stream = extensions.get("network_stream") if isinstance(extensions, dict) else None
    if stream is None or not hasattr(stream, "get_extra_info"):
        return
    peer = None
    for key in ("peername", "server_addr"):
        try:
            peer = stream.get_extra_info(key)
        except Exception:
            peer = None
        if peer:
            break
    if isinstance(peer, tuple) and peer:
        peer_host = str(peer[0] or "").strip()
    else:
        peer_host = str(peer or "").strip()
    if not peer_host:
        return
    from core.application.extensions.lifecycle import _is_blocked_ip

    if _is_blocked_ip(peer_host):
        parsed = urlparse(str(response.request.url))
        raise httpx.ConnectError(
            f"http_request step url host '{parsed.hostname or ''}' connected to disallowed address '{peer_host}'",
            request=response.request,
        )


def _effective_port(parsed: Any) -> int:
    if parsed.port:
        return int(parsed.port)
    return 443 if str(parsed.scheme).lower() == "https" else 80


def _origin_tuple(url: str) -> tuple[str, str, int]:
    parsed = urlparse(str(url or ""))
    host = str(parsed.hostname or "").strip()
    return (
        str(parsed.scheme or "").lower(),
        host.encode("idna").decode("ascii").lower(),
        _effective_port(parsed),
    )


def _template_in_authority_or_scheme(url_template: str) -> bool:
    text = str(url_template or "").strip()
    marker = "{{"
    scheme_idx = text.find("://")
    if scheme_idx < 0:
        return marker in text
    authority = text[scheme_idx + 3 :].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return marker in text[:scheme_idx] or marker in authority


def _validate_static_https_auth_origin(
    url_template: str, rendered_url: str
) -> tuple[str, str, int]:
    if _template_in_authority_or_scheme(url_template):
        raise ValueError("authenticated http_request url scheme and authority must be static")
    literal = urlparse(str(url_template or "").strip())
    if str(literal.scheme or "").lower() != "https":
        raise ValueError("authenticated http_request url must use https")
    if literal.username or literal.password:
        raise ValueError("authenticated http_request url must not contain userinfo")
    if not literal.hostname:
        raise ValueError("authenticated http_request url requires a host")
    origin = _origin_tuple(url_template)
    rendered = urlparse(str(rendered_url or "").strip())
    if rendered.username or rendered.password:
        raise ValueError("authenticated http_request rendered url must not contain userinfo")
    if _origin_tuple(rendered_url) != origin:
        raise ValueError(
            "authenticated http_request rendered url origin does not match configured origin"
        )
    return origin


def _redact_request_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in (headers or {}).items():
        header_name = str(key or "").strip()
        if not header_name:
            continue
        if header_name.lower() in {"authorization", "proxy-authorization", "x-api-key"}:
            redacted[header_name] = "[REDACTED]"
        else:
            redacted[header_name] = str(value or "")
    return redacted


def _get_shared_logger():
    try:
        from core.infrastructure.db.platform_stores import PlatformConnectionStore

        return PlatformConnectionStore.get_shared_instance()
    except Exception:
        return None


def _scope_id(value: Any) -> str:
    return str(value or "").strip()


def _resolve_project_id(state: dict[str, Any], *, bl: Any | None = None) -> str:
    direct_project_id = _scope_id(state.get("project_id"))
    if direct_project_id:
        return direct_project_id

    project_config = state.get("project_config")
    if isinstance(project_config, dict):
        from_config = _scope_id(project_config.get("project_id") or project_config.get("id"))
        if from_config:
            return from_config

    metadata = state.get("meta")
    if isinstance(metadata, dict):
        from_meta = _scope_id(metadata.get("project_id"))
        if from_meta:
            return from_meta

    job_id = _scope_id(state.get("job_id"))
    if not job_id or bl is None or not hasattr(bl, "get_job"):
        return ""
    try:
        job_row = bl.get_job(job_id) or {}
    except Exception:
        return ""
    if isinstance(job_row, dict):
        return _scope_id(job_row.get("project_id"))
    return ""


def _resolve_secret_ref_value(ref: str, state: dict[str, Any]) -> str:
    normalized_ref = str(ref or "").strip()
    if not normalized_ref:
        return ""

    if normalized_ref.startswith("env:"):
        raise ValueError("env: auth references are not allowed for http_request steps")

    secret_key = (
        normalized_ref[len("secret:") :].strip()
        if normalized_ref.startswith("secret:")
        else normalized_ref
    )
    bl = _get_shared_logger()
    project_id = _resolve_project_id(state, bl=bl)
    if bl is not None and project_id and hasattr(bl, "get_project_secret"):
        try:
            value = str(
                bl.get_project_secret(
                    project_id=project_id,
                    secret_key=secret_key,
                )
                or ""
            ).strip()
            if value:
                return value
        except TypeError:
            try:
                value = str(bl.get_project_secret(project_id, secret_key) or "").strip()
                if value:
                    return value
            except Exception:
                pass
        except Exception:
            pass
    return ""


def _resolve_auth_headers_and_object(
    *,
    auth_mode: str,
    auth_ref: str,
    state: dict[str, Any],
) -> tuple[dict[str, str], httpx.Auth | None]:
    mode = str(auth_mode or "none").strip().lower()
    ref = str(auth_ref or "").strip()
    if mode in {"", "none"}:
        return {}, None
    if not ref:
        raise ValueError("auth_ref is required when auth_mode is not none")
    if mode == "bearer":
        token = _resolve_secret_ref_value(ref, state)
        if not token:
            raise ValueError(f"auth_ref '{ref}' is not set")
        return {"Authorization": f"Bearer {token}"}, None
    if mode == "basic":
        user_env, sep, pass_env = ref.partition(":")
        username = _resolve_secret_ref_value(user_env.strip(), state)
        password = _resolve_secret_ref_value(pass_env.strip(), state) if sep else ""
        if not username:
            raise ValueError(f"auth_ref '{ref}' is not set")
        return {}, httpx.BasicAuth(username=username, password=password)
    raise ValueError(f"Unsupported auth_mode '{mode}'")


def _resolve_artifact_filename(
    config: dict[str, Any], state: dict[str, Any], response: httpx.Response, rendered_url: str
) -> str:
    explicit = config.get("artifact_filename")
    if explicit not in (None, ""):
        return _safe_plain_filename(str(_render_value(explicit, state) or ""))
    cd = str(response.headers.get("content-disposition") or "")
    filename = _filename_from_content_disposition(cd)
    if filename:
        return filename
    basename = _filename_from_url_path(rendered_url)
    if basename:
        return basename
    return "download.bin"


def _owner_scope_from_state(state: dict[str, Any]) -> dict[str, Any]:
    owner: dict[str, Any] = {}
    for key in ("account_id", "project_id", "job_id", "workflow_id"):
        value = state.get(key)
        if value not in (None, ""):
            owner[key] = value
    project_config = state.get("project_config")
    if isinstance(project_config, dict):
        for source_key, target_key in (
            ("account_id", "account_id"),
            ("project_id", "project_id"),
            ("id", "project_id"),
        ):
            value = project_config.get(source_key)
            if value not in (None, "") and target_key not in owner:
                owner[target_key] = value
    return owner or {"scope": "http_request"}


def _shared_artifact_plane_from_state(state: dict[str, Any]) -> Any | None:
    plane = state.get("_artifact_plane") or state.get("artifact_plane")
    if plane is not None:
        return plane
    try:
        from core.infrastructure.runtime import http_app_request_helpers

        plane = getattr(http_app_request_helpers, "_artifact_plane", None)
        if plane is not None:
            return plane
    except Exception:
        pass
    global _ARTIFACT_PLANE_SINGLETON
    if _ARTIFACT_PLANE_SINGLETON is not None:
        return _ARTIFACT_PLANE_SINGLETON
    try:
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_storage import create_storage_adapter
        from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage

        _ARTIFACT_PLANE_SINGLETON = build_artifact_storage(
            environ=dict(_runtime_env),
            create_storage_adapter=create_storage_adapter,
            artifact_plane_factory=ArtifactDatasetPlane,
        )
    except Exception:
        _ARTIFACT_PLANE_SINGLETON = None
    return _ARTIFACT_PLANE_SINGLETON


def _base_result(
    *,
    status_code: int | None,
    ok: bool,
    retryable: bool,
    failure_reason: str,
    response_excerpt: str,
    request: dict[str, Any],
    response: httpx.Response | None = None,
    response_mode: str = "inline",
) -> dict[str, Any]:
    headers = response.headers if response is not None else {}
    result = {
        "ok": ok,
        "status_code": status_code,
        "retryable": retryable,
        "failure_reason": failure_reason,
        "response_excerpt": response_excerpt,
        "response_headers": {
            "content_type": str(headers.get("content-type") or "").strip(),
            "content_length": str(headers.get("content-length") or "").strip(),
        },
        "request": request,
        "response_mode": response_mode,
    }
    if response_mode == "inline":
        result["response_body"] = ""
    result.update(
        _http_retry_facts(
            method=str(request.get("method") or "GET"),
            ok=ok,
            status_code=status_code,
            failure_reason=failure_reason,
            response=response,
        )
    )
    return result


def _http_retry_facts(
    *,
    method: str,
    ok: bool,
    status_code: int | None,
    failure_reason: str,
    response: httpx.Response | None = None,
) -> dict[str, Any]:
    """Classify generic HTTP outcomes without provider-specific knowledge."""
    normalized_method = str(method or "GET").strip().upper() or "GET"
    semantics = "safe_read" if normalized_method in _SAFE_READ_METHODS else "mutation"
    facts: dict[str, Any] = {
        "request_semantics": semantics,
        "retryable": False,
        "definitely_no_external_effect": semantics == "safe_read",
        "external_effect_status": "succeeded" if ok else "failed",
    }
    if status_code is not None:
        facts["http_status"] = int(status_code)
    if ok:
        return facts

    transport_failure = failure_reason in {"timeout", "network_error"}
    retryable_status = status_code in _RETRYABLE_HTTP_STATUSES
    if transport_failure:
        facts["failure_class"] = "transient"
    elif status_code is not None:
        facts["failure_class"] = "provider"
    else:
        facts["failure_class"] = "validation"

    facts["retryable"] = semantics == "safe_read" and (transport_failure or retryable_status)
    if semantics == "mutation" and (transport_failure or retryable_status):
        facts["external_effect_status"] = "timeout_unknown"
        facts["definitely_no_external_effect"] = False
    if response is not None:
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            facts["retry_after_seconds"] = retry_after
    return facts


def _read_http_input_ciphertext(account_id: str, ref_key: str) -> str | None:
    from core.infrastructure.db.model_provider_store import get_secret_ref_ciphertext

    store = _get_shared_logger()
    if store is None:
        return None
    return get_secret_ref_ciphertext(store.conn, account_id=account_id, ref_key=ref_key)


def _secret_failure(reason: str, boundary: HttpSecretBoundary) -> dict[str, Any]:
    uncertain_mutation = (
        boundary.request_dispatched and boundary.request_method not in _SAFE_READ_METHODS
    )
    return {
        "ok": False,
        "status_code": None,
        "retryable": False,
        "failure_reason": reason,
        "response_excerpt": "",
        "request": {},
        "request_semantics": "mutation"
        if boundary.request_method not in _SAFE_READ_METHODS
        else "safe_read",
        "external_effect_status": "timeout_unknown" if uncertain_mutation else "failed",
        "definitely_no_external_effect": not uncertain_mutation,
        "failure_class": "validation",
    }


def execute_http_request_step(
    *, config: dict[str, Any], state: dict[str, Any] | None = None
) -> dict[str, Any]:
    from core.infrastructure.security.secret_reference import (
        SecretReferenceUnauthorized,
        SecretReferenceUnresolved,
    )

    boundary = HttpSecretBoundary(dict(state or {}), _read_http_input_ciphertext)
    try:
        return boundary.safe_result(
            _execute_http_request_step(config=config, state=state, secret_boundary=boundary)
        )
    except (HttpSecretInputError, SecretReferenceUnauthorized, SecretReferenceUnresolved):
        return _secret_failure("unresolved_secret_input", boundary)
    except HttpSecretExposureError:
        return _secret_failure("secret_exposure_blocked", boundary)
    except Exception as exc:
        if not boundary.has_references:
            raise
        try:
            boundary.ensure_safe(str(exc))
        except HttpSecretExposureError:
            return _secret_failure("secret_exposure_blocked", boundary)
        return _secret_failure("http_request_error", boundary)


def _execute_http_request_step(
    *,
    config: dict[str, Any],
    state: dict[str, Any] | None = None,
    secret_boundary: HttpSecretBoundary,
) -> dict[str, Any]:
    runtime_state = dict(state or {})
    selector_issues = validate_step_state_selectors(
        {"step_kind": "http_request", "override": {"http_request": config}}
    )
    if selector_issues:
        issue = selector_issues[0]
        return {
            "ok": False,
            "failure_reason": RUNTIME_ERROR_CODE,
            "selector": issue["selector"],
            "path": issue["path"].removeprefix("step."),
            "request": {},
            "response_mode": str(config.get("response_mode") or "inline").strip().lower(),
        }
    response_mode = _normalize_response_mode(config.get("response_mode"))
    max_response_bytes = _response_size_limit(config, response_mode)
    rendered_method = secret_boundary.public_control(
        render_http_value(config.get("method") or "GET", runtime_state)
    )
    method = str(rendered_method or "GET").strip().upper() or "GET"
    secret_boundary.request_method = method
    url_template = str(config.get("url") or "").strip()
    rendered_url = str(
        secret_boundary.public_control(render_http_value(url_template, runtime_state)) or ""
    ).strip()
    if not rendered_url:
        raise ValueError("http_request url is required")
    rendered_url = _validate_http_request_url(rendered_url)
    auth_mode = str(config.get("auth_mode") or "none").strip().lower()
    if auth_mode in {"basic", "bearer"}:
        _validate_static_https_auth_origin(url_template, rendered_url)

    timeout_seconds = float(config.get("timeout_seconds") or 30.0)
    if timeout_seconds <= 0:
        raise ValueError("http_request timeout_seconds must be positive")
    try:
        deadline_epoch = float(runtime_state.get("_operation_deadline_epoch_seconds") or 0.0)
    except (TypeError, ValueError):
        deadline_epoch = 0.0
    if deadline_epoch > 0:
        remaining = deadline_epoch - time.time()
        if remaining <= 0:
            return _base_result(
                status_code=None,
                ok=False,
                retryable=False,
                failure_reason="operation_deadline_exhausted",
                response_excerpt="",
                request={"method": method, "url": rendered_url},
                response_mode=response_mode,
            )
        timeout_seconds = min(timeout_seconds, remaining)

    expected_statuses_raw = config.get("expected_statuses")
    expected_statuses: set[int] = set()
    if isinstance(expected_statuses_raw, list):
        for item in expected_statuses_raw:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if 100 <= parsed <= 599:
                expected_statuses.add(parsed)
    if not expected_statuses:
        expected_statuses = {200}

    secret_boundary.public_control(
        render_http_value(config.get("artifact_filename"), runtime_state)
    )
    presented = {
        "headers": render_http_value(config.get("headers") or {}, runtime_state),
        "query_params": render_http_value(config.get("query_params") or {}, runtime_state),
        "body": render_http_value(_request_body_template(config), runtime_state),
    }
    if has_http_secret(presented):
        secret_boundary.has_references = True
        _validate_static_https_auth_origin(url_template, rendered_url)
    dispatch = secret_boundary.resolve(presented) if secret_boundary.has_references else presented
    rendered_headers = _normalize_http_headers(dispatch["headers"])
    rendered_query = _normalize_query_params(dispatch["query_params"])
    rendered_body = dispatch["body"]
    public_payload = secret_boundary.metadata(presented)

    auth_headers, auth_obj = _resolve_auth_headers_and_object(
        auth_mode=auth_mode,
        auth_ref=str(config.get("auth_ref") or "").strip(),
        state=runtime_state,
    )
    merged_headers = {**rendered_headers, **auth_headers}
    if not any(str(key).lower() == "accept-encoding" for key in merged_headers):
        merged_headers["Accept-Encoding"] = "gzip, deflate"
    safe_request_metadata = {
        "method": method,
        "url": rendered_url,
        "query_params": _normalize_query_params(public_payload["query_params"]),
        "headers": _redact_request_headers(
            {**merged_headers, **_normalize_http_headers(public_payload["headers"]), **auth_headers}
        ),
    }
    if rendered_body not in (None, "", {}):
        # The body as sent, after templating; the run view masks credential
        # fields in it and bounds its size.
        safe_request_metadata["body"] = public_payload["body"]

    request_kwargs: dict[str, Any] = {
        "method": method,
        "url": rendered_url,
        "params": rendered_query or None,
        "headers": merged_headers or None,
        "timeout": timeout_seconds,
        "auth": auth_obj,
    }
    if rendered_body not in (None, "", {}):
        if isinstance(rendered_body, (dict, list)):
            request_kwargs["json"] = rendered_body
        else:
            request_kwargs["content"] = str(rendered_body).encode("utf-8")

    try:
        secret_boundary.request_dispatched = True
        with (
            httpx.Client(
                follow_redirects=False,
                transport=_SSRFGuardedHTTPTransport(),
            ) as client,
            client.stream(**request_kwargs) as response,
        ):
            _reject_blocked_response_peer(response)
            status_code = int(response.status_code)
            ok = status_code in expected_statuses
            has_body = _response_can_have_body(method, status_code)
            if not ok:
                prefix = b""
                decode_excerpt = ""
                if has_body:
                    try:
                        prefix = _bounded_prefix(
                            _decoded_response_chunks(
                                response,
                                max_size=ERROR_RESPONSE_PREFIX_BYTES,
                                stop_at_limit=True,
                            ),
                            limit=ERROR_RESPONSE_PREFIX_BYTES,
                        )
                    except httpx.HTTPError as exc:
                        decode_excerpt = str(exc)[:RESPONSE_TEXT_EXCERPT_CHARS]
                return _base_result(
                    status_code=status_code,
                    ok=False,
                    retryable=status_code == 429 or status_code >= 500,
                    failure_reason="unexpected_status",
                    response_excerpt=decode_excerpt
                    or _decode_excerpt(
                        prefix,
                        str(response.headers.get("content-type") or ""),
                    ),
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
            trustworthy_length = _trustworthy_content_length(response.headers)
            if (
                has_body
                and trustworthy_length is not None
                and trustworthy_length > max_response_bytes
            ):
                return _base_result(
                    status_code=status_code,
                    ok=False,
                    retryable=False,
                    failure_reason="response_too_large",
                    response_excerpt="",
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
            if response_mode == "inline":
                try:
                    if has_body:
                        within_limit, payload = _collect_inline_body(
                            _decoded_response_chunks(response, max_size=max_response_bytes),
                            max_response_bytes,
                        )
                    else:
                        within_limit, payload = True, b""
                except _ResponseTooLargeError:
                    within_limit, payload = False, b""
                except httpx.TimeoutException as exc:
                    return _base_result(
                        status_code=status_code,
                        ok=False,
                        retryable=True,
                        failure_reason="timeout",
                        response_excerpt=str(exc),
                        request=safe_request_metadata,
                        response=response,
                        response_mode=response_mode,
                    )
                except httpx.HTTPError as exc:
                    return _base_result(
                        status_code=status_code,
                        ok=False,
                        retryable=True,
                        failure_reason="network_error",
                        response_excerpt=str(exc),
                        request=safe_request_metadata,
                        response=response,
                        response_mode=response_mode,
                    )
                if not within_limit:
                    return _base_result(
                        status_code=status_code,
                        ok=False,
                        retryable=False,
                        failure_reason="response_too_large",
                        response_excerpt="",
                        request=safe_request_metadata,
                        response=response,
                        response_mode=response_mode,
                    )
                response_body, excerpt = _inline_payload_from_bytes(
                    payload,
                    str(response.headers.get("content-type") or ""),
                )
                result = _base_result(
                    status_code=status_code,
                    ok=True,
                    retryable=False,
                    failure_reason="",
                    response_excerpt=excerpt,
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
                result["response_body"] = response_body
                return result

            plane = _shared_artifact_plane_from_state(runtime_state)
            if plane is None or not hasattr(plane, "create_artifact_from_stream"):
                return _base_result(
                    status_code=status_code,
                    ok=False,
                    retryable=False,
                    failure_reason="storage_error",
                    response_excerpt="artifact storage is unavailable",
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
            filename = _resolve_artifact_filename(
                config,
                runtime_state,
                response,
                rendered_url,
            )
            content_type = _media_type(str(response.headers.get("content-type") or ""))
            secret_boundary.ensure_safe({"filename": filename, "content_type": content_type})
            try:
                handle, meta = plane.create_artifact_from_stream(
                    owner_scope=_owner_scope_from_state(runtime_state),
                    run_id=str(
                        runtime_state.get("run_id") or runtime_state.get("job_id") or "http_request"
                    ),
                    step_id=str(runtime_state.get("step_id") or "http_request"),
                    filename=filename,
                    content_type=content_type,
                    retention_class="run_window",
                    max_size_bytes=max_response_bytes,
                    chunks=secret_boundary.response_chunks(
                        _decoded_response_chunks(response, max_size=max_response_bytes),
                        str(response.headers.get("content-type") or ""),
                    )
                    if has_body
                    else (),
                )
            except HttpSecretExposureError:
                raise
            except httpx.TimeoutException as exc:
                return _base_result(
                    status_code=status_code,
                    ok=False,
                    retryable=True,
                    failure_reason="timeout",
                    response_excerpt=str(exc),
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
            except httpx.HTTPError as exc:
                return _base_result(
                    status_code=status_code,
                    ok=False,
                    retryable=True,
                    failure_reason="network_error",
                    response_excerpt=str(exc),
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
            except _ResponseTooLargeError:
                return _base_result(
                    status_code=status_code,
                    ok=False,
                    retryable=False,
                    failure_reason="response_too_large",
                    response_excerpt="",
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
            except ValueError as exc:
                reason = (
                    "response_too_large"
                    if "max_size_bytes" in str(exc) or "exceeds" in str(exc)
                    else "storage_error"
                )
                return _base_result(
                    status_code=status_code,
                    ok=False,
                    retryable=False,
                    failure_reason=reason,
                    response_excerpt="" if reason == "response_too_large" else str(exc),
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
            except RuntimeError as exc:
                return _base_result(
                    status_code=status_code,
                    ok=False,
                    retryable=False,
                    failure_reason="artifact_integrity_error"
                    if "artifact_integrity_error" in str(exc)
                    else "storage_error",
                    response_excerpt=str(exc),
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
            except Exception as exc:
                return _base_result(
                    status_code=status_code,
                    ok=False,
                    retryable=False,
                    failure_reason="storage_error",
                    response_excerpt=str(exc),
                    request=safe_request_metadata,
                    response=response,
                    response_mode=response_mode,
                )
            artifact_id = str(getattr(handle, "artifact_id", "") or meta.get("artifact_id") or "")
            artifact_handle = f"artifact:{artifact_id}" if artifact_id else ""
            result = _base_result(
                status_code=status_code,
                ok=True,
                retryable=False,
                failure_reason="",
                response_excerpt="",
                request=safe_request_metadata,
                response=response,
                response_mode=response_mode,
            )
            result.update(
                {
                    "artifact_handle": artifact_handle,
                    "artifact_id": artifact_id,
                    "filename": str(meta.get("filename") or filename),
                    "content_type": str(meta.get("content_type") or content_type),
                    "size_bytes": int(meta.get("size_bytes") or 0),
                    "sha256": str(meta.get("sha256") or ""),
                }
            )
            return result
    except httpx.TimeoutException as exc:
        result = {
            "ok": False,
            "status_code": None,
            "failure_reason": "timeout",
            "response_excerpt": str(exc),
            "request": safe_request_metadata,
            "response_mode": response_mode,
            **({"response_body": ""} if response_mode == "inline" else {}),
        }
        result.update(
            _http_retry_facts(
                method=method,
                ok=False,
                status_code=None,
                failure_reason="timeout",
            )
        )
        return result
    except httpx.HTTPError as exc:
        result = {
            "ok": False,
            "status_code": None,
            "failure_reason": "network_error",
            "response_excerpt": str(exc),
            "request": safe_request_metadata,
            "response_mode": response_mode,
            **({"response_body": ""} if response_mode == "inline" else {}),
        }
        result.update(
            _http_retry_facts(
                method=method,
                ok=False,
                status_code=None,
                failure_reason="network_error",
            )
        )
        return result
