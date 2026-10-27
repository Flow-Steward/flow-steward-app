"""Test-only transport for exercising stored HTTPS runtime URLs on loopback."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def install_loopback_https_runtime_transport(
    monkeypatch,
    *,
    https_base_url: str,
    http_base_url: str,
) -> None:
    """Keep the persisted URL HTTPS while routing the test socket to HTTP loopback."""
    from core.application.extensions import remote_runtime_protocol_v2, url_safety
    from core.infrastructure.db import remote_extension_runtime

    def _validate_https(url: str, *, purpose: str) -> str:
        _ = purpose
        normalized = str(url or "").strip()
        if urlparse(normalized).scheme.lower() != "https":
            raise ValueError("remote_base_url must use https URL")
        return normalized

    def _http_call(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> tuple[int, Any]:
        if not url.startswith(https_base_url):
            raise ValueError("unexpected remote runtime URL")
        local_url = f"{http_base_url}{url[len(https_base_url):]}"
        data = None
        if method.upper() != "GET":
            data = json.dumps(payload).encode("utf-8")
        request = Request(local_url, data=data, method=method.upper(), headers=headers)
        try:
            response = urlopen(request, timeout=timeout)  # nosec B310 - loopback test fixture
        except HTTPError as exc:
            raw = exc.read()
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
            return int(exc.code), decoded
        with response:
            raw = response.read()
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
            return int(response.status), decoded

    real_client = remote_runtime_protocol_v2.RemoteRuntimeClient

    def _client_factory(**kwargs):
        return real_client(**kwargs, http_call=_http_call)

    monkeypatch.setattr(url_safety, "assert_safe_remote_https_url", _validate_https)
    monkeypatch.setattr(remote_extension_runtime, "RemoteRuntimeClient", _client_factory)
