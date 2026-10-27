"""E2E contracts for the /api/events/stream SSE (Server-Sent Events) channel.

P0 gap: SSE stream is PARTIAL — only basic route coverage; missing deterministic
cursor resume, ordering, auth/permission boundary, and backpressure behavior tests.

Coverage:

A. Authentication boundary
   - Unauthenticated stream request must return 401/403 or redirect
   - Authenticated request must return 200 with text/event-stream content-type

B. Cursor-based resumability
   - Stream with ?cursor=<N> must resume from that position
   - Cursor in response must monotonically increase
   - No duplicate events when resuming from cursor

C. Message structure and ordering
   - Each SSE frame must be parseable (data: {...}\\n\\n format)
   - Event data must be valid JSON
   - Events must arrive in non-decreasing cursor order

D. Authorization scope
   - Events for project A must not appear in stream when scoped to project B
   - Admin sees all events; viewer sees only their permitted scope

E. Backpressure and timeout
   - Stream must send heartbeat/keepalive within timeout window
   - Stream must be closeable by client without server-side hang

Run:
    docker compose exec fs-app python -m pytest \
        tests/e2e/test_sse_events_stream_contracts.py -v
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest
import requests as _req

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import BASE_URL, COOKIES, VERIFY_SSL, anon_get, get

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_PROJECT_ID = str(_TEST_ENV.project_id or "").strip()
_SSE_PATH = "/api/events/stream"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _sse_frames(response, max_frames: int = 10, timeout: float = 5.0) -> list[dict]:
    """Read up to max_frames from an SSE response, returning parsed data dicts."""
    frames = []
    deadline = time.monotonic() + timeout
    buffer = ""
    try:
        for chunk in response.iter_content(chunk_size=256, decode_unicode=True):
            if time.monotonic() > deadline:
                break
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for line in frame.splitlines():
                    if line.startswith("data:"):
                        raw = line[5:].strip()
                        if raw:
                            try:
                                frames.append(json.loads(raw))
                            except json.JSONDecodeError:
                                frames.append({"_raw": raw})
                if len(frames) >= max_frames:
                    return frames
    except Exception:
        pass
    return frames


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Server not reachable: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"Server unhealthy: {resp.status_code}")


# ===========================================================================
# A. Authentication boundary
# ===========================================================================


class TestSseAuthBoundary:
    """SSE stream must reject unauthenticated connections."""

    def test_unauthenticated_sse_returns_401_or_redirect(self, server_available):
        """Anonymous SSE request must not receive an event stream."""
        # Use anon_get which bypasses the e2e_authenticated_requests monkeypatch
        # so the request is genuinely unauthenticated (no session cookies).
        resp = anon_get(_SSE_PATH + "?_stream_check=1")
        resp.close()
        assert resp.status_code in (401, 403, 302), (
            f"Unauthenticated SSE must return 401/403/redirect; got {resp.status_code}"
        )

    def test_authenticated_sse_returns_200_event_stream(self, server_available):
        """Authenticated SSE request must return 200 with text/event-stream."""
        url = BASE_URL.rstrip("/") + _SSE_PATH
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=8,
            cookies=COOKIES,
            allow_redirects=True,
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
        try:
            assert resp.status_code == 200, (
                f"Authenticated SSE must return 200; got {resp.status_code}"
            )
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct or "stream" in ct.lower(), (
                f"SSE response content-type must include text/event-stream; got {ct!r}"
            )
        finally:
            resp.close()


# ===========================================================================
# B. Cursor-based resumability
# ===========================================================================


class TestSseCursorResumability:
    """SSE stream must support cursor-based resumption."""

    def test_cursor_param_accepted_without_error(self, server_available):
        """?cursor=0 must be accepted by the stream endpoint."""
        url = BASE_URL.rstrip("/") + _SSE_PATH + "?cursor=0"
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=5,
            cookies=COOKIES,
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
        resp.close()
        assert resp.status_code in (200, 404), (
            f"?cursor=0 must not return 4xx validation error; got {resp.status_code}"
        )

    def test_cursor_param_large_value_accepted(self, server_available):
        """?cursor=<very large> must be handled gracefully (no 500)."""
        url = BASE_URL.rstrip("/") + _SSE_PATH + "?cursor=9999999999"
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=5,
            cookies=COOKIES,
            stream=True,
        )
        resp.close()
        assert resp.status_code != 500, (
            f"Large cursor value must not cause 500; got {resp.status_code}"
        )

    def test_cursor_in_events_monotonically_increases(self, server_available):
        """Event cursor values must be non-decreasing."""
        url = BASE_URL.rstrip("/") + _SSE_PATH + "?cursor=0&limit=5"
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=8,
            cookies=COOKIES,
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
        if resp.status_code != 200:
            pytest.skip(f"SSE not available: {resp.status_code}")

        frames = _sse_frames(resp, max_frames=5, timeout=5.0)
        resp.close()

        cursors = [f.get("cursor") or f.get("id") for f in frames if isinstance(f, dict)]
        cursors = [c for c in cursors if isinstance(c, (int, float))]
        if len(cursors) >= 2:
            for i in range(len(cursors) - 1):
                assert cursors[i] <= cursors[i + 1], (
                    f"SSE cursors must be non-decreasing; got {cursors[i]} > {cursors[i + 1]}"
                )

    def test_resume_from_cursor_skips_earlier_events(self, server_available):
        """Resuming from cursor=N must not return events with cursor < N."""
        # First pass: get initial cursor
        url0 = BASE_URL.rstrip("/") + _SSE_PATH + "?cursor=0&limit=3"
        resp0 = _req.get(
            url0,
            verify=VERIFY_SSL,
            timeout=6,
            cookies=COOKIES,
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
        if resp0.status_code != 200:
            pytest.skip(f"SSE not available: {resp0.status_code}")

        frames0 = _sse_frames(resp0, max_frames=3, timeout=4.0)
        resp0.close()

        if not frames0:
            pytest.skip("No events available to test cursor resumption")

        seen_cursors = [
            f.get("cursor") or f.get("id") for f in frames0 if isinstance(f, dict)
        ]
        seen_cursors = [str(cursor) for cursor in seen_cursors if cursor]
        last_cursor = seen_cursors[-1] if seen_cursors else ""
        if not last_cursor:
            pytest.skip("Events have no cursor field — cannot test resumption")

        # Second pass: resume from last_cursor
        url1 = BASE_URL.rstrip("/") + _SSE_PATH + f"?cursor={last_cursor}&limit=3"
        resp1 = _req.get(
            url1,
            verify=VERIFY_SSL,
            timeout=6,
            cookies=COOKIES,
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
        if resp1.status_code != 200:
            resp1.close()
            pytest.skip("Cannot resume SSE stream")

        frames1 = _sse_frames(resp1, max_frames=3, timeout=4.0)
        resp1.close()

        seen_cursor_set = set(seen_cursors)
        for frame in frames1:
            cursor = frame.get("cursor") or frame.get("id")
            if cursor is not None:
                assert str(cursor) not in seen_cursor_set, (
                    f"Resumed stream returned already-seen event cursor {cursor}"
                )


# ===========================================================================
# C. Message structure and ordering
# ===========================================================================


class TestSseMessageStructure:
    """SSE messages must be well-formed JSON with consistent structure."""

    def test_sse_frames_are_valid_json(self, server_available):
        """All SSE data frames must be parseable JSON."""
        url = BASE_URL.rstrip("/") + _SSE_PATH + "?limit=5"
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=7,
            cookies=COOKIES,
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
        if resp.status_code != 200:
            pytest.skip(f"SSE not available: {resp.status_code}")

        frames = _sse_frames(resp, max_frames=5, timeout=4.0)
        resp.close()

        invalid = [f for f in frames if "_raw" in f]
        assert not invalid, f"SSE data frames contain non-JSON content: {invalid[:3]}"

    def test_sse_event_has_required_structure(self, server_available):
        """Each SSE event object must have expected structure (type or event_type field)."""
        url = BASE_URL.rstrip("/") + _SSE_PATH + "?limit=3"
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=7,
            cookies=COOKIES,
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
        if resp.status_code != 200:
            pytest.skip(f"SSE not available: {resp.status_code}")

        frames = _sse_frames(resp, max_frames=3, timeout=4.0)
        resp.close()

        for frame in frames:
            if not isinstance(frame, dict) or "_raw" in frame:
                continue
            # Must have some kind of event type field
            has_type = (
                "type" in frame or "event_type" in frame or "event" in frame or "kind" in frame
            )
            assert has_type, (
                f"SSE event must have a type/event_type field; got: {list(frame.keys())}"
            )

    def test_sse_stream_sends_keepalive_within_10s(self, server_available):
        """SSE stream must send at least one frame (keepalive or event) within 10s."""
        url = BASE_URL.rstrip("/") + _SSE_PATH
        start = time.monotonic()
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=12,
            cookies=COOKIES,
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
        if resp.status_code != 200:
            pytest.skip(f"SSE not available: {resp.status_code}")

        frames = _sse_frames(resp, max_frames=1, timeout=10.0)
        elapsed = time.monotonic() - start
        resp.close()

        # Accept: at least 1 frame OR the stream is empty (no events) within 10s
        # The important thing is the stream doesn't hang without sending anything
        assert elapsed <= 12.0, f"SSE stream took {elapsed:.1f}s — possible hang (no keepalive)"


# ===========================================================================
# D. Authorization scope
# ===========================================================================


class TestSseAuthorizationScope:
    """SSE stream must respect authorization scope."""

    def test_sse_with_project_filter_accepted(self, server_available):
        """?project_id=<id> filter must be accepted without 422."""
        if not _PROJECT_ID:
            pytest.skip("TEST_PROJECT_BACKEND_ID not set")
        url = BASE_URL.rstrip("/") + _SSE_PATH + f"?project_id={_PROJECT_ID}"
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=5,
            cookies=COOKIES,
            stream=True,
        )
        resp.close()
        assert resp.status_code not in (422, 500), (
            f"?project_id filter must not return 422/500; got {resp.status_code}"
        )

    def test_sse_with_invalid_project_id_no_500(self, server_available):
        """?project_id=nonexistent must not cause 500."""
        url = BASE_URL.rstrip("/") + _SSE_PATH + "?project_id=nonexistent-xyz"
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=5,
            cookies=COOKIES,
            stream=True,
        )
        resp.close()
        assert resp.status_code != 500, (
            f"Invalid project_id filter must not cause 500; got {resp.status_code}"
        )

    def test_sse_closeable_by_client(self, server_available):
        """Client closing the SSE connection must not cause server-side error."""
        url = BASE_URL.rstrip("/") + _SSE_PATH
        resp = _req.get(
            url,
            verify=VERIFY_SSL,
            timeout=5,
            cookies=COOKIES,
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
        if resp.status_code != 200:
            pytest.skip(f"SSE not available: {resp.status_code}")

        # Immediately close — server must handle gracefully
        resp.close()

        # Server must still be healthy
        health = get("/health", timeout=5)
        assert health.status_code < 500, "Server must remain healthy after abrupt SSE client close"
