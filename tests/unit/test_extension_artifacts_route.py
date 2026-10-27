"""AC17-R123: the route that serves artifacts to extensions, and what it refuses."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth.config import PUBLIC_WEBHOOK_PATH_PREFIXES
from core.application.extensions.runtime_grants import PLATFORM_SERVED_ARTIFACT_PATH
from core.entrypoints.api.extension_artifacts import create_extension_artifacts_router
from core.infrastructure.security.artifact_access_tokens import (
    HEADER_NAME,
    issue_artifact_access_token,
)

_BODY = b"hello-r123"


class _Plane:
    def __init__(self, *, stream: bool = True, written: dict | None = None) -> None:
        self._stream = stream
        self.writes: list[tuple[str, bytes]] = []
        self._written = written if written is not None else {"sha256": "b" * 64}
        self.raise_on_write: Exception | None = None

    def artifact_local_stream(self, artifact_id: str):
        if not self._stream:
            return None
        return (iter([_BODY]), len(_BODY))

    def write_artifact_content(self, artifact_id: str, *, body: bytes, content_type: str = ""):
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.writes.append((artifact_id, body))
        return self._written


class _Logger:
    def warning(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - noise only
        pass


@pytest.fixture(autouse=True)
def _deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "a-deployment-secret-for-tests")


def _client(plane: _Plane) -> TestClient:
    app = FastAPI()
    app.include_router(create_extension_artifacts_router(artifact_plane=plane, logger=_Logger()))
    return TestClient(app)


def _url(object_id: str = "art_1", kind: str = "artifact") -> str:
    return f"{PLATFORM_SERVED_ARTIFACT_PATH}/{kind}/{object_id}"


def _headers(method: str = "GET", object_id: str = "art_1", kind: str = "artifact") -> dict:
    return {
        HEADER_NAME: issue_artifact_access_token(
            object_id=object_id, kind=kind, method=method
        )
    }


def test_a_valid_read_token_gets_the_body() -> None:
    response = _client(_Plane()).get(_url(), headers=_headers("GET"))

    assert response.status_code == 200
    assert response.content == _BODY


def test_a_valid_write_token_stores_the_body_and_finalizes() -> None:
    plane = _Plane()

    response = _client(plane).put(_url(), content=_BODY, headers=_headers("PUT"))

    assert response.status_code == 204
    assert plane.writes == [("art_1", _BODY)]


def test_no_token_is_refused() -> None:
    assert _client(_Plane()).get(_url()).status_code == 403


def test_a_read_token_cannot_write() -> None:
    """The narrowest thing this has to get right."""
    response = _client(_Plane()).put(_url(), content=_BODY, headers=_headers("GET"))

    assert response.status_code == 403


def test_a_write_token_cannot_read() -> None:
    assert _client(_Plane()).get(_url(), headers=_headers("PUT")).status_code == 403


def test_a_token_for_another_artifact_is_refused() -> None:
    response = _client(_Plane()).get(_url("art_1"), headers=_headers("GET", object_id="art_2"))

    assert response.status_code == 403


def test_a_token_from_another_deployment_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _headers("GET")
    monkeypatch.setenv("FS_SECRET_KEY", "a-different-deployments-secret")

    assert _client(_Plane()).get(_url(), headers=headers).status_code == 403


def test_a_second_write_with_a_live_token_is_refused() -> None:
    """Single use is the extension's own bookkeeping; this is what makes it true here."""
    plane = _Plane()
    plane.raise_on_write = ValueError("Artifact bodies are immutable once written")

    response = _client(plane).put(_url(), content=_BODY, headers=_headers("PUT"))

    assert response.status_code == 403


def test_every_refusal_looks_the_same() -> None:
    """Separating "expired" from "wrong object" tells a caller which half to vary."""
    client = _client(_Plane())
    refusals = {
        client.get(_url()).text,
        client.get(_url(), headers=_headers("PUT")).text,
        client.get(_url("art_1"), headers=_headers("GET", object_id="art_2")).text,
    }

    assert len(refusals) == 1


