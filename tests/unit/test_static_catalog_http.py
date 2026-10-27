"""Bounded outbound HTTP for the static catalogs.

These run against a real loopback HTTP server rather than a stubbed opener, so
the redirect chain, ``ETag``/``304`` handling, ``Content-Length`` and the
streamed read ceiling are exercised through the same urllib path production
uses. Only the https-scheme assertion is relaxed for loopback; a separate test
pins that the unrelaxed helper still rejects plain http.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from core.application.extensions.binding_constants import MAX_EXTENSION_ARCHIVE_BYTES
from core.application.extensions.url_safety import (
    assert_safe_remote_https_url,
    open_pinned_url,
)
from core.infrastructure.static_catalog import http_fetch
from core.infrastructure.static_catalog.http_fetch import (
    CatalogArtifactMismatchError,
    CatalogFetchError,
    CatalogNotModifiedError,
    CatalogUrlGuard,
    fetch_catalog_artifact,
    fetch_catalog_index_json,
)
from core.infrastructure.static_catalog.settings import MAX_EXTENSION_RELEASE_ZIP_BYTES


@dataclass
class _Reply:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _Stand:
    base_url: str
    routes: dict[str, Callable[[dict[str, str]], _Reply]]
    requests: list[tuple[str, dict[str, str]]]


@pytest.fixture
def stand() -> Iterator[_Stand]:
    routes: dict[str, Callable[[dict[str, str]], _Reply]] = {}
    seen: list[tuple[str, dict[str, str]]] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            headers = {key.lower(): value for key, value in self.headers.items()}
            seen.append((self.path, headers))
            route = routes.get(self.path)
            reply = route(headers) if route else _Reply(status=404, body=b"missing")
            self.send_response(reply.status)
            for key, value in reply.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            if reply.body:
                self.wfile.write(reply.body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield _Stand(base_url=f"http://{host}:{port}", routes=routes, requests=seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


#: Exactly the binding each business area makes in production.
GUARD = CatalogUrlGuard(
    assert_safe_remote_https_url=assert_safe_remote_https_url,
    open_pinned_url=open_pinned_url,
)


def fetch_index(url: str, **kwargs: Any) -> Any:
    return fetch_catalog_index_json(url, guard=GUARD, **kwargs)


def fetch_artifact(url: str, **kwargs: Any) -> bytes:
    return fetch_catalog_artifact(url, guard=GUARD, **kwargs)


@pytest.fixture(autouse=True)
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real transport but let it dial the loopback test server."""
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(
        http_fetch,
        "_safe_https_url",
        lambda _guard, url, *, purpose: str(url),
    )


def _json_reply(payload: object, **headers: str) -> Callable[[dict[str, str]], _Reply]:
    body = json.dumps(payload).encode("utf-8")
    return lambda _headers: _Reply(
        status=200, body=body, headers={"Content-Type": "application/json", **headers}
    )


def test_first_load_decodes_the_index_and_keeps_the_etag(stand: _Stand) -> None:
    stand.routes["/index.json"] = _json_reply({"schema_version": "x"}, ETag='"rev-1"')

    fetched = fetch_index(f"{stand.base_url}/index.json", max_bytes=1024, purpose="index")

    assert fetched.payload == {"schema_version": "x"}
    assert fetched.etag == '"rev-1"'


def test_stored_etag_is_sent_as_if_none_match_and_304_is_reported(stand: _Stand) -> None:
    def _route(headers: dict[str, str]) -> _Reply:
        if headers.get("if-none-match") == '"rev-1"':
            return _Reply(status=304)
        return _Reply(status=200, body=b"{}")

    stand.routes["/index.json"] = _route

    with pytest.raises(CatalogNotModifiedError):
        fetch_index(f"{stand.base_url}/index.json", max_bytes=1024, purpose="index", etag='"rev-1"')
    assert stand.requests[-1][1]["if-none-match"] == '"rev-1"'


def test_last_modified_is_kept_and_sent_back_as_if_modified_since(stand: _Stand) -> None:
    stamp = "Fri, 16 Oct 2026 10:00:00 GMT"

    def _route(headers: dict[str, str]) -> _Reply:
        if headers.get("if-modified-since") == stamp:
            return _Reply(status=304)
        return _Reply(status=200, body=b"{}", headers={"Last-Modified": stamp})

    stand.routes["/index.json"] = _route
    url = f"{stand.base_url}/index.json"

    first = fetch_index(url, max_bytes=1024, purpose="index")
    assert first.last_modified == stamp
    with pytest.raises(CatalogNotModifiedError):
        fetch_index(url, max_bytes=1024, purpose="index", last_modified=first.last_modified)
    assert stand.requests[-1][1]["if-modified-since"] == stamp


