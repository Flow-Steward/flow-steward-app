"""ADR 0047 decision 5: CIMD-only public clients (task5b C1-C4)."""

from __future__ import annotations

import json
import time
from email.message import Message
from urllib.error import HTTPError

import pytest

from core.application.auth import inbound_oauth_clients as clients

CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"


def _document(**overrides) -> bytes:
    document = {
        "client_id": CLIENT_ID,
        "client_name": "Desktop agent",
        "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    document.update(overrides)
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    "client_id",
    [
        "https://evil.example/client.json",
        "https://sub.claude.ai/client.json",
        "http://claude.ai/client.json",
        "https://claude.ai/client.json?x=1",
        "https://claude.ai/client.json#f",
        "https://user@claude.ai/client.json",
        "https://claude.ai",
        "https://claude.ai/",
        "https://claude.ai:8443/client.json",
        "https://claude.ai.evil.example/client.json",
        "",
        "https://claude.ai/" + "a" * 2050,
    ],
)
def test_client_id_outside_the_allowlist_is_refused_before_any_fetch(client_id: str) -> None:
    """C1."""
    with pytest.raises(clients.ClientNotAllowedError):
        clients.validate_client_id(client_id)


def test_allowed_client_ids() -> None:
    assert clients.validate_client_id(CLIENT_ID) == CLIENT_ID
    assert clients.validate_client_id("https://chatgpt.com/oauth/client.json")
    assert frozenset({"chatgpt.com", "claude.ai"}) == clients.ALLOWED_CLIENT_ID_HOSTS


@pytest.mark.parametrize("document_client_id", [CLIENT_ID[:-1] + "X", CLIENT_ID + "/"])
def test_document_client_id_must_match_exactly(document_client_id: str) -> None:
    """C2."""
    document = _document(client_id=document_client_id)
    with pytest.raises(clients.InvalidClientMetadataError):
        clients.validate_client_metadata(document, client_id=CLIENT_ID)


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_endpoint_auth_method": "private_key_jwt"},
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"grant_types": ["refresh_token"]},
        {"grant_types": ["authorization_code", "client_credentials"]},
        {"response_types": ["token"]},
        {"redirect_uris": ["https://claude.ai/cb#frag"]},
        {"redirect_uris": ["http://claude.ai/cb"]},
        {"redirect_uris": []},
        {"redirect_uris": "https://claude.ai/cb"},
    ],
)
def test_unsupported_documents_are_refused(overrides: dict) -> None:
    """C3."""
    document = _document(**overrides)
    with pytest.raises(clients.InvalidClientMetadataError):
        clients.validate_client_metadata(document, client_id=CLIENT_ID)


def test_valid_document_keeps_name_and_redirects() -> None:
    metadata = clients.validate_client_metadata(_document(), client_id=CLIENT_ID)
    assert metadata.client_name == "Desktop agent"
    assert metadata.redirect_uris == ("http://localhost/callback", "http://127.0.0.1/callback")
    assert len(metadata.metadata_sha256) == 64
    unnamed = clients.validate_client_metadata(
        _document(client_name=None, token_endpoint_auth_method=None), client_id=CLIENT_ID
    )
    assert unnamed.client_name == ""
    with pytest.raises(clients.InvalidClientMetadataError):
        clients.validate_client_metadata(b"[]", client_id=CLIENT_ID)
    with pytest.raises(clients.InvalidClientMetadataError):
        clients.validate_client_metadata(b"not json", client_id=CLIENT_ID)


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("", 3600),
        ("max-age=60", 300),
        ("public, max-age=7200", 7200),
        ("max-age=999999", 86400),
        ("no-store", 3600),
    ],
)
def test_cache_lifetime_is_clamped(header: str, seconds: int) -> None:
    assert clients.cache_lifetime(header).total_seconds() == seconds


class _Response:
    def __init__(self, body: bytes, *, status: int = 200, delay: float = 0.0) -> None:
        self._body = body
        self.status = status
        self.headers = Message()
        self.headers["Cache-Control"] = "max-age=600"
        self._delay = delay

    def read(self, limit: int) -> bytes:
        if self._delay:
            time.sleep(self._delay)
        return self._body[:limit]

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def _public(_host: str, _port: int) -> tuple[str, ...]:
    return ("160.79.104.10",)


