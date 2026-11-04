"""Authenticated HTTP step requests: the static-origin rule and secret-input credentials.

Split from `http_request_step_runtime` (ADR 0057). Basic and bearer auth require a
literal HTTPS origin; their credentials may name workflow secret inputs, which the
HTTP secret boundary resolves like any other opaque input reference.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from core.application.orchestration.http_secret_boundary import (
    HttpSecretInputError,
    has_http_secret,
    render_http_value,
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


def _split_basic_auth_ref(ref: str) -> tuple[str, str]:
    """`username:password` of a basic auth reference; a colon inside `{{ }}` is not one."""
    depth = 0
    for index, char in enumerate(ref):
        if ref.startswith("{{", index):
            depth += 1
        elif ref.startswith("}}", index) and depth:
            depth -= 1
        elif char == ":" and depth == 0:
            return ref[:index].strip(), ref[index + 1 :].strip()
    return ref.strip(), ""


def _presented_input_auth(
    *, auth_mode: str, auth_ref: str, state: dict[str, Any]
) -> dict[str, Any] | None:
    """Basic or bearer credentials taken from workflow secret inputs, still opaque.

    An auth reference that is a template (`{{ state.inputs.api_key }}`) names secret
    inputs; each part must render to a secret reference, so the HTTP secret boundary
    resolves it with the run's authority, the approval gate and its destinations,
    exactly as a header carrying the same input. A plain value is refused: auth
    credentials never come from an ordinary input. None means a project secret key.
    """
    if auth_mode not in {"basic", "bearer"} or "{{" not in auth_ref:
        return None
    parts = (
        {"token": auth_ref}
        if auth_mode == "bearer"
        else dict(zip(("username", "password"), _split_basic_auth_ref(auth_ref), strict=True))
    )
    presented: dict[str, Any] = {}
    for name, template in parts.items():
        if not template:
            continue
        rendered = render_http_value(template, state)
        if not has_http_secret(rendered):
            raise HttpSecretInputError(f"auth_ref {name} must name a secret input")
        presented[name] = rendered
    if not presented.get("token") and not presented.get("username"):
        raise ValueError("auth_ref is required when auth_mode is not none")
    return presented


def _input_auth_headers_and_object(
    auth_mode: str, resolved: dict[str, Any]
) -> tuple[dict[str, str], httpx.Auth | None]:
    if auth_mode == "bearer":
        token = str(resolved.get("token") or "").strip()
        if not token:
            raise ValueError("auth_ref secret input is not set")
        return {"Authorization": f"Bearer {token}"}, None
    username = str(resolved.get("username") or "").strip()
    if not username:
        raise ValueError("auth_ref secret input is not set")
    return {}, httpx.BasicAuth(username=username, password=str(resolved.get("password") or ""))
