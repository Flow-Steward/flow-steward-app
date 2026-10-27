from __future__ import annotations

import hashlib
import random

import pytest

from core.application.tooling.testing import fuzz_api
from core.application.tooling.testing.fuzz_api import (
    _check_login_payload_fuzz,
    _check_search_query_fuzz,
)


class _Response:
    status_code = 200


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _Response()


def test_nightfire_seed_7331_replays_search_payload_with_request_deadline() -> None:
    session = _RecordingSession()

    result = _check_search_query_fuzz(
        session=session,  # type: ignore[arg-type]
        base_url="https://flow.example",
        verify=True,
        timeout=15.0,
        rng=random.Random(7331),
        max_text_size=4096,
    )

    assert result.status == "pass"
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://flow.example/api/search"
    assert session.calls[0]["timeout"] == 15.0
    assert session.calls[0]["verify"] is True
    assert session.calls[0]["params"]["category"] == "all"  # type: ignore[index]
    assert session.calls[0]["params"]["limit"] == 20  # type: ignore[index]
    query = session.calls[0]["params"]["query"]  # type: ignore[index]
    assert len(query) == 2085
    assert query.startswith("<script>alert(1)</script>' OR 1=1 -- ")
    assert (
        hashlib.sha256(query.encode()).hexdigest()
        == (
            "6e33a31023a869526668241ee2d0fd1cc29892fe836b2b97c6c8afea5cd1450a"  # pragma: allowlist secret
        )
    )


def test_login_payload_fuzz_accepts_validation_rejection_as_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fuzz_api.requests, "post", lambda *_args, **_kwargs: _ValidationResponse())

    result = _check_login_payload_fuzz(
        base_url="https://flow.example",
        verify=True,
        timeout=15.0,
        rng=random.Random(7331),
        max_text_size=4096,
    )

    assert result.status == "pass"
    assert result.http_status == 422


class _ValidationResponse:
    status_code = 422