def test_an_authorized_request_for_a_body_that_is_gone_is_not_a_refusal() -> None:
    """The token was good; saying 403 would send the caller looking at the wrong thing."""
    response = _client(_Plane(stream=False)).get(_url(), headers=_headers("GET"))

    assert response.status_code == 503


def test_the_path_family_is_exempt_from_the_session_check() -> None:
    """Otherwise the route is unreachable by the only caller it exists for.

    Asserted against the one list that answers "what does not require a session", so
    a future reorganisation of that list cannot quietly close this door.
    """
    from core.application.auth.enforce import is_public_auth_path

    assert f"{PLATFORM_SERVED_ARTIFACT_PATH}/" in PUBLIC_WEBHOOK_PATH_PREFIXES
    assert is_public_auth_path(_url())
    assert not is_public_auth_path("/api/jobs/j1/artifacts/a1")


# --- P2: the upload limit was checked after the whole body had been read ---


def test_an_oversized_upload_is_refused_without_reading_all_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`await request.body()` came first and the length check second.

    So a client could make the server buffer any amount before hearing 413. Counted at
    the server, through the ASGI receive channel: a test client drains its own body
    generator whatever the server does, so counting there would measure the client.
    """
    import asyncio

    from starlette.requests import Request

    from core.entrypoints.api import extension_artifacts

    monkeypatch.setattr(extension_artifacts, "_MAX_UPLOAD_BYTES", 1_000)
    handed_out = 0

    async def receive():
        nonlocal handed_out
        handed_out += 1
        return {"type": "http.request", "body": b"x" * 500, "more_body": handed_out < 200}

    request = Request({"type": "http", "method": "PUT", "headers": []}, receive)

    body = asyncio.run(extension_artifacts._bounded_body(request))

    assert body is None
    assert handed_out < 5, f"the server read {handed_out} of 200 chunks before refusing"


def test_an_oversized_upload_is_answered_413_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.entrypoints.api import extension_artifacts

    monkeypatch.setattr(extension_artifacts, "_MAX_UPLOAD_BYTES", 1_000)
    plane = _Plane()

    response = _client(plane).put(
        _url(), content=(b"x" * 500 for _ in range(10)), headers=_headers("PUT")
    )

    assert response.status_code == 413
    assert plane.writes == []


def test_a_declared_length_over_the_limit_is_refused_before_any_body_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.api import extension_artifacts

    monkeypatch.setattr(extension_artifacts, "_MAX_UPLOAD_BYTES", 1_000)
    plane = _Plane()

    response = _client(plane).put(
        _url(), content=b"x" * 5_000, headers={**_headers("PUT"), "content-length": "5000"}
    )

    assert response.status_code == 413
    assert plane.writes == []


def test_the_write_does_not_run_on_the_event_loop() -> None:
    """A synchronous write inside an async handler stalls every other request."""
    import asyncio

    seen: list[bool] = []

    class _LoopAwarePlane(_Plane):
        def write_artifact_content(self, artifact_id, *, body, content_type=""):
            try:
                asyncio.get_running_loop()
                seen.append(True)
            except RuntimeError:
                seen.append(False)
            return super().write_artifact_content(artifact_id, body=body, content_type=content_type)

    response = _client(_LoopAwarePlane()).put(_url(), content=_BODY, headers=_headers("PUT"))

    assert response.status_code == 204
    assert seen == [False], "the storage write ran on the event loop thread"


def test_an_upload_at_the_limit_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The green direction: the limit is inclusive, and a body under it still lands."""
    from core.entrypoints.api import extension_artifacts

    monkeypatch.setattr(extension_artifacts, "_MAX_UPLOAD_BYTES", 1_000)
    plane = _Plane()

    response = _client(plane).put(_url(), content=b"y" * 1_000, headers=_headers("PUT"))

    assert response.status_code == 204
    assert plane.writes == [("art_1", b"y" * 1_000)]