def test_fetch_reads_a_bounded_document_from_a_pinned_public_peer() -> None:
    seen: dict = {}

    def _open(request, *, timeout, ips):
        seen.update(url=request.full_url, accept=request.get_header("Accept"), ips=ips)
        seen["timeout"] = timeout
        return _Response(_document())

    fetched = clients.fetch_client_metadata(CLIENT_ID, resolve_ips=_public, open_request=_open)
    assert seen == {
        "url": CLIENT_ID,
        "accept": "application/json",
        "ips": ("160.79.104.10",),
        "timeout": 5.0,
    }
    assert fetched.cache_control == "max-age=600"


def test_fetch_refuses_a_redirect() -> None:
    """C4: redirects are failures (the pinned opener never follows them)."""

    def _open(request, **_kwargs):
        raise HTTPError(request.full_url, 302, "Found", Message(), None)

    with pytest.raises(clients.ClientMetadataUnavailableError):
        clients.fetch_client_metadata(CLIENT_ID, resolve_ips=_public, open_request=_open)


def test_fetch_identifies_the_product_to_a_metadata_endpoint_rejecting_generic_clients() -> None:
    """Claude's public CIMD endpoint rejects the default Python user agent with 403."""

    def _open(request, *, timeout, ips):
        user_agent = request.get_header("User-agent") or ""
        if not user_agent.startswith("FlowSteward/") or "https://flowsteward.org" not in user_agent:
            raise HTTPError(request.full_url, 403, "Forbidden", Message(), None)
        assert request.get_header("Accept") == "application/json"
        assert ips == ("160.79.104.10",)
        assert timeout == 5.0
        return _Response(_document())

    fetched = clients.fetch_client_metadata(CLIENT_ID, resolve_ips=_public, open_request=_open)
    metadata = clients.validate_client_metadata(fetched.body, client_id=CLIENT_ID)
    assert metadata.client_name == "Desktop agent"
    assert metadata.redirect_uris == ("http://localhost/callback", "http://127.0.0.1/callback")


def test_fetch_refuses_an_oversized_body() -> None:
    """C4: 70 KiB is over the 64 KiB limit."""
    body = b"{" + b" " * (70 * 1024) + b"}"
    with pytest.raises(clients.ClientMetadataUnavailableError):
        clients.fetch_client_metadata(
            CLIENT_ID, resolve_ips=_public, open_request=lambda *_a, **_k: _Response(body)
        )


def test_fetch_refuses_a_slow_answer() -> None:
    """C4: the 5 s limit holds even when the server trickles its answer."""
    ticks = iter([0.0, 6.0])
    with pytest.raises(clients.ClientMetadataUnavailableError):
        clients.fetch_client_metadata(
            CLIENT_ID,
            resolve_ips=_public,
            open_request=lambda *_a, **_k: _Response(_document()),
            clock=lambda: next(ticks),
        )


def test_fetch_refuses_a_non_200_answer() -> None:
    with pytest.raises(clients.ClientMetadataUnavailableError):
        clients.fetch_client_metadata(
            CLIENT_ID,
            resolve_ips=_public,
            open_request=lambda *_a, **_k: _Response(_document(), status=204),
        )


def test_private_peers_are_refused_even_when_private_urls_are_allowed(monkeypatch) -> None:
    """C4: a name resolving to 10.0.0.1 is refused whatever FS_ALLOW_PRIVATE_REMOTE_URLS says."""
    from core.application.extensions import lifecycle_descriptor_io

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(
        lifecycle_descriptor_io, "_resolve_hostname_ips", lambda _host, *, port: ["10.0.0.1"]
    )
    opened: list[str] = []

    def _open(request, **_kwargs):
        opened.append(request.full_url)
        return _Response(_document())

    with pytest.raises(clients.ClientMetadataUnavailableError):
        clients.fetch_client_metadata(CLIENT_ID, open_request=_open)
    assert opened == []