def test_a_host_that_always_answers_200_keeps_working(stand: _Stand) -> None:
    """No ETag anywhere: correctness must not depend on conditional requests."""
    stand.routes["/index.json"] = _json_reply({"catalog_revision": "r1"})

    first = fetch_index(f"{stand.base_url}/index.json", max_bytes=1024, purpose="index")
    second = fetch_index(
        f"{stand.base_url}/index.json", max_bytes=1024, purpose="index", etag=first.etag
    )

    assert first.etag == ""
    assert second.payload == {"catalog_revision": "r1"}


def test_redirects_are_followed_within_the_bound(stand: _Stand) -> None:
    stand.routes["/start.json"] = lambda _h: _Reply(status=302, headers={"Location": "/final.json"})
    stand.routes["/final.json"] = _json_reply({"ok": True})

    fetched = fetch_index(f"{stand.base_url}/start.json", max_bytes=1024, purpose="index")

    assert fetched.payload == {"ok": True}


def test_a_redirect_loop_is_cut_off_at_the_bound(stand: _Stand) -> None:
    stand.routes["/loop.json"] = lambda _h: _Reply(status=302, headers={"Location": "/loop.json"})

    with pytest.raises(CatalogFetchError, match="exceeded"):
        fetch_index(f"{stand.base_url}/loop.json", max_bytes=1024, purpose="index", max_redirects=2)


def test_a_redirect_without_a_location_is_an_error(stand: _Stand) -> None:
    stand.routes["/nowhere.json"] = lambda _h: _Reply(status=302)

    with pytest.raises(CatalogFetchError, match="Location"):
        fetch_index(f"{stand.base_url}/nowhere.json", max_bytes=1024, purpose="index")


def test_an_http_failure_is_reported_with_its_status(stand: _Stand) -> None:
    stand.routes["/broken.json"] = lambda _h: _Reply(status=503, body=b"nope")

    with pytest.raises(CatalogFetchError, match="HTTP 503"):
        fetch_index(f"{stand.base_url}/broken.json", max_bytes=1024, purpose="index")


def test_a_declared_content_length_over_the_limit_is_refused(stand: _Stand) -> None:
    stand.routes["/big.json"] = lambda _h: _Reply(status=200, body=b"x" * 4096)

    with pytest.raises(CatalogFetchError, match="byte limit"):
        fetch_index(f"{stand.base_url}/big.json", max_bytes=64, purpose="index")


def test_a_non_json_body_is_refused(stand: _Stand) -> None:
    stand.routes["/text.json"] = lambda _h: _Reply(status=200, body=b"not json")

    with pytest.raises(CatalogFetchError, match="not valid JSON"):
        fetch_index(f"{stand.base_url}/text.json", max_bytes=1024, purpose="index")


def test_a_json_array_is_refused_because_an_index_is_an_object(stand: _Stand) -> None:
    stand.routes["/array.json"] = _json_reply([1, 2, 3])

    with pytest.raises(CatalogFetchError, match="JSON object"):
        fetch_index(f"{stand.base_url}/array.json", max_bytes=1024, purpose="index")


def test_a_verified_artifact_is_returned(stand: _Stand) -> None:
    body = b"workflow: bundle\n"
    stand.routes["/a.yaml"] = lambda _h: _Reply(status=200, body=body)

    fetched = fetch_artifact(
        f"{stand.base_url}/a.yaml",
        expected_bytes=len(body),
        expected_sha256=hashlib.sha256(body).hexdigest(),
        max_bytes=1024,
        purpose="artifact",
    )

    assert fetched == body


def test_a_size_mismatch_refuses_the_artifact(stand: _Stand) -> None:
    body = b"shorter than declared"
    stand.routes["/a.yaml"] = lambda _h: _Reply(status=200, body=body)

    digest = hashlib.sha256(body).hexdigest()

    with pytest.raises(CatalogFetchError):
        fetch_artifact(
            f"{stand.base_url}/a.yaml",
            expected_bytes=len(body) + 10,
            expected_sha256=digest,
            max_bytes=1024,
            purpose="artifact",
        )


def test_a_digest_mismatch_refuses_the_artifact(stand: _Stand) -> None:
    body = b"tampered"
    stand.routes["/a.yaml"] = lambda _h: _Reply(status=200, body=body)

    with pytest.raises(CatalogArtifactMismatchError, match="SHA-256"):
        fetch_artifact(
            f"{stand.base_url}/a.yaml",
            expected_bytes=len(body),
            expected_sha256="0" * 64,
            max_bytes=1024,
            purpose="artifact",
        )


