"""AC17-R152: a failed readiness probe records what the endpoint said, not only its status."""

from __future__ import annotations

import pytest

from core.entrypoints.cli import lifecycle_runtime


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, _limit: int) -> bytes:
        return self._body


def _connection(status: int, body: bytes):
    class _Connection:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request(self, *_args, **_kwargs) -> None:
            pass

        def getresponse(self) -> _Response:
            return _Response(status, body)

        def close(self) -> None:
            pass

    return _Connection


def test_an_unexpected_status_carries_a_redacted_excerpt_of_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"Internal Server Error\n  token=abcdef0123456789abcdef0123456789 " + b"x" * 500
    monkeypatch.setattr(lifecycle_runtime, "HTTPConnection", _connection(500, body))

    with pytest.raises(ValueError) as raised:
        lifecycle_runtime._runtime_health_payload("fs-app", 8099)

    message = str(raised.value)
    assert message.startswith("health endpoint returned HTTP 500: Internal Server Error")
    assert "abcdef0123456789abcdef0123456789" not in message
    assert len(message) < 300


def test_an_empty_body_keeps_the_old_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_runtime, "HTTPConnection", _connection(401, b""))

    with pytest.raises(ValueError, match=r"^health endpoint returned HTTP 401$"):
        lifecycle_runtime._runtime_health_payload("fs-app", 8099)