def test_a_body_longer_than_declared_is_cut_off_before_it_is_buffered(stand: _Stand) -> None:
    body = b"y" * 4096
    stand.routes["/a.zip"] = lambda _h: _Reply(status=200, body=body)

    digest = hashlib.sha256(body).hexdigest()

    with pytest.raises(CatalogFetchError, match="byte limit"):
        fetch_artifact(
            f"{stand.base_url}/a.zip",
            expected_bytes=16,
            expected_sha256=digest,
            max_bytes=8192,
            purpose="artifact",
        )


@pytest.mark.parametrize(
    ("expected_bytes", "digest", "match"),
    [
        (0, "a" * 64, "no byte size"),
        (10, "not-a-digest", "SHA-256 digest"),
        (10_000_000, "a" * 64, "byte limit"),
    ],
)
def test_an_unusable_artifact_declaration_never_reaches_the_network(
    stand: _Stand, expected_bytes: int, digest: str, match: str
) -> None:
    with pytest.raises(CatalogArtifactMismatchError, match=match):
        fetch_artifact(
            f"{stand.base_url}/never.zip",
            expected_bytes=expected_bytes,
            expected_sha256=digest,
            max_bytes=1024,
            purpose="artifact",
        )
    assert stand.requests == []


def test_plain_http_is_refused_by_the_unrelaxed_url_check(
    monkeypatch: pytest.MonkeyPatch, stand: _Stand
) -> None:
    monkeypatch.undo()
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")

    with pytest.raises(CatalogFetchError, match="not allowed"):
        fetch_index(f"{stand.base_url}/index.json", max_bytes=1024, purpose="index")


def test_the_catalog_archive_ceiling_tracks_the_installer_ceiling() -> None:
    """A catalog limit above the installer's would only fail later, inside it."""
    assert MAX_EXTENSION_RELEASE_ZIP_BYTES == MAX_EXTENSION_ARCHIVE_BYTES


def _gzip_reply(raw: bytes) -> Callable[[dict[str, str]], _Reply]:
    body = gzip.compress(raw)
    return lambda _headers: _Reply(
        status=200,
        body=body,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip", "ETag": 'W/"gz"'},
    )


def test_the_index_is_asked_for_gzip_and_decoded(stand: _Stand) -> None:
    """GitHub's raw host serves gzip when asked; a multi-megabyte index shrinks ninefold."""
    payload = {"schema_version": "x", "items": ["a" * 500] * 50}
    stand.routes["/index.json"] = _gzip_reply(json.dumps(payload).encode())

    fetched = fetch_index(f"{stand.base_url}/index.json", max_bytes=1_000_000, purpose="index")

    assert fetched.payload == payload
    assert fetched.etag == 'W/"gz"'
    assert stand.requests[-1][1]["accept-encoding"] == "gzip"


def test_a_gzip_body_that_expands_past_the_limit_is_refused(stand: _Stand) -> None:
    """A tiny compressed body must not inflate into an unbounded one."""
    stand.routes["/bomb.json"] = _gzip_reply(b'{"x": "' + b"0" * 2_000_000 + b'"}')

    with pytest.raises(CatalogFetchError, match="expands past"):
        fetch_index(f"{stand.base_url}/bomb.json", max_bytes=100_000, purpose="index")


def test_an_unsupported_encoding_is_refused(stand: _Stand) -> None:
    stand.routes["/br.json"] = lambda _h: _Reply(
        status=200, body=b"\x0b\x02\x80{}\x03", headers={"Content-Encoding": "br"}
    )
    with pytest.raises(CatalogFetchError, match="unsupported encoding br"):
        fetch_index(f"{stand.base_url}/br.json", max_bytes=1000, purpose="index")


def test_artifacts_are_never_requested_compressed(stand: _Stand) -> None:
    """An artifact's SHA-256 is of the published bytes, so it travels as published."""
    data = b"archive-bytes"
    stand.routes["/a.zip"] = lambda _h: _Reply(status=200, body=data)
    fetch_artifact(
        f"{stand.base_url}/a.zip",
        expected_bytes=len(data),
        expected_sha256=hashlib.sha256(data).hexdigest(),
        max_bytes=1000,
        purpose="artifact",
    )
    assert "accept-encoding" not in stand.requests[-1][1] or stand.requests[-1][1][
        "accept-encoding"
    ] in ("identity",)
